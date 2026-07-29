"""Integration tests for the Case Management API.

Exercise the endpoints over real HTTP: the CRUD contract, validation responses,
authorization (401 vs 403 for every route and every role, plus the per-case
assignment check), assignment, search, filtering, sorting, and pagination.

The service-level rules are unit-tested in ``tests/unit/test_case_service.py``;
what these add is the wire contract — status codes, response shapes, error
envelopes, and the guarantee that a lawyer cannot reach a case they are not on.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.config import settings
from models.case import Case, CasePriority, CaseStatus
from models.user import User, UserRole

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
CASES_URL = f"{settings.API_V1_PREFIX}/cases"

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == status.HTTP_200_OK, response.text
    access_token: str = response.json()["access_token"]
    return access_token


def creation_payload(**overrides: object) -> dict[str, object]:
    return {"title": "Benali v. Societe Atlas", **overrides}


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)


@pytest.fixture
def admin_headers(api_client: TestClient, admin: User) -> dict[str, str]:
    return bearer(token_for(api_client, admin.email))


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(
        email="lawyer@example.com",
        password=PASSWORD,
        first_name="Karim",
        last_name="Zahra",
        role=UserRole.LAWYER,
    )


@pytest.fixture
def lawyer_headers(api_client: TestClient, lawyer: User) -> dict[str, str]:
    return bearer(token_for(api_client, lawyer.email))


@pytest.fixture
def representative(make_user: MakeUser) -> User:
    return make_user(
        email="court@example.com",
        password=PASSWORD,
        first_name="Nadia",
        last_name="Alami",
        role=UserRole.COURT_REPRESENTATIVE,
    )


@pytest.fixture
def representative_headers(api_client: TestClient, representative: User) -> dict[str, str]:
    return bearer(token_for(api_client, representative.email))


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


class TestAuthentication:
    """No credentials must be 401, never 403 — the two are not interchangeable."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", ""),
            ("POST", ""),
            ("GET", "/{id}"),
            ("PATCH", "/{id}"),
            ("PATCH", "/{id}/assignments"),
            ("DELETE", "/{id}"),
        ],
    )
    def test_every_route_requires_authentication(
        self, api_client: TestClient, method: str, path: str
    ) -> None:
        url = CASES_URL + path.format(id=uuid.uuid4())

        response = api_client.request(method, url, json={})

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        # RFC 6750: tell the client that Bearer credentials are expected.
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_a_malformed_token_is_also_401(self, api_client: TestClient) -> None:
        response = api_client.get(CASES_URL, headers=bearer("not-a-real-token"))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestCapabilityAuthorization:
    """Only holders of the matching ``cases:*`` permission may act."""

    @pytest.mark.parametrize("headers_fixture", ["lawyer_headers", "representative_headers"])
    def test_a_restricted_role_cannot_create_a_case(
        self, api_client: TestClient, request: pytest.FixtureRequest, headers_fixture: str
    ) -> None:
        headers: dict[str, str] = request.getfixturevalue(headers_fixture)

        response = api_client.post(CASES_URL, json=creation_payload(), headers=headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_an_assigned_lawyer_still_cannot_archive(
        self, api_client: TestClient, lawyer: User, lawyer_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        # Archiving is `cases:delete`. Being on the case does not confer it.
        legal_case = make_case(assigned_lawyer_id=lawyer.id)

        response = api_client.delete(f"{CASES_URL}/{legal_case.id}", headers=lawyer_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_an_assigned_lawyer_cannot_reassign(
        self,
        api_client: TestClient,
        lawyer: User,
        lawyer_headers: dict[str, str],
        representative: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer.id)

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_court_representative_id": str(representative.id)},
            headers=lawyer_headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_denial_names_neither_permission_nor_role(
        self, api_client: TestClient, lawyer_headers: dict[str, str]
    ) -> None:
        response = api_client.post(CASES_URL, json=creation_payload(), headers=lawyer_headers)
        body = response.json()

        assert body["error"] == "forbidden"
        assert "cases:create" not in response.text
        assert "administrator" not in response.text


class TestResourceAuthorization:
    """A permission grants a capability, not a row."""

    def test_an_unassigned_lawyer_cannot_read_a_case(
        self, api_client: TestClient, lawyer_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()

        response = api_client.get(f"{CASES_URL}/{legal_case.id}", headers=lawyer_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error"] == "forbidden"

    def test_an_assigned_lawyer_can_read_their_case(
        self,
        api_client: TestClient,
        lawyer: User,
        lawyer_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer.id)

        response = api_client.get(f"{CASES_URL}/{legal_case.id}", headers=lawyer_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["id"] == str(legal_case.id)

    def test_the_list_is_scoped_to_the_callers_assignments(
        self,
        api_client: TestClient,
        lawyer: User,
        lawyer_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        mine = make_case(assigned_lawyer_id=lawyer.id)
        make_case()
        make_case()

        body = api_client.get(CASES_URL, headers=lawyer_headers).json()

        assert [item["id"] for item in body["items"]] == [str(mine.id)]
        # The total must count only what the caller may see, or the pagination
        # footer becomes a leak of how large the caseload really is.
        assert body["total_records"] == 1

    def test_an_administrator_sees_everything(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer: User,
        make_case: MakeCase,
    ) -> None:
        make_case(assigned_lawyer_id=lawyer.id)
        make_case()

        assert api_client.get(CASES_URL, headers=admin_headers).json()["total_records"] == 2

    def test_a_representative_reaches_only_the_cases_they_cover(
        self,
        api_client: TestClient,
        representative: User,
        representative_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        mine = make_case(assigned_court_representative_id=representative.id)
        theirs = make_case()

        assert (
            api_client.get(f"{CASES_URL}/{mine.id}", headers=representative_headers).status_code
            == status.HTTP_200_OK
        )
        assert (
            api_client.get(f"{CASES_URL}/{theirs.id}", headers=representative_headers).status_code
            == status.HTTP_403_FORBIDDEN
        )


class TestFieldAuthorization:
    """``cases:update-hearing`` reaches the court-facing fields and no others."""

    def test_a_representative_may_record_a_hearing(
        self,
        api_client: TestClient,
        representative: User,
        representative_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(assigned_court_representative_id=representative.id)

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}",
            json={"next_hearing_date": "2026-06-10", "status": "waiting_for_hearing"},
            headers=representative_headers,
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["next_hearing_date"] == "2026-06-10"
        assert response.json()["status"] == "waiting_for_hearing"

    def test_a_representative_may_not_rewrite_the_case(
        self,
        api_client: TestClient,
        representative: User,
        representative_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(assigned_court_representative_id=representative.id)

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}",
            json={"title": "Rewritten"},
            headers=representative_headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_a_mixed_update_is_refused_in_full(
        self,
        api_client: TestClient,
        representative: User,
        representative_headers: dict[str, str],
        make_case: MakeCase,
        admin_headers: dict[str, str],
    ) -> None:
        legal_case = make_case(
            title="Original",
            court_name="Tribunal de Rabat",
            assigned_court_representative_id=representative.id,
        )

        refused = api_client.patch(
            f"{CASES_URL}/{legal_case.id}",
            json={"title": "Rewritten", "court_name": "Tribunal de Casablanca"},
            headers=representative_headers,
        )
        assert refused.status_code == status.HTTP_403_FORBIDDEN

        # Nothing was applied — not even the field they were allowed to write.
        after = api_client.get(f"{CASES_URL}/{legal_case.id}", headers=admin_headers).json()
        assert after["title"] == "Original"
        assert after["court_name"] == "Tribunal de Rabat"


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #


class TestCreateCase:
    def test_creates_a_case_and_returns_201(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(CASES_URL, json=creation_payload(), headers=admin_headers)

        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()
        assert body["title"] == "Benali v. Societe Atlas"
        assert body["status"] == "draft"
        assert body["priority"] == "medium"

    def test_a_case_number_is_generated_when_omitted(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.post(
            CASES_URL, json=creation_payload(), headers=admin_headers
        ).json()

        assert body["case_number"].startswith("CASE-")

    def test_generated_numbers_are_unique(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        numbers = {
            api_client.post(
                CASES_URL, json=creation_payload(title=f"Matter {index}"), headers=admin_headers
            ).json()["case_number"]
            for index in range(3)
        }

        assert len(numbers) == 3

    def test_a_duplicate_case_number_is_a_409(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        api_client.post(
            CASES_URL, json=creation_payload(case_number="TC/2026/44"), headers=admin_headers
        )

        response = api_client.post(
            CASES_URL,
            json=creation_payload(title="Another", case_number="TC/2026/44"),
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error"] == "case_number_already_exists"

    def test_a_missing_title_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(CASES_URL, json={}, headers=admin_headers)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "title" in response.text

    def test_audit_fields_cannot_be_supplied(
        self, api_client: TestClient, admin_headers: dict[str, str], lawyer: User
    ) -> None:
        response = api_client.post(
            CASES_URL,
            json=creation_payload(created_by=str(lawyer.id)),
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_audit_fields_are_populated_from_the_caller(
        self, api_client: TestClient, admin: User, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.post(
            CASES_URL, json=creation_payload(), headers=admin_headers
        ).json()

        assert body["created_by"] == str(admin.id)
        assert body["creator"]["email"] == admin.email

    def test_assignments_are_returned_as_people(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer: User,
        representative: User,
    ) -> None:
        # A bare identifier is useless in a table; the client must not have to
        # resolve every assignee itself.
        body = api_client.post(
            CASES_URL,
            json=creation_payload(
                assigned_lawyer_id=str(lawyer.id),
                assigned_court_representative_id=str(representative.id),
            ),
            headers=admin_headers,
        ).json()

        assert body["assigned_lawyer"]["full_name"] == "Karim Zahra"
        assert body["assigned_court_representative"]["full_name"] == "Nadia Alami"

    def test_an_assignee_with_the_wrong_role_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str], representative: User
    ) -> None:
        response = api_client.post(
            CASES_URL,
            json=creation_payload(assigned_lawyer_id=str(representative.id)),
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["error"] == "invalid_assignment"
        assert response.json()["details"][0]["field"] == "assigned_lawyer_id"

    def test_a_hearing_before_the_filing_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            CASES_URL,
            json=creation_payload(filing_date="2026-05-10", next_hearing_date="2026-05-09"),
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


class TestGetCase:
    def test_returns_the_complete_record(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case(court_name="Tribunal de Rabat", category="Commercial")

        body = api_client.get(f"{CASES_URL}/{legal_case.id}", headers=admin_headers).json()

        assert body["case_number"] == legal_case.case_number
        assert body["court_name"] == "Tribunal de Rabat"
        assert body["category"] == "Commercial"
        assert body["is_archived"] is False

    def test_it_serves_the_legal_next_statuses(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        # So a client offers only valid moves instead of re-implementing the
        # lifecycle rules.
        legal_case = make_case(status=CaseStatus.DRAFT)

        body = api_client.get(f"{CASES_URL}/{legal_case.id}", headers=admin_headers).json()

        assert body["allowed_transitions"] == ["open", "archived"]

    def test_an_unknown_id_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{CASES_URL}/{uuid.uuid4()}", headers=admin_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "case_not_found"


class TestUpdateCase:
    def test_a_partial_update_leaves_other_fields_alone(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case(title="Original", court_name="Tribunal de Rabat")

        body = api_client.patch(
            f"{CASES_URL}/{legal_case.id}", json={"title": "Renamed"}, headers=admin_headers
        ).json()

        assert body["title"] == "Renamed"
        assert body["court_name"] == "Tribunal de Rabat"

    def test_an_explicit_null_clears_a_field(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case(court_name="Tribunal de Rabat")

        body = api_client.patch(
            f"{CASES_URL}/{legal_case.id}", json={"court_name": None}, headers=admin_headers
        ).json()

        assert body["court_name"] is None

    def test_an_empty_body_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}", json={}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.parametrize("field", ["case_number", "id", "created_by", "created_at"])
    def test_an_immutable_field_is_a_422(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_case: MakeCase,
        field: str,
    ) -> None:
        legal_case = make_case()

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}", json={field: "TC/9999/1"}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_a_legal_transition_succeeds(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case(status=CaseStatus.OPEN)

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}", json={"status": "in_progress"}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "in_progress"

    def test_an_illegal_transition_is_a_409(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case(status=CaseStatus.DRAFT)

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}", json={"status": "closed"}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["error"] == "invalid_case_transition"

    def test_the_updater_is_recorded(
        self, api_client: TestClient, admin: User, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()

        body = api_client.patch(
            f"{CASES_URL}/{legal_case.id}", json={"priority": "urgent"}, headers=admin_headers
        ).json()

        assert body["updated_by"] == str(admin.id)
        assert body["updater"]["email"] == admin.email

    def test_a_hearing_moved_before_the_stored_filing_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case(filing_date=date(2026, 5, 10))

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}",
            json={"next_hearing_date": "2026-05-09"},
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["error"] == "invalid_case_dates"


class TestAssignments:
    def test_a_lawyer_can_be_assigned(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        body = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_lawyer_id": str(lawyer.id)},
            headers=admin_headers,
        ).json()

        assert body["assigned_lawyer_id"] == str(lawyer.id)

    def test_a_lawyer_can_be_changed(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer: User,
        make_user: MakeUser,
        make_case: MakeCase,
    ) -> None:
        replacement = make_user(
            email="second.lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER
        )
        legal_case = make_case(assigned_lawyer_id=lawyer.id)

        body = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_lawyer_id": str(replacement.id)},
            headers=admin_headers,
        ).json()

        assert body["assigned_lawyer_id"] == str(replacement.id)

    def test_a_lawyer_can_be_removed(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer.id)

        body = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_lawyer_id": None},
            headers=admin_headers,
        ).json()

        assert body["assigned_lawyer_id"] is None
        assert body["assigned_lawyer"] is None

    def test_changing_one_position_leaves_the_other_alone(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer: User,
        representative: User,
        make_case: MakeCase,
    ) -> None:
        # The omitted-versus-null distinction: omitting the representative must
        # not unassign them.
        legal_case = make_case(assigned_court_representative_id=representative.id)

        body = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_lawyer_id": str(lawyer.id)},
            headers=admin_headers,
        ).json()

        assert body["assigned_court_representative_id"] == str(representative.id)

    def test_a_representative_can_be_assigned_and_removed(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        representative: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        assigned = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_court_representative_id": str(representative.id)},
            headers=admin_headers,
        ).json()
        assert assigned["assigned_court_representative_id"] == str(representative.id)

        removed = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_court_representative_id": None},
            headers=admin_headers,
        ).json()
        assert removed["assigned_court_representative_id"] is None

    def test_the_assignee_role_is_validated(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_court_representative_id": str(lawyer.id)},
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_unknown_assignee_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_lawyer_id": str(uuid.uuid4())},
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_empty_assignment_body_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments", json={}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_assignment_grants_access_to_the_case(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer: User,
        lawyer_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        # The end-to-end point of assignment: it is what opens the case up.
        legal_case = make_case()
        assert (
            api_client.get(f"{CASES_URL}/{legal_case.id}", headers=lawyer_headers).status_code
            == status.HTTP_403_FORBIDDEN
        )

        api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_lawyer_id": str(lawyer.id)},
            headers=admin_headers,
        )

        assert (
            api_client.get(f"{CASES_URL}/{legal_case.id}", headers=lawyer_headers).status_code
            == status.HTTP_200_OK
        )

    def test_removing_an_assignment_withdraws_access(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer: User,
        lawyer_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer.id)

        api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_lawyer_id": None},
            headers=admin_headers,
        )

        assert (
            api_client.get(f"{CASES_URL}/{legal_case.id}", headers=lawyer_headers).status_code
            == status.HTTP_403_FORBIDDEN
        )


class TestArchiveCase:
    def test_archiving_returns_the_updated_case(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()

        response = api_client.delete(f"{CASES_URL}/{legal_case.id}", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "archived"
        assert response.json()["is_archived"] is True

    def test_an_archived_case_is_still_readable(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        # A soft delete: the row is kept, because documents and history reference
        # it.
        legal_case = make_case()
        api_client.delete(f"{CASES_URL}/{legal_case.id}", headers=admin_headers)

        assert (
            api_client.get(f"{CASES_URL}/{legal_case.id}", headers=admin_headers).status_code
            == status.HTTP_200_OK
        )

    def test_an_archived_case_is_still_searchable(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case(title="Shelved matter")
        api_client.delete(f"{CASES_URL}/{legal_case.id}", headers=admin_headers)

        body = api_client.get(
            CASES_URL, params={"search": "shelved"}, headers=admin_headers
        ).json()

        assert [item["id"] for item in body["items"]] == [str(legal_case.id)]

    def test_archiving_is_idempotent(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()
        api_client.delete(f"{CASES_URL}/{legal_case.id}", headers=admin_headers)

        response = api_client.delete(f"{CASES_URL}/{legal_case.id}", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK

    def test_an_archived_case_can_be_restored(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()
        api_client.delete(f"{CASES_URL}/{legal_case.id}", headers=admin_headers)

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}", json={"status": "open"}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "open"

    def test_an_unknown_id_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.delete(f"{CASES_URL}/{uuid.uuid4()}", headers=admin_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class TestListing:
    def test_the_page_carries_the_totals(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        for _ in range(3):
            make_case()

        body = api_client.get(
            CASES_URL, params={"page": 1, "page_size": 2}, headers=admin_headers
        ).json()

        assert body["total_records"] == 3
        assert body["page"] == 1
        assert body["page_size"] == 2
        assert body["total_pages"] == 2
        assert len(body["items"]) == 2

    @pytest.mark.parametrize("term", ["atlas", "ATLAS"])
    def test_search_is_case_insensitive(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase, term: str
    ) -> None:
        make_case(title="Benali v. Atlas")
        make_case(title="Unrelated")

        body = api_client.get(CASES_URL, params={"search": term}, headers=admin_headers).json()

        assert body["total_records"] == 1

    def test_a_wildcard_search_term_is_literal(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        make_case(title="Benali v. Atlas")

        body = api_client.get(CASES_URL, params={"search": "%"}, headers=admin_headers).json()

        assert body["total_records"] == 0

    def test_filters_combine(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        wanted = make_case(status=CaseStatus.OPEN, priority=CasePriority.URGENT)
        make_case(status=CaseStatus.OPEN, priority=CasePriority.LOW)
        make_case(status=CaseStatus.CLOSED, priority=CasePriority.URGENT)

        body = api_client.get(
            CASES_URL, params={"status": "open", "priority": "urgent"}, headers=admin_headers
        ).json()

        assert [item["id"] for item in body["items"]] == [str(wanted.id)]

    def test_a_date_range_filters_the_result(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        inside = make_case(filing_date=date(2026, 5, 15))
        make_case(filing_date=date(2026, 4, 15))

        body = api_client.get(
            CASES_URL,
            params={"filing_date_from": "2026-05-01", "filing_date_to": "2026-05-31"},
            headers=admin_headers,
        ).json()

        assert [item["id"] for item in body["items"]] == [str(inside.id)]

    def test_an_inverted_date_range_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            CASES_URL,
            params={"filing_date_from": "2026-06-01", "filing_date_to": "2026-05-01"},
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_priority_sorts_by_urgency(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        for priority in (CasePriority.MEDIUM, CasePriority.URGENT, CasePriority.LOW):
            make_case(priority=priority)

        body = api_client.get(
            CASES_URL,
            params={"sort_by": "priority", "sort_order": "desc"},
            headers=admin_headers,
        ).json()

        assert [item["priority"] for item in body["items"]] == ["urgent", "medium", "low"]

    def test_sorting_works_in_both_directions(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        for sequence in (3, 1, 2):
            make_case(case_number=f"CASE-2026-{sequence:04d}")

        ascending = api_client.get(
            CASES_URL,
            params={"sort_by": "case_number", "sort_order": "asc"},
            headers=admin_headers,
        ).json()
        descending = api_client.get(
            CASES_URL,
            params={"sort_by": "case_number", "sort_order": "desc"},
            headers=admin_headers,
        ).json()

        numbers = [item["case_number"] for item in ascending["items"]]
        assert numbers == sorted(numbers)
        assert [item["case_number"] for item in descending["items"]] == numbers[::-1]

    def test_pages_do_not_overlap(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        # Without the primary-key tiebreaker in the ORDER BY, rows sharing a sort
        # value could appear on both pages — or on neither.
        for _ in range(4):
            make_case(priority=CasePriority.MEDIUM)

        params = {"sort_by": "priority", "page_size": 2}
        first = api_client.get(CASES_URL, params={**params, "page": 1}, headers=admin_headers)
        second = api_client.get(CASES_URL, params={**params, "page": 2}, headers=admin_headers)

        ids = {item["id"] for item in first.json()["items"]}
        assert not ids & {item["id"] for item in second.json()["items"]}

    def test_an_unknown_sort_column_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            CASES_URL, params={"sort_by": "title"}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_a_page_size_beyond_the_ceiling_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            CASES_URL, params={"page_size": 1_000}, headers=admin_headers
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


class TestOpenApi:
    """Every endpoint must be documented, per the spec."""

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/api/v1/cases", "get"),
            ("/api/v1/cases", "post"),
            ("/api/v1/cases/{case_id}", "get"),
            ("/api/v1/cases/{case_id}", "patch"),
            ("/api/v1/cases/{case_id}", "delete"),
            ("/api/v1/cases/{case_id}/assignments", "patch"),
        ],
    )
    def test_each_endpoint_is_fully_documented(
        self, api_client: TestClient, path: str, method: str
    ) -> None:
        schema = api_client.get("/openapi.json").json()
        operation = schema["paths"][path][method]

        assert operation["summary"]
        assert operation["description"]
        assert operation["responses"]["401"]
        assert operation["responses"]["403"]

    def test_write_endpoints_declare_a_request_schema(self, api_client: TestClient) -> None:
        schema = api_client.get("/openapi.json").json()

        for path, method in (
            ("/api/v1/cases", "post"),
            ("/api/v1/cases/{case_id}", "patch"),
            ("/api/v1/cases/{case_id}/assignments", "patch"),
        ):
            assert schema["paths"][path][method]["requestBody"]["content"]["application/json"]
