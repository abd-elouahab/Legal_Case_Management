"""Integration tests for Settings.

The whole of ``20-settings.md``'s Testing section, end to end against the real
application: profile updates, password changes, notification and communication
preferences, AI and dashboard preferences, administrator settings, authorization,
and persistence.

Two properties get more attention than the rest, because they are the ones this
feature could plausibly get wrong:

* **authorization**, which here is mostly the *absence* of a parameter — every
  route is about the caller, so the assertions are that one account's settings
  never reach another and that the administrative surface is closed to everybody
  else;
* **ownership**, the spec's *"each feature should own its configuration"* — that
  notification preferences are served by the Notification Service and that the
  Settings API has no second endpoint for them.

Only the session registry and the metrics recorder are doubles; the queries,
the schemas, the router, and the role policy are the application's own.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from models.user import UserRole

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
SETTINGS_URL = f"{settings.API_V1_PREFIX}/settings"
PROFILE_URL = f"{SETTINGS_URL}/profile"
PREFERENCES_URL = f"{SETTINGS_URL}/preferences"
SESSIONS_URL = f"{SETTINGS_URL}/sessions"
PASSWORD_URL = f"{SETTINGS_URL}/password"
ADMINISTRATION_URL = f"{SETTINGS_URL}/administration"
MAINTENANCE_URL = f"{SETTINGS_URL}/maintenance"
METRICS_URL = f"{SETTINGS_URL}/metrics"
NOTIFICATION_PREFERENCES_URL = f"{settings.API_V1_PREFIX}/notifications/preferences"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    access_token: str = response.json()["access_token"]
    return access_token


def setting_of(payload: dict[str, Any], key: str) -> dict[str, Any]:
    for entry in payload["settings"]:
        if entry["key"] == key:
            return entry
    raise AssertionError(f"{key} absent; got {[e['key'] for e in payload['settings']]}")


@pytest.fixture
def lawyer(make_user):  # type: ignore[no-untyped-def]
    return make_user(
        email="lawyer@example.com", role=UserRole.LAWYER, first_name="Karim", last_name="Idrissi"
    )


@pytest.fixture
def administrator(make_user):  # type: ignore[no-untyped-def]
    return make_user(email="admin@example.com", role=UserRole.ADMINISTRATOR)


# --------------------------------------------------------------------------- #
# The unified view
# --------------------------------------------------------------------------- #


class TestOverview:
    def test_one_request_returns_the_whole_page(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        response = api_client.get(SETTINGS_URL, headers=bearer(token))

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["profile"]["email"] == lawyer.email
        assert payload["settings"]["settings"]
        assert payload["settings"]["definitions"]
        assert payload["maintenance"] == {"maintenance_mode": False, "message": None}

    def test_the_section_list_is_served_rather_than_assumed(
        self, api_client: TestClient, lawyer
    ) -> None:
        """`20-settings.md`: support future sections without redesign.

        A client renders its navigation from this, so a tenth section reaches a
        browser nobody redeployed.
        """
        token = token_for(api_client, lawyer.email)

        payload = api_client.get(SETTINGS_URL, headers=bearer(token)).json()

        assert [entry["section"] for entry in payload["sections"]] == [
            "profile",
            "security",
            "notifications",
            "communication",
            "ai",
            "dashboard",
            "appearance",
            "language",
        ]

    def test_the_notification_sections_name_the_feature_that_owns_them(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        payload = api_client.get(SETTINGS_URL, headers=bearer(token)).json()
        storage = {entry["section"]: entry["storage"] for entry in payload["sections"]}

        assert storage["notifications"] == "notification_preferences"
        assert storage["communication"] == "notification_preferences"

    def test_administration_is_invisible_without_the_capability(
        self, api_client: TestClient, lawyer, administrator
    ) -> None:
        """Omitted entirely rather than served disabled.

        Showing it would tell every lawyer which platform settings exist and that
        somebody else controls them.
        """
        lawyer_sections = api_client.get(
            SETTINGS_URL, headers=bearer(token_for(api_client, lawyer.email))
        ).json()["sections"]
        admin_sections = api_client.get(
            SETTINGS_URL, headers=bearer(token_for(api_client, administrator.email))
        ).json()["sections"]

        assert "administration" not in {entry["section"] for entry in lawyer_sections}
        assert "administration" in {entry["section"] for entry in admin_sections}

    def test_an_anonymous_caller_is_refused(self, api_client: TestClient) -> None:
        assert api_client.get(SETTINGS_URL).status_code == 401


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


class TestProfile:
    def test_a_person_can_change_their_own_name_and_job_title(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        response = api_client.patch(
            PROFILE_URL,
            headers=bearer(token),
            json={"last_name": "Alaoui", "job_title": "Senior Associate"},
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["last_name"] == "Alaoui"
        assert payload["job_title"] == "Senior Associate"
        assert payload["full_name"] == "Karim Alaoui"

    def test_a_change_survives_a_new_request(
        self, api_client: TestClient, lawyer
    ) -> None:
        """`20-settings.md`: settings survive logout, login, and device changes.

        A fresh sign-in is the strongest form of that this suite can express, and
        it is what rules out the answer being held in the client.
        """
        token = token_for(api_client, lawyer.email)
        api_client.patch(
            PROFILE_URL, headers=bearer(token), json={"job_title": "Partner"}
        )

        fresh = token_for(api_client, lawyer.email)
        payload = api_client.get(PROFILE_URL, headers=bearer(fresh)).json()

        assert payload["job_title"] == "Partner"

    def test_a_field_can_be_cleared_and_omission_leaves_one_alone(
        self, api_client: TestClient, make_user
    ) -> None:
        user = make_user(email="phone@example.com", phone="+212600000000")
        token = token_for(api_client, user.email)
        api_client.patch(PROFILE_URL, headers=bearer(token), json={"job_title": "Clerk"})

        # An explicit null clears; the omitted job title is untouched.
        payload = api_client.patch(
            PROFILE_URL, headers=bearer(token), json={"phone": None}
        ).json()

        assert payload["phone"] is None
        assert payload["job_title"] == "Clerk"

    def test_email_role_and_status_are_not_editable(
        self, api_client: TestClient, lawyer
    ) -> None:
        """Not ignored — *refused*. A self-service endpoint accepting `role` would
        be a privilege-escalation door however carefully the service behind it was
        written, so the field does not exist and `extra="forbid"` says so.
        """
        token = token_for(api_client, lawyer.email)

        for field, value in (
            ("email", "someone.else@example.com"),
            ("role", "administrator"),
            ("status", "active"),
        ):
            response = api_client.patch(
                PROFILE_URL, headers=bearer(token), json={field: value}
            )
            assert response.status_code == 422, f"{field}: {response.text}"

        assert lawyer.role is UserRole.LAWYER

    def test_an_invalid_phone_number_is_refused(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        response = api_client.patch(
            PROFILE_URL, headers=bearer(token), json={"phone": "not a number"}
        )

        assert response.status_code == 422

    def test_a_profile_is_the_callers_own_and_names_nobody_else(
        self, api_client: TestClient, lawyer, administrator
    ) -> None:
        token = token_for(api_client, lawyer.email)

        payload = api_client.get(PROFILE_URL, headers=bearer(token)).json()

        assert payload["id"] == str(lawyer.id)
        assert administrator.email not in response_text(payload)


def response_text(payload: Any) -> str:
    import json

    return json.dumps(payload)


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


class TestPreferences:
    def test_every_setting_comes_back_with_how_to_render_it(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        payload = api_client.get(PREFERENCES_URL, headers=bearer(token)).json()

        keys = {entry["key"] for entry in payload["settings"]}
        assert {"theme", "language", "ai_streaming", "dashboard_range"} <= keys
        # Every setting has a definition, which is what lets the client render one
        # it has never heard of.
        assert keys == {entry["key"] for entry in payload["definitions"]}

    def test_an_untouched_account_reports_platform_defaults(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        payload = api_client.get(PREFERENCES_URL, headers=bearer(token)).json()

        assert all(entry["is_default"] for entry in payload["settings"])

    def test_ai_preferences_are_stored_and_survive_a_new_session(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        api_client.put(
            PREFERENCES_URL,
            headers=bearer(token),
            json={
                "settings": [
                    {"setting_key": "ai_response_length", "value": "concise"},
                    {"setting_key": "ai_streaming", "value": False},
                    {"setting_key": "ai_citations", "value": "hidden"},
                ]
            },
        )

        fresh = api_client.get(
            PREFERENCES_URL, headers=bearer(token_for(api_client, lawyer.email))
        ).json()

        assert setting_of(fresh, "ai_response_length")["value"] == "concise"
        assert setting_of(fresh, "ai_streaming")["value"] is False
        assert setting_of(fresh, "ai_citations")["value"] == "hidden"
        assert setting_of(fresh, "ai_streaming")["is_default"] is False

    def test_dashboard_preferences_are_stored(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        response = api_client.put(
            PREFERENCES_URL,
            headers=bearer(token),
            json={
                "settings": [
                    {"setting_key": "dashboard_range", "value": "last_7_days"},
                    {
                        "setting_key": "dashboard_widgets",
                        "value": ["my_cases", "upcoming_hearings"],
                    },
                ]
            },
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert setting_of(payload, "dashboard_range")["value"] == "last_7_days"
        assert setting_of(payload, "dashboard_widgets")["value"] == [
            "my_cases",
            "upcoming_hearings",
        ]

    def test_appearance_and_language_are_stored(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        payload = api_client.put(
            PREFERENCES_URL,
            headers=bearer(token),
            json={
                "settings": [
                    {"setting_key": "theme", "value": "light"},
                    {"setting_key": "language", "value": "ar"},
                    {"setting_key": "timezone", "value": "Africa/Casablanca"},
                    {"setting_key": "date_format", "value": "year_month_day"},
                    {"setting_key": "time_format", "value": "hour_12"},
                ]
            },
        ).json()

        assert setting_of(payload, "theme")["value"] == "light"
        assert setting_of(payload, "language")["value"] == "ar"
        assert setting_of(payload, "timezone")["value"] == "Africa/Casablanca"

    def test_an_omitted_setting_keeps_its_value(
        self, api_client: TestClient, lawyer
    ) -> None:
        """Two settings panels open at once must not revert each other's saves."""
        token = token_for(api_client, lawyer.email)
        api_client.put(
            PREFERENCES_URL,
            headers=bearer(token),
            json={"settings": [{"setting_key": "theme", "value": "light"}]},
        )

        payload = api_client.put(
            PREFERENCES_URL,
            headers=bearer(token),
            json={"settings": [{"setting_key": "language", "value": "ar"}]},
        ).json()

        assert setting_of(payload, "theme")["value"] == "light"

    def test_an_invalid_value_changes_nothing(
        self, api_client: TestClient, lawyer
    ) -> None:
        """`20-settings.md`: invalid configuration must never corrupt stored preferences."""
        token = token_for(api_client, lawyer.email)

        response = api_client.put(
            PREFERENCES_URL,
            headers=bearer(token),
            json={
                "settings": [
                    {"setting_key": "theme", "value": "light"},
                    {"setting_key": "timezone", "value": "Middle/Earth"},
                ]
            },
        )

        assert response.status_code == 422, response.text
        assert response.json()["error"] == "invalid_setting_value"

        after = api_client.get(PREFERENCES_URL, headers=bearer(token)).json()
        assert setting_of(after, "theme")["is_default"] is True

    def test_an_unknown_setting_is_refused_and_names_itself(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        response = api_client.put(
            PREFERENCES_URL,
            headers=bearer(token),
            json={"settings": [{"setting_key": "a_setting_from_next_year", "value": True}]},
        )

        # Rejected by the schema's enum before the service is reached, which is
        # where an unrecognised *key* belongs — the value's vocabulary is the
        # registry's business, the key's is the contract's.
        assert response.status_code == 422

    def test_the_same_setting_twice_in_one_request_is_refused(
        self, api_client: TestClient, lawyer
    ) -> None:
        # Two entries for one key have no defensible resolution: taking the last
        # would make the outcome depend on JSON ordering.
        token = token_for(api_client, lawyer.email)

        response = api_client.put(
            PREFERENCES_URL,
            headers=bearer(token),
            json={
                "settings": [
                    {"setting_key": "theme", "value": "light"},
                    {"setting_key": "theme", "value": "dark"},
                ]
            },
        )

        assert response.status_code == 422

    def test_one_persons_settings_never_reach_another(
        self, api_client: TestClient, lawyer, administrator
    ) -> None:
        """The whole of *"users may modify only their own settings"*.

        There is no parameter naming an account, so this asserts the consequence:
        two callers get two answers from one endpoint.
        """
        api_client.put(
            PREFERENCES_URL,
            headers=bearer(token_for(api_client, lawyer.email)),
            json={"settings": [{"setting_key": "theme", "value": "light"}]},
        )

        other = api_client.get(
            PREFERENCES_URL, headers=bearer(token_for(api_client, administrator.email))
        ).json()

        assert setting_of(other, "theme")["is_default"] is True
        assert setting_of(other, "theme")["value"] == "dark"


# --------------------------------------------------------------------------- #
# Notification and communication preferences
# --------------------------------------------------------------------------- #


class TestNotificationOwnership:
    def test_the_settings_api_has_no_notification_preference_route(
        self, api_client: TestClient
    ) -> None:
        """`20-settings.md`: each feature owns its configuration.

        Asserted rather than left to review, in the shape `EMAIL_RULES`' absences
        are: a second endpoint for one stored thing is how two answers to one
        question start to disagree.
        """
        paths = api_client.app.openapi()["paths"]  # type: ignore[attr-defined]
        settings_paths = {path for path in paths if path.startswith(f"{SETTINGS_URL}")}

        assert not any("notification" in path for path in settings_paths)
        assert not any("communication" in path for path in settings_paths)

    def test_notification_and_communication_preferences_share_one_store(
        self, api_client: TestClient, lawyer
    ) -> None:
        """The two sections are two projections of one grid, not two stores.

        Switching the in-app channel off (the Notifications section) and the email
        channel off (the Communication section) reaches the same row.
        """
        token = token_for(api_client, lawyer.email)

        api_client.put(
            NOTIFICATION_PREFERENCES_URL,
            headers=bearer(token),
            json={"preferences": [{"preference_key": "hearing_updates", "in_app": False}]},
        )
        payload = api_client.put(
            NOTIFICATION_PREFERENCES_URL,
            headers=bearer(token),
            json={"preferences": [{"preference_key": "hearing_updates", "email": False}]},
        ).json()

        row = next(
            entry
            for entry in payload["preferences"]
            if entry["preference_key"] == "hearing_updates"
        )
        assert row["in_app"] is False
        assert row["email"] is False
        # The channel nobody touched is untouched, which is what the per-channel
        # partial update exists for.
        assert row["whatsapp"] is True


# --------------------------------------------------------------------------- #
# Account & security
# --------------------------------------------------------------------------- #


class TestPassword:
    def test_a_password_change_requires_the_current_one(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        response = api_client.post(
            PASSWORD_URL,
            headers=bearer(token),
            json={"current_password": "not-it", "new_password": "a-brand-new-secret"},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_password"

    def test_a_successful_change_hands_back_a_working_token(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        response = api_client.post(
            PASSWORD_URL,
            headers=bearer(token),
            json={"current_password": PASSWORD, "new_password": "a-brand-new-secret"},
        )

        assert response.status_code == 200, response.text
        replacement = response.json()["access_token"]
        # The device making the change stays signed in.
        assert api_client.get(PROFILE_URL, headers=bearer(replacement)).status_code == 200
        # And the new password is the one that works.
        assert token_for(api_client, lawyer.email, "a-brand-new-secret")

    def test_it_clears_the_must_change_flag(
        self, api_client: TestClient, make_user
    ) -> None:
        """`20-settings.md`'s Password Change Policy, first clause."""
        user = make_user(email="reset@example.com", must_change_password=True)
        token = token_for(api_client, user.email)

        response = api_client.post(
            PASSWORD_URL,
            headers=bearer(token),
            json={"current_password": PASSWORD, "new_password": "a-brand-new-secret"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["user"]["must_change_password"] is False

    def test_it_invalidates_every_other_session(
        self, api_client: TestClient, lawyer
    ) -> None:
        """`20-settings.md`'s Password Change Policy, second and third clauses."""
        other_device = token_for(api_client, lawyer.email)
        this_device = token_for(api_client, lawyer.email)

        api_client.post(
            PASSWORD_URL,
            headers=bearer(this_device),
            json={"current_password": PASSWORD, "new_password": "a-brand-new-secret"},
        )

        assert api_client.get(PROFILE_URL, headers=bearer(other_device)).status_code == 401


class TestSessions:
    def test_each_sign_in_is_listed_once(
        self, api_client: TestClient, lawyer
    ) -> None:
        """A session is a *sign-in*, not a credential.

        Two sign-ins are two rows; the token rotations a browser performs every
        fifteen minutes are not, which is why the `sid` claim exists separately
        from `jti`.
        """
        token_for(api_client, lawyer.email)
        second = token_for(api_client, lawyer.email)

        payload = api_client.get(SESSIONS_URL, headers=bearer(second)).json()

        assert payload["available"] is True
        assert len(payload["sessions"]) == 2
        assert sum(1 for entry in payload["sessions"] if entry["is_current"]) == 1

    def test_a_refresh_does_not_create_a_second_session(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)
        before = api_client.get(SESSIONS_URL, headers=bearer(token)).json()

        refreshed = api_client.post(f"{settings.API_V1_PREFIX}/auth/refresh")
        assert refreshed.status_code == 200, refreshed.text

        after = api_client.get(
            SESSIONS_URL, headers=bearer(refreshed.json()["access_token"])
        ).json()

        assert len(after["sessions"]) == len(before["sessions"])
        assert {entry["session_id"] for entry in after["sessions"]} == {
            entry["session_id"] for entry in before["sessions"]
        }

    def test_a_session_carries_no_credential(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        payload = api_client.get(SESSIONS_URL, headers=bearer(token)).json()
        fields = set(payload["sessions"][0])

        assert fields == {
            "session_id",
            "is_current",
            "created_at",
            "last_seen_at",
            "expires_at",
            "ip_address",
            "user_agent",
        }

    def test_signing_out_elsewhere_ends_every_other_session(
        self, api_client: TestClient, lawyer
    ) -> None:
        other_device = token_for(api_client, lawyer.email)
        this_device = token_for(api_client, lawyer.email)

        response = api_client.request(
            "DELETE", SESSIONS_URL, headers=bearer(this_device)
        )

        assert response.status_code == 200, response.text
        replacement = response.json()["access_token"]
        # This device keeps working, on the replacement pair.
        assert api_client.get(PROFILE_URL, headers=bearer(replacement)).status_code == 200
        # The other one does not.
        assert api_client.get(PROFILE_URL, headers=bearer(other_device)).status_code == 401

    def test_only_this_session_is_left_in_the_list(
        self, api_client: TestClient, lawyer
    ) -> None:
        token_for(api_client, lawyer.email)
        this_device = token_for(api_client, lawyer.email)

        replacement = api_client.request(
            "DELETE", SESSIONS_URL, headers=bearer(this_device)
        ).json()["access_token"]

        payload = api_client.get(SESSIONS_URL, headers=bearer(replacement)).json()
        assert len(payload["sessions"]) == 1
        assert payload["sessions"][0]["is_current"] is True

    def test_sessions_are_the_callers_own(
        self, api_client: TestClient, lawyer, administrator
    ) -> None:
        token_for(api_client, lawyer.email)
        token_for(api_client, lawyer.email)
        admin_token = token_for(api_client, administrator.email)

        payload = api_client.get(SESSIONS_URL, headers=bearer(admin_token)).json()

        # An administrator sees their own single session, never the lawyer's two:
        # where and when somebody signs in is a detailed statement about their
        # working life, and no permission grants it.
        assert len(payload["sessions"]) == 1


# --------------------------------------------------------------------------- #
# Administration
# --------------------------------------------------------------------------- #


class TestAdministration:
    def test_a_lawyer_cannot_read_platform_settings(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        assert api_client.get(ADMINISTRATION_URL, headers=bearer(token)).status_code == 403

    def test_a_lawyer_cannot_change_platform_settings(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        response = api_client.put(
            ADMINISTRATION_URL,
            headers=bearer(token),
            json={"settings": [{"setting_key": "maintenance_mode", "value": True}]},
        )

        assert response.status_code == 403

    def test_a_court_representative_cannot_either(
        self, api_client: TestClient, make_user
    ) -> None:
        user = make_user(email="court@example.com", role=UserRole.COURT_REPRESENTATIVE)
        token = token_for(api_client, user.email)

        assert api_client.get(ADMINISTRATION_URL, headers=bearer(token)).status_code == 403

    def test_an_administrator_can_configure_the_platform(
        self, api_client: TestClient, administrator
    ) -> None:
        token = token_for(api_client, administrator.email)

        response = api_client.put(
            ADMINISTRATION_URL,
            headers=bearer(token),
            json={
                "settings": [
                    {"setting_key": "default_language", "value": "ar"},
                    {"setting_key": "ai_default_streaming", "value": False},
                ]
            },
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert setting_of(payload, "default_language")["value"] == "ar"
        assert setting_of(payload, "ai_default_streaming")["value"] is False

    def test_a_platform_default_reaches_an_account_that_chose_nothing(
        self, api_client: TestClient, administrator, lawyer
    ) -> None:
        """The reason administrator defaults exist rather than merely being stored.

        No backfill is involved: the account has no row, so it reads the
        platform's answer.
        """
        api_client.put(
            ADMINISTRATION_URL,
            headers=bearer(token_for(api_client, administrator.email)),
            json={"settings": [{"setting_key": "default_theme", "value": "light"}]},
        )

        payload = api_client.get(
            PREFERENCES_URL, headers=bearer(token_for(api_client, lawyer.email))
        ).json()
        theme = setting_of(payload, "theme")

        assert theme["value"] == "light"
        assert theme["is_default"] is True

    def test_a_personal_choice_is_not_overwritten_by_a_platform_default(
        self, api_client: TestClient, administrator, lawyer
    ) -> None:
        api_client.put(
            PREFERENCES_URL,
            headers=bearer(token_for(api_client, lawyer.email)),
            json={"settings": [{"setting_key": "theme", "value": "dark"}]},
        )
        api_client.put(
            ADMINISTRATION_URL,
            headers=bearer(token_for(api_client, administrator.email)),
            json={"settings": [{"setting_key": "default_theme", "value": "light"}]},
        )

        payload = api_client.get(
            PREFERENCES_URL, headers=bearer(token_for(api_client, lawyer.email))
        ).json()

        assert setting_of(payload, "theme")["value"] == "dark"

    def test_administrator_settings_never_appear_among_a_persons_own(
        self, api_client: TestClient, administrator
    ) -> None:
        """The two registries share no key, so this is structural — asserted anyway."""
        token = token_for(api_client, administrator.email)

        personal = api_client.get(PREFERENCES_URL, headers=bearer(token)).json()
        platform = api_client.get(ADMINISTRATION_URL, headers=bearer(token)).json()

        personal_keys = {entry["key"] for entry in personal["settings"]}
        platform_keys = {entry["key"] for entry in platform["settings"]}
        assert personal_keys & platform_keys == set()


class TestMaintenance:
    def test_everybody_may_read_the_maintenance_posture(
        self, api_client: TestClient, administrator, lawyer
    ) -> None:
        """The switch is administrative; the announcement is not.

        A maintenance notice only administrators can see is a notice nobody
        needed.
        """
        api_client.put(
            ADMINISTRATION_URL,
            headers=bearer(token_for(api_client, administrator.email)),
            json={
                "settings": [
                    {"setting_key": "maintenance_message", "value": "Back at 18:00"},
                    {"setting_key": "maintenance_mode", "value": True},
                ]
            },
        )

        response = api_client.get(
            MAINTENANCE_URL, headers=bearer(token_for(api_client, lawyer.email))
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"maintenance_mode": True, "message": "Back at 18:00"}

    def test_a_draft_notice_is_not_published(
        self, api_client: TestClient, administrator, lawyer
    ) -> None:
        api_client.put(
            ADMINISTRATION_URL,
            headers=bearer(token_for(api_client, administrator.email)),
            json={"settings": [{"setting_key": "maintenance_message", "value": "Soon"}]},
        )

        payload = api_client.get(
            MAINTENANCE_URL, headers=bearer(token_for(api_client, lawyer.email))
        ).json()

        assert payload == {"maintenance_mode": False, "message": None}

    def test_maintenance_mode_does_not_close_the_platform(
        self, api_client: TestClient, administrator, lawyer
    ) -> None:
        """Announced rather than enforced, and asserted so the scope is explicit.

        Refusing traffic would be a platform-wide behaviour change this spec does
        not describe. See `progress-tracker.md` for the open question.
        """
        api_client.put(
            ADMINISTRATION_URL,
            headers=bearer(token_for(api_client, administrator.email)),
            json={"settings": [{"setting_key": "maintenance_mode", "value": True}]},
        )

        token = token_for(api_client, lawyer.email)
        assert api_client.get(PROFILE_URL, headers=bearer(token)).status_code == 200


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestMetrics:
    def test_the_metrics_view_is_administrative(
        self, api_client: TestClient, lawyer
    ) -> None:
        token = token_for(api_client, lawyer.email)

        assert api_client.get(METRICS_URL, headers=bearer(token)).status_code == 403

    def test_it_reports_the_four_figures_the_spec_names(
        self, api_client: TestClient, administrator, lawyer
    ) -> None:
        api_client.put(
            PREFERENCES_URL,
            headers=bearer(token_for(api_client, lawyer.email)),
            json={"settings": [{"setting_key": "theme", "value": "light"}]},
        )
        api_client.patch(
            PROFILE_URL,
            headers=bearer(token_for(api_client, lawyer.email)),
            json={"job_title": "Associate"},
        )

        payload = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, administrator.email))
        ).json()

        assert payload["updated"] >= 1
        assert payload["profile_changes"] == 1
        assert payload["failed"] == 0
        assert "password_changes" in payload
        # Counted by section, never by setting and never by person.
        assert payload["updated_by_section"]["appearance"] == 1
        assert payload["customised_users"] == 1

    def test_a_rejected_change_is_counted_as_a_failure(
        self, api_client: TestClient, administrator, lawyer
    ) -> None:
        api_client.put(
            PREFERENCES_URL,
            headers=bearer(token_for(api_client, lawyer.email)),
            json={"settings": [{"setting_key": "timezone", "value": "Middle/Earth"}]},
        )

        payload = api_client.get(
            METRICS_URL, headers=bearer(token_for(api_client, administrator.email))
        ).json()

        assert payload["failed"] == 1
        assert payload["failures_by_reason"] == {"invalid_value": 1}
