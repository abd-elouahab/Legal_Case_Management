"""Integration tests for the Timeline & Audit Trail API.

Exercise the two endpoints over real HTTP: the read contract, authorization (401
vs 403 for both routes and every role, plus the per-case assignment check),
search, filtering, sorting, and pagination.

The service-level rules are unit-tested in ``tests/unit/test_timeline_service.py``;
what these add is the wire contract — status codes, the ``metadata`` field name,
error envelopes — and the end-to-end proof that **the events exist at all**: every
case and document request below goes through the real dependency graph, so an
event only appears if the application actually wires the recorder in.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.config import settings
from models.case import Case
from models.timeline import TimelineEvent
from models.user import User, UserRole
from tests.helpers import PDF_BYTES

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
CASES_URL = f"{settings.API_V1_PREFIX}/cases"
DOCUMENTS_URL = f"{settings.API_V1_PREFIX}/documents"
TIMELINE_URL = f"{settings.API_V1_PREFIX}/timeline"

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]
MakeEvent = Callable[..., TimelineEvent]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == status.HTTP_200_OK, response.text
    access_token: str = response.json()["access_token"]
    return access_token


def case_timeline_url(case_id: uuid.UUID | str) -> str:
    return f"{CASES_URL}/{case_id}/timeline"


def types_of(payload: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in payload["items"]]


@pytest.fixture
def admin(make_user: MakeUser) -> User:
    return make_user(
        email="admin@example.com",
        password=PASSWORD,
        first_name="Amina",
        last_name="Benali",
        role=UserRole.ADMINISTRATOR,
    )


@pytest.fixture
def admin_headers(api_client: TestClient, admin: User) -> dict[str, str]:
    return bearer(token_for(api_client, admin.email))


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(
        email="lawyer@example.com",
        password=PASSWORD,
        first_name="Sarah",
        last_name="Smith",
        role=UserRole.LAWYER,
    )


@pytest.fixture
def lawyer_headers(api_client: TestClient, lawyer: User) -> dict[str, str]:
    return bearer(token_for(api_client, lawyer.email))


@pytest.fixture
def other_lawyer(make_user: MakeUser) -> User:
    return make_user(email="other@example.com", password=PASSWORD, role=UserRole.LAWYER)


@pytest.fixture
def other_lawyer_headers(api_client: TestClient, other_lawyer: User) -> dict[str, str]:
    return bearer(token_for(api_client, other_lawyer.email))


@pytest.fixture
def court(make_user: MakeUser) -> User:
    return make_user(
        email="court@example.com",
        password=PASSWORD,
        first_name="Karim",
        last_name="Ziani",
        role=UserRole.COURT_REPRESENTATIVE,
    )


@pytest.fixture
def court_headers(api_client: TestClient, court: User) -> dict[str, str]:
    return bearer(token_for(api_client, court.email))


class TestDependencyWiring:
    def test_the_application_wires_a_real_recorder_into_both_publishers(
        self, db_session: Any, document_storage: Any
    ) -> None:
        # `CaseService` and `DocumentService` default to a recorder that records
        # nothing, so they can be built in a context with no timeline — a script,
        # a unit test about something else. This asserts that the *application*
        # never takes that default, because if it did the whole feature would
        # silently be a no-op in production while every unit test still passed.
        from api.deps import get_case_service, get_document_service, get_ocr_service
        from repositories.case import CaseRepository
        from repositories.document import DocumentRepository
        from repositories.ocr import OcrRepository
        from repositories.timeline import TimelineRepository
        from repositories.user import UserRepository
        from services.ocr_engine import get_ocr_engine
        from services.ocr_queue import NullOcrJobQueue
        from services.timeline import TimelineService

        cases = CaseRepository(db_session)
        documents = DocumentRepository(db_session)
        timeline = TimelineService(TimelineRepository(db_session), cases)
        # OCR is a publisher too, and `get_document_service` now takes one — so
        # it is built the same way the application does, through its own factory.
        ocr = get_ocr_service(
            OcrRepository(db_session),
            documents,
            document_storage,
            get_ocr_engine(),
            NullOcrJobQueue(),
            timeline,
        )

        case_service = get_case_service(cases, UserRepository(db_session), timeline)
        document_service = get_document_service(
            documents, cases, document_storage, timeline, ocr
        )

        for service in (case_service, document_service, ocr):
            assert isinstance(service._timeline, TimelineService), type(service)


class TestAuthentication:
    def test_the_case_timeline_requires_a_token(
        self, api_client: TestClient, make_case: MakeCase
    ) -> None:
        response = api_client.get(case_timeline_url(make_case().id))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_a_single_event_requires_a_token(self, api_client: TestClient) -> None:
        response = api_client.get(f"{TIMELINE_URL}/{uuid.uuid4()}")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_a_malformed_token_is_401_not_403(
        self, api_client: TestClient, make_case: MakeCase
    ) -> None:
        response = api_client.get(
            case_timeline_url(make_case().id), headers=bearer("not-a-token")
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestAuthorization:
    def test_every_role_holds_the_timeline_capability(
        self,
        api_client: TestClient,
        lawyer: User,
        lawyer_headers: dict[str, str],
        court: User,
        court_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        # `timeline:view` is granted to all three roles; *which* timelines they
        # see is a per-case decision, not a capability one.
        for user, headers in ((lawyer, lawyer_headers), (court, court_headers)):
            field = (
                "assigned_lawyer_id"
                if user.role is UserRole.LAWYER
                else "assigned_court_representative_id"
            )
            legal_case = make_case(**{field: user.id})

            response = api_client.get(case_timeline_url(legal_case.id), headers=headers)
            assert response.status_code == status.HTTP_200_OK, response.text

    def test_an_unassigned_lawyer_is_refused_a_case_timeline(
        self,
        api_client: TestClient,
        lawyer: User,
        other_lawyer_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer.id)

        response = api_client.get(case_timeline_url(legal_case.id), headers=other_lawyer_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_the_denial_names_neither_permission_nor_role(
        self,
        api_client: TestClient,
        other_lawyer_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        response = api_client.get(
            case_timeline_url(make_case().id), headers=other_lawyer_headers
        )

        body = response.json()
        assert body["error"] == "forbidden"
        assert "timeline" not in body["message"].lower()
        assert "lawyer" not in body["message"].lower()

    def test_an_unassigned_lawyer_is_refused_a_single_event(
        self,
        api_client: TestClient,
        other_lawyer_headers: dict[str, str],
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        event = make_timeline_event(case_id=make_case().id)

        response = api_client.get(f"{TIMELINE_URL}/{event.id}", headers=other_lawyer_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_an_administrator_reads_any_case_s_timeline(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        response = api_client.get(case_timeline_url(make_case().id), headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK

    def test_an_unknown_case_is_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(case_timeline_url(uuid.uuid4()), headers=admin_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "case_not_found"

    def test_an_unknown_event_is_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{TIMELINE_URL}/{uuid.uuid4()}", headers=admin_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "timeline_event_not_found"

    def test_the_timeline_is_read_only(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        # There is no route through which a client can write, amend, or remove an
        # event — an audit trail a client can edit is not an audit trail.
        legal_case = make_case()

        for verb, url in (
            ("POST", case_timeline_url(legal_case.id)),
            ("POST", TIMELINE_URL),
            ("DELETE", f"{TIMELINE_URL}/{uuid.uuid4()}"),
            ("PATCH", f"{TIMELINE_URL}/{uuid.uuid4()}"),
            ("PUT", f"{TIMELINE_URL}/{uuid.uuid4()}"),
        ):
            response = api_client.request(verb, url, headers=admin_headers)
            # 405 where a GET route exists on that path, 404 where no route does
            # at all — either way, there is nothing to write to.
            assert response.status_code in {
                status.HTTP_404_NOT_FOUND,
                status.HTTP_405_METHOD_NOT_ALLOWED,
            }, url


class TestResponseShape:
    def test_it_returns_the_fields_the_spec_lists(
        self,
        api_client: TestClient,
        admin: User,
        admin_headers: dict[str, str],
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        legal_case = make_case()
        make_timeline_event(
            case_id=legal_case.id,
            actor=admin,
            description='Amina Benali uploaded "Contract.pdf".',
            metadata={"filename": "Contract.pdf"},
        )

        body = api_client.get(case_timeline_url(legal_case.id), headers=admin_headers).json()
        event = body["items"][0]

        assert set(event) >= {
            "id",
            "case_id",
            "event_type",
            "title",
            "description",
            "actor_id",
            "actor_name",
            "actor_role",
            "metadata",
            "created_at",
        }
        # The ORM attribute is `event_metadata`; the wire field must be `metadata`.
        assert event["metadata"] == {"filename": "Contract.pdf"}
        assert "event_metadata" not in event
        assert event["actor_name"] == "Amina Benali"
        assert event["actor_role"] == "administrator"

    def test_it_computes_the_category(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        from models.timeline import TimelineEventType

        legal_case = make_case()
        make_timeline_event(
            case_id=legal_case.id, event_type=TimelineEventType.LAWYER_ASSIGNED
        )

        body = api_client.get(case_timeline_url(legal_case.id), headers=admin_headers).json()

        assert body["items"][0]["category"] == "assignment"

    def test_the_page_carries_the_totals(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        legal_case = make_case()
        for _ in range(5):
            make_timeline_event(case_id=legal_case.id)

        body = api_client.get(
            case_timeline_url(legal_case.id), params={"page_size": 2}, headers=admin_headers
        ).json()

        assert body["total_records"] == 5
        assert body["page"] == 1
        assert body["page_size"] == 2
        assert body["total_pages"] == 3
        assert len(body["items"]) == 2

    def test_a_single_event_is_fetchable_by_id(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_case: MakeCase,
        make_timeline_event: MakeEvent,
    ) -> None:
        event = make_timeline_event(case_id=make_case().id, description="Something happened.")

        response = api_client.get(f"{TIMELINE_URL}/{event.id}", headers=admin_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["id"] == str(event.id)
        assert response.json()["description"] == "Something happened."


class TestQueryContract:
    @pytest.fixture(autouse=True)
    def _events(
        self, admin: User, lawyer: User, make_case: MakeCase, make_timeline_event: MakeEvent
    ) -> None:
        from datetime import UTC, datetime

        from models.timeline import TimelineEventType

        self.legal_case = make_case()
        make_timeline_event(
            case_id=self.legal_case.id,
            event_type=TimelineEventType.CASE_CREATED,
            actor=admin,
            description="Amina Benali created case CASE-2026-0001.",
            created_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        )
        make_timeline_event(
            case_id=self.legal_case.id,
            event_type=TimelineEventType.DOCUMENT_UPLOADED,
            actor=lawyer,
            description='Sarah Smith uploaded "Contract.pdf".',
            created_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
        )
        make_timeline_event(
            case_id=self.legal_case.id,
            event_type=TimelineEventType.STATUS_CHANGED,
            actor=admin,
            description="Amina Benali changed the status from Open to In progress.",
            created_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        )

    def test_it_defaults_to_newest_first(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(
            case_timeline_url(self.legal_case.id), headers=admin_headers
        ).json()

        assert types_of(body) == ["status_changed", "document_uploaded", "case_created"]

    def test_ascending_order_is_supported(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(
            case_timeline_url(self.legal_case.id),
            params={"sort_order": "asc"},
            headers=admin_headers,
        ).json()

        assert types_of(body) == ["case_created", "document_uploaded", "status_changed"]

    def test_search_matches_the_description_case_insensitively(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(
            case_timeline_url(self.legal_case.id),
            params={"search": "CONTRACT.pdf"},
            headers=admin_headers,
        ).json()

        assert types_of(body) == ["document_uploaded"]

    def test_search_matches_the_title(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(
            case_timeline_url(self.legal_case.id),
            params={"search": "Case Created"},
            headers=admin_headers,
        ).json()

        assert types_of(body) == ["case_created"]

    def test_it_filters_by_event_type(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(
            case_timeline_url(self.legal_case.id),
            params={"event_type": "status_changed"},
            headers=admin_headers,
        ).json()

        assert body["total_records"] == 1

    def test_it_filters_by_actor(
        self, api_client: TestClient, admin_headers: dict[str, str], lawyer: User
    ) -> None:
        body = api_client.get(
            case_timeline_url(self.legal_case.id),
            params={"actor_id": str(lawyer.id)},
            headers=admin_headers,
        ).json()

        assert types_of(body) == ["document_uploaded"]

    def test_it_filters_by_date_range(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        body = api_client.get(
            case_timeline_url(self.legal_case.id),
            params={"date_from": "2026-07-05", "date_to": "2026-07-15"},
            headers=admin_headers,
        ).json()

        assert types_of(body) == ["document_uploaded"]

    def test_filters_combine(
        self, api_client: TestClient, admin_headers: dict[str, str], admin: User
    ) -> None:
        body = api_client.get(
            case_timeline_url(self.legal_case.id),
            params={"actor_id": str(admin.id), "event_type": "case_created"},
            headers=admin_headers,
        ).json()

        assert body["total_records"] == 1

    def test_pages_do_not_overlap(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        seen: list[str] = []
        for page in (1, 2, 3):
            body = api_client.get(
                case_timeline_url(self.legal_case.id),
                params={"page": page, "page_size": 1},
                headers=admin_headers,
            ).json()
            seen.extend(event["id"] for event in body["items"])

        assert len(set(seen)) == 3

    def test_an_inverted_date_range_is_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            case_timeline_url(self.legal_case.id),
            params={"date_from": "2026-07-31", "date_to": "2026-07-01"},
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_unknown_query_parameter_is_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            case_timeline_url(self.legal_case.id),
            params={"case_id": str(uuid.uuid4())},
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_an_oversized_page_is_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(
            case_timeline_url(self.legal_case.id),
            params={"page_size": 500},
            headers=admin_headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_a_case_with_no_history_returns_an_empty_page(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        body = api_client.get(case_timeline_url(make_case().id), headers=admin_headers).json()

        assert body["items"] == []
        assert body["total_records"] == 0
        # Never "page 1 of 0".
        assert body["total_pages"] == 1


class TestEventsAreGeneratedAutomatically:
    """The spec's central requirement, proved through the real dependency graph."""

    def _timeline(
        self, api_client: TestClient, case_id: str, headers: dict[str, str]
    ) -> list[str]:
        response = api_client.get(
            f"{CASES_URL}/{case_id}/timeline",
            params={"sort_order": "asc", "page_size": 100},
            headers=headers,
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        return types_of(response.json())

    def test_creating_a_case_appears_on_its_timeline(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        created = api_client.post(
            CASES_URL, json={"title": "Benali v. Atlas"}, headers=admin_headers
        )
        assert created.status_code == status.HTTP_201_CREATED, created.text

        assert self._timeline(api_client, created.json()["id"], admin_headers) == [
            "case_created"
        ]

    def test_updating_a_case_appears_on_its_timeline(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}",
            json={"title": "Benali v. Atlas SARL"},
            headers=admin_headers,
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        assert self._timeline(api_client, str(legal_case.id), admin_headers) == ["case_updated"]

    def test_a_status_change_appears_on_its_timeline(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        from models.case import CaseStatus

        legal_case = make_case(status=CaseStatus.OPEN)

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}", json={"status": "in_progress"}, headers=admin_headers
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        assert self._timeline(api_client, str(legal_case.id), admin_headers) == [
            "status_changed"
        ]

    def test_assigning_a_lawyer_appears_on_its_timeline(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_lawyer_id": str(lawyer.id)},
            headers=admin_headers,
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        assert self._timeline(api_client, str(legal_case.id), admin_headers) == [
            "lawyer_assigned"
        ]

    def test_assigning_a_representative_appears_on_its_timeline(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        court: User,
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case()

        response = api_client.patch(
            f"{CASES_URL}/{legal_case.id}/assignments",
            json={"assigned_court_representative_id": str(court.id)},
            headers=admin_headers,
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        assert self._timeline(api_client, str(legal_case.id), admin_headers) == [
            "representative_assigned"
        ]

    def test_archiving_a_case_appears_on_its_timeline(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()

        response = api_client.delete(f"{CASES_URL}/{legal_case.id}", headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK, response.text

        assert self._timeline(api_client, str(legal_case.id), admin_headers) == [
            "case_archived"
        ]

    def test_the_document_lifecycle_appears_on_the_case_s_timeline(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()

        uploaded = api_client.post(
            f"{DOCUMENTS_URL}/upload",
            data={"case_id": str(legal_case.id), "category": "contract"},
            files={"file": ("Contract.pdf", PDF_BYTES, "application/pdf")},
            headers=admin_headers,
        )
        assert uploaded.status_code == status.HTTP_201_CREATED, uploaded.text
        document_id = uploaded.json()["id"]

        replaced = api_client.post(
            f"{DOCUMENTS_URL}/{document_id}/replace",
            files={"file": ("Contract-v2.pdf", PDF_BYTES, "application/pdf")},
            headers=admin_headers,
        )
        assert replaced.status_code == status.HTTP_200_OK, replaced.text

        deleted = api_client.delete(f"{DOCUMENTS_URL}/{document_id}", headers=admin_headers)
        assert deleted.status_code == status.HTTP_200_OK, deleted.text

        # Filtered to the document module's own events. Uploading a PDF also
        # schedules text extraction, which publishes `ocr_started` and
        # `ocr_completed` between them — a *different* module's contribution to
        # the same history, asserted in `tests/integration/test_ocr.py`. Pinning
        # the whole sequence here would make this test fail whenever another
        # module correctly joined the timeline.
        recorded = self._timeline(api_client, str(legal_case.id), admin_headers)

        assert [event for event in recorded if event.startswith("document_")] == [
            "document_uploaded",
            "document_replaced",
            "document_deleted",
        ]

    def test_a_document_event_carries_the_filename(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        legal_case = make_case()
        api_client.post(
            f"{DOCUMENTS_URL}/upload",
            data={"case_id": str(legal_case.id)},
            files={"file": ("Contract.pdf", PDF_BYTES, "application/pdf")},
            headers=admin_headers,
        )

        # Addressed by type rather than by position: the upload also schedules
        # text extraction, whose events are newer and therefore first under the
        # default descending order.
        body = api_client.get(
            case_timeline_url(legal_case.id),
            params={"event_type": "document_uploaded"},
            headers=admin_headers,
        ).json()
        event = body["items"][0]

        assert event["description"] == 'Amina Benali uploaded "Contract.pdf".'
        assert event["metadata"]["filename"] == "Contract.pdf"

    def test_an_assigned_lawyer_sees_the_history_of_their_own_case(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        lawyer: User,
        lawyer_headers: dict[str, str],
        make_case: MakeCase,
    ) -> None:
        legal_case = make_case(assigned_lawyer_id=lawyer.id)
        api_client.patch(
            f"{CASES_URL}/{legal_case.id}", json={"priority": "urgent"}, headers=admin_headers
        )

        assert self._timeline(api_client, str(legal_case.id), lawyer_headers) == [
            "priority_changed"
        ]

    def test_a_failed_request_records_nothing(
        self, api_client: TestClient, admin_headers: dict[str, str], make_case: MakeCase
    ) -> None:
        from models.case import CaseStatus

        # An illegal transition is refused, so there is nothing that happened for
        # the timeline to record.
        legal_case = make_case(status=CaseStatus.CLOSED)

        refused = api_client.patch(
            f"{CASES_URL}/{legal_case.id}", json={"status": "draft"}, headers=admin_headers
        )
        assert refused.status_code == status.HTTP_409_CONFLICT

        assert self._timeline(api_client, str(legal_case.id), admin_headers) == []


class TestOpenApi:
    def test_both_endpoints_are_documented(self, api_client: TestClient) -> None:
        schema = api_client.get("/openapi.json").json()

        case_path = schema["paths"][f"{settings.API_V1_PREFIX}/cases/{{case_id}}/timeline"]["get"]
        event_path = schema["paths"][f"{settings.API_V1_PREFIX}/timeline/{{event_id}}"]["get"]

        for operation in (case_path, event_path):
            assert operation["summary"]
            assert operation["description"]
            assert "200" in operation["responses"]
            assert "401" in operation["responses"]
            assert "403" in operation["responses"]
            assert "404" in operation["responses"]
            assert operation["tags"] == ["timeline"]
