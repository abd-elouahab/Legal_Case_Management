"""Integration tests for real-time synchronization.

Three things end to end, against the real application:

* **the socket** — connect, authenticate, subscribe, receive, reconnect — through
  Starlette's ``TestClient.websocket_connect``, which drives the actual endpoint,
  the actual protocol decoder, and the actual connection manager;
* **publishing** — that the business services announce what the spec says they
  announce, and nothing they should not;
* **the administrative surface** — the metrics and presence reads, and who may
  see them.

The dispatcher is replaced per test by a recording publisher (see the
``event_publisher`` fixture), so one test's uploads are never counted by
another's assertions.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from core.events import DomainEventType, case_topic, document_topic, user_topic
from core.realtime import (
    CLOSE_AUTH_TIMEOUT,
    CLOSE_POLICY_VIOLATION,
    CLOSE_UNAUTHENTICATED,
)
from models.user import UserRole
from tests.helpers import PDF_BYTES

PASSWORD = "correct-horse-battery"

LOGIN_URL = f"{settings.API_V1_PREFIX}/auth/login"
WS_URL = f"{settings.API_V1_PREFIX}/realtime/ws"
METRICS_URL = f"{settings.API_V1_PREFIX}/realtime/metrics"
PRESENCE_URL = f"{settings.API_V1_PREFIX}/realtime/presence"
STATUS_URL = f"{settings.API_V1_PREFIX}/realtime/status"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(client: TestClient, email: str, password: str = PASSWORD) -> str:
    response = client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    access_token: str = response.json()["access_token"]
    return access_token


@pytest.fixture
def admin_user(make_user: Any) -> Any:
    return make_user(email="admin@example.com", password=PASSWORD, role=UserRole.ADMINISTRATOR)


@pytest.fixture
def access_token(api_client: TestClient, admin_user: Any) -> str:
    return token_for(api_client, admin_user.email)


@pytest.fixture
def auth_headers(access_token: str) -> dict[str, str]:
    return bearer(access_token)


@pytest.fixture
def lawyer_token(api_client: TestClient, make_user: Any) -> str:
    make_user(email="lawyer@example.com", password=PASSWORD, role=UserRole.LAWYER)
    return token_for(api_client, "lawyer@example.com")


def _authenticate(socket: Any, token: str) -> dict[str, Any]:
    """Send the opening frame and return the ``ready`` body."""
    socket.send_text(json.dumps({"type": "authenticate", "token": token}))
    return json.loads(socket.receive_text())


def _drain_until(socket: Any, frame_type: str, *, limit: int = 10) -> dict[str, Any] | None:
    """Read frames until one of ``frame_type`` arrives.

    The channel interleaves keepalives and subscription echoes with events, so a
    test that read exactly one frame would be asserting about whichever happened
    to arrive first.
    """
    for _ in range(limit):
        body = json.loads(socket.receive_text())
        if body.get("type") == frame_type:
            return body
    return None


# --------------------------------------------------------------------------- #
# Handshake
# --------------------------------------------------------------------------- #


class TestAuthentication:
    def test_a_client_authenticates_with_its_first_frame(
        self, api_client: TestClient, access_token: str, admin_user: Any
    ) -> None:
        """The credential travels in a body, never in the URL."""
        with api_client.websocket_connect(WS_URL) as socket:
            ready = _authenticate(socket, access_token)

        assert ready["type"] == "ready"
        assert ready["user_id"] == str(admin_user.id)
        assert ready["role"] == admin_user.role.value
        assert "connection_id" in ready
        assert ready["heartbeat_seconds"] > 0

    def test_an_invalid_token_closes_the_socket(self, api_client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as caught, api_client.websocket_connect(WS_URL) as socket:
            socket.send_text(json.dumps({"type": "authenticate", "token": "not-a-token"}))
            while True:
                socket.receive_text()

        assert caught.value.code == CLOSE_UNAUTHENTICATED

    def test_any_other_frame_before_authentication_closes_the_socket(
        self, api_client: TestClient
    ) -> None:
        """An unauthenticated socket may do exactly one thing."""
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as caught, api_client.websocket_connect(WS_URL) as socket:
            socket.send_text(json.dumps({"type": "ping"}))
            while True:
                socket.receive_text()

        assert caught.value.code == CLOSE_POLICY_VIOLATION

    def test_a_malformed_opening_frame_closes_the_socket(
        self, api_client: TestClient
    ) -> None:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), api_client.websocket_connect(WS_URL) as socket:
            socket.send_text("not json at all")
            while True:
                socket.receive_text()

    def test_the_close_code_for_a_timeout_is_distinct(self) -> None:
        """Each 4xxx code means something a client acts on differently.

        A client that cannot tell "your token expired" from "you talk too much"
        retries both the same way, and one of those retries is a loop.
        """
        assert len({CLOSE_AUTH_TIMEOUT, CLOSE_UNAUTHENTICATED, CLOSE_POLICY_VIOLATION}) == 3


# --------------------------------------------------------------------------- #
# Subscriptions and delivery
# --------------------------------------------------------------------------- #


class TestSubscriptions:
    def test_an_authorized_case_is_granted(
        self, api_client: TestClient, access_token: str, make_case: Any
    ) -> None:
        legal_case = make_case()
        topic = case_topic(legal_case.id).key

        with api_client.websocket_connect(WS_URL) as socket:
            _authenticate(socket, access_token)
            socket.send_text(json.dumps({"type": "subscribe", "topics": [topic]}))
            body = _drain_until(socket, "subscriptions")

        assert body is not None
        assert body["granted"] == [topic]
        assert body["refused"] == []
        assert body["active"] == [topic]

    def test_an_unauthorized_case_is_refused_by_name(
        self, api_client: TestClient, lawyer_token: str, make_case: Any
    ) -> None:
        """A refused topic is reported; a *delivered* event never would be."""
        topic = case_topic(make_case().id).key

        with api_client.websocket_connect(WS_URL) as socket:
            _authenticate(socket, lawyer_token)
            socket.send_text(json.dumps({"type": "subscribe", "topics": [topic]}))
            body = _drain_until(socket, "subscriptions")

        assert body is not None
        assert body["granted"] == []
        assert body["refused"] == [topic]
        assert body["active"] == []

    def test_one_bad_topic_does_not_cost_the_good_ones(
        self, api_client: TestClient, access_token: str, make_case: Any
    ) -> None:
        good = case_topic(make_case().id).key

        with api_client.websocket_connect(WS_URL) as socket:
            _authenticate(socket, access_token)
            socket.send_text(
                json.dumps({"type": "subscribe", "topics": [good, "case:nonsense"]})
            )
            body = _drain_until(socket, "subscriptions")

        assert body is not None
        assert body["granted"] == [good]

    def test_unsubscribing_removes_the_topic(
        self, api_client: TestClient, access_token: str, make_case: Any
    ) -> None:
        topic = case_topic(make_case().id).key

        with api_client.websocket_connect(WS_URL) as socket:
            _authenticate(socket, access_token)
            socket.send_text(json.dumps({"type": "subscribe", "topics": [topic]}))
            _drain_until(socket, "subscriptions")
            socket.send_text(json.dumps({"type": "unsubscribe", "topics": [topic]}))
            body = _drain_until(socket, "subscriptions")

        assert body is not None
        assert body["active"] == []

    def test_a_user_may_always_follow_their_own_topic(
        self, api_client: TestClient, access_token: str, admin_user: Any
    ) -> None:
        topic = user_topic(admin_user.id).key

        with api_client.websocket_connect(WS_URL) as socket:
            _authenticate(socket, access_token)
            socket.send_text(json.dumps({"type": "subscribe", "topics": [topic]}))
            body = _drain_until(socket, "subscriptions")

        assert body is not None
        assert body["granted"] == [topic]

    def test_another_user_s_topic_is_refused(
        self, api_client: TestClient, access_token: str, make_user: Any
    ) -> None:
        other = make_user(email="other@example.com")
        topic = user_topic(other.id).key

        with api_client.websocket_connect(WS_URL) as socket:
            _authenticate(socket, access_token)
            socket.send_text(json.dumps({"type": "subscribe", "topics": [topic]}))
            body = _drain_until(socket, "subscriptions")

        assert body is not None
        assert body["refused"] == [topic]


class TestProtocolBehaviour:
    def test_ping_is_answered_with_the_current_sequence(
        self, api_client: TestClient, access_token: str
    ) -> None:
        with api_client.websocket_connect(WS_URL) as socket:
            _authenticate(socket, access_token)
            socket.send_text(json.dumps({"type": "ping"}))
            body = _drain_until(socket, "pong")

        assert body is not None
        assert isinstance(body["sequence"], int)

    def test_resume_reports_whether_anything_was_missed(
        self, api_client: TestClient, access_token: str
    ) -> None:
        """The server holds no history to replay, so a gap means *refetch*."""
        with api_client.websocket_connect(WS_URL) as socket:
            _authenticate(socket, access_token)
            socket.send_text(json.dumps({"type": "resume", "last_sequence": 1}))
            body = _drain_until(socket, "resumed")

        assert body is not None
        assert body["last_sequence"] == 1
        assert "gap" in body

    def test_re_authenticating_is_refused(
        self, api_client: TestClient, access_token: str
    ) -> None:
        """Swapping identity on a socket whose grants belong to the first one."""
        with api_client.websocket_connect(WS_URL) as socket:
            _authenticate(socket, access_token)
            socket.send_text(json.dumps({"type": "authenticate", "token": access_token}))
            body = _drain_until(socket, "error")

        assert body is not None
        assert body["error"] == "already_authenticated"

    def test_a_malformed_frame_does_not_close_an_authenticated_socket(
        self, api_client: TestClient, access_token: str
    ) -> None:
        """Once authenticated, a bad frame is a mistake rather than an attack."""
        with api_client.websocket_connect(WS_URL) as socket:
            _authenticate(socket, access_token)
            socket.send_text("{oops")
            error = _drain_until(socket, "error")
            socket.send_text(json.dumps({"type": "ping"}))
            pong = _drain_until(socket, "pong")

        assert error is not None
        assert error["error"] == "malformed_frame"
        assert pong is not None


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #


class TestCaseEvents:
    def test_creating_a_case_announces_it(
        self, api_client: TestClient, auth_headers: dict[str, str], event_publisher: Any
    ) -> None:
        response = api_client.post(
            f"{settings.API_V1_PREFIX}/cases",
            json={"title": "Benali v. Atlas", "status": "open", "priority": "medium"},
            headers=auth_headers,
        )
        assert response.status_code == 201

        assert DomainEventType.CASE_CREATED in event_publisher.types()

    def test_the_payload_carries_the_case_number_and_not_the_title(
        self, api_client: TestClient, auth_headers: dict[str, str], event_publisher: Any
    ) -> None:
        """A title names a client and a matter; a number identifies the record."""
        api_client.post(
            f"{settings.API_V1_PREFIX}/cases",
            json={"title": "Confidential merger dispute", "status": "open", "priority": "high"},
            headers=auth_headers,
        )

        created = next(
            event
            for event in event_publisher.events
            if event.event_type is DomainEventType.CASE_CREATED
        )
        assert "case_number" in created.payload
        assert "Confidential merger dispute" not in json.dumps(created.payload)

    def test_archiving_announces_an_archive_rather_than_a_status_change(
        self,
        api_client: TestClient,
        auth_headers: dict[str, str],
        make_case: Any,
        event_publisher: Any,
    ) -> None:
        """A client that had to infer "archived" from a status field would get it
        wrong the first time a status was added."""
        legal_case = make_case()
        event_publisher.reset()

        response = api_client.delete(f"{settings.API_V1_PREFIX}/cases/{legal_case.id}", headers=auth_headers)
        assert response.status_code in {200, 204}

        assert DomainEventType.CASE_ARCHIVED in event_publisher.types()

    def test_a_case_event_is_published_on_the_case_topic(
        self,
        api_client: TestClient,
        auth_headers: dict[str, str],
        make_case: Any,
        event_publisher: Any,
    ) -> None:
        legal_case = make_case()
        event_publisher.reset()

        api_client.patch(
            f"{settings.API_V1_PREFIX}/cases/{legal_case.id}",
            json={"title": "Renamed"},
            headers=auth_headers,
        )

        published = [
            event for event in event_publisher.events if event.topic == case_topic(legal_case.id)
        ]
        assert published


class TestDocumentEvents:
    def test_uploading_announces_the_document(
        self,
        api_client: TestClient,
        auth_headers: dict[str, str],
        make_case: Any,
        event_publisher: Any,
    ) -> None:
        legal_case = make_case()
        event_publisher.reset()

        response = api_client.post(
            f"{settings.API_V1_PREFIX}/documents/upload",
            files={"file": ("brief.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id), "category": "pleading"},
            headers=auth_headers,
        )
        assert response.status_code == 201

        uploaded = next(
            event
            for event in event_publisher.events
            if event.event_type is DomainEventType.DOCUMENT_UPLOADED
        )
        # On the document's topic, carrying the case so a case follower knows
        # which workspace is stale.
        assert uploaded.topic == document_topic(uuid.UUID(response.json()["id"]))
        assert uploaded.case_id == legal_case.id

    def test_no_filename_travels_on_the_wire(
        self,
        api_client: TestClient,
        auth_headers: dict[str, str],
        make_case: Any,
        event_publisher: Any,
    ) -> None:
        """A filename can name a client or a matter. The timeline carries it — to
        people already party to the case — and this channel does not."""
        legal_case = make_case()
        event_publisher.reset()

        api_client.post(
            f"{settings.API_V1_PREFIX}/documents/upload",
            files={"file": ("Benali-settlement-offer.pdf", PDF_BYTES, "application/pdf")},
            data={"case_id": str(legal_case.id), "category": "correspondence"},
            headers=auth_headers,
        )

        serialized = json.dumps([dict(event.payload) for event in event_publisher.events])
        assert "Benali-settlement-offer" not in serialized

    def test_downloading_announces_no_document_event(
        self,
        api_client: TestClient,
        auth_headers: dict[str, str],
        make_case: Any,
        make_document: Any,
        event_publisher: Any,
    ) -> None:
        """A download changes nothing *about the document*.

        `08-timeline.md` records who took a copy — that is accountability the
        case's participants read later — so a `timeline.updated` event is
        published and the activity feed refreshes. What is deliberately **not**
        published is a `document.*` event: it would tell every connected screen
        to refetch a list that has not changed, while broadcasting that a named
        colleague is reading a particular file right now. The spec asks for the
        opposite on both counts — *"avoid unnecessary broadcasts"* and *"deliver
        only relevant events"*.
        """
        document = make_document(case_id=make_case().id)
        event_publisher.reset()

        response = api_client.get(
            f"{settings.API_V1_PREFIX}/documents/{document.id}/download", headers=auth_headers
        )
        assert response.status_code == 200

        published = event_publisher.types()
        assert not [event for event in published if event.group == "document"]
        assert DomainEventType.TIMELINE_UPDATED in published


class TestTimelineEvents:
    def test_appending_to_a_case_history_announces_it(
        self,
        api_client: TestClient,
        auth_headers: dict[str, str],
        make_case: Any,
        event_publisher: Any,
    ) -> None:
        """One event that says "this case's history changed", rather than the
        client re-deriving which event types append an entry."""
        legal_case = make_case()
        event_publisher.reset()

        api_client.patch(
            f"{settings.API_V1_PREFIX}/cases/{legal_case.id}",
            json={"priority": "urgent"},
            headers=auth_headers,
        )

        assert DomainEventType.TIMELINE_UPDATED in event_publisher.types()

    def test_the_timeline_event_carries_no_description(
        self,
        api_client: TestClient,
        auth_headers: dict[str, str],
        make_case: Any,
        event_publisher: Any,
    ) -> None:
        """The description quotes filenames and names people."""
        legal_case = make_case()
        event_publisher.reset()

        api_client.patch(
            f"{settings.API_V1_PREFIX}/cases/{legal_case.id}", json={"priority": "low"}, headers=auth_headers
        )

        updated = next(
            event
            for event in event_publisher.events
            if event.event_type is DomainEventType.TIMELINE_UPDATED
        )
        assert set(updated.payload) == {"event_id", "timeline_event_type"}


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class TestStatus:
    def test_status_reports_the_deployment_s_configuration(
        self, api_client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        response = api_client.get(STATUS_URL, headers=auth_headers)
        assert response.status_code == 200

        body = response.json()
        assert body["enabled"] is True
        assert body["heartbeat_seconds"] > 0
        assert body["max_subscriptions"] > 0

    def test_status_requires_a_session(self, api_client: TestClient) -> None:
        assert api_client.get(STATUS_URL).status_code == 401


class TestMetrics:
    def test_an_administrator_reads_the_metrics(
        self, api_client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        response = api_client.get(METRICS_URL, headers=auth_headers)
        assert response.status_code == 200

        body = response.json()
        # The five figures the spec's Monitoring section names.
        for field in (
            "active_connections",
            "events_published",
            "average_delivery_latency_ms",
            "failed_deliveries",
            "reconnections",
        ):
            assert field in body

    def test_the_metrics_report_no_topic_case_or_user(
        self, api_client: TestClient, auth_headers: dict[str, str], make_case: Any
    ) -> None:
        """A per-topic breakdown would be a live index of which matters are being
        worked on."""
        legal_case = make_case()
        body = api_client.get(METRICS_URL, headers=auth_headers).json()

        serialized = json.dumps(body)
        assert str(legal_case.id) not in serialized
        assert "topic" not in json.dumps(body.get("events_by_type", {}))

    def test_a_lawyer_may_not_read_the_metrics(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        """`realtime:monitor` is administrative, like every other `*:monitor`."""
        response = api_client.get(METRICS_URL, headers=bearer(lawyer_token))
        assert response.status_code == 403

    def test_the_metrics_require_a_session(self, api_client: TestClient) -> None:
        assert api_client.get(METRICS_URL).status_code == 401


class TestPresence:
    def test_presence_is_readable_by_an_administrator(
        self, api_client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        response = api_client.get(PRESENCE_URL, headers=auth_headers)
        assert response.status_code == 200
        assert "items" in response.json()

    def test_a_presence_entry_never_says_what_is_followed(
        self, connection_manager: Any, make_user: Any
    ) -> None:
        """That would be a live index of who is working on which matter.

        Asserted against the manager rather than over HTTP, deliberately: the
        `TestClient` drives its socket and its requests through one portal, so a
        request issued while a socket is open on the same client deadlocks. What
        matters here is the *shape* of an entry, and the manager is what produces
        it.
        """
        from datetime import UTC, datetime

        from core.events import case_topic as topic_for_case
        from websocket.connection import ClientConnection, ConnectionIdentity

        user = make_user()
        legal_case_id = uuid.uuid4()
        connection = ClientConnection(
            ConnectionIdentity(
                user_id=user.id,
                role=user.role,
                session_generation=0,
                connected_at=datetime.now(UTC),
            ),
            queue_size=8,
            max_subscriptions=8,
            dedupe_window=8,
        )
        connection.grant(topic_for_case(legal_case_id))
        connection_manager.register(connection)

        entries = connection_manager.presence()

        assert [entry.user_id for entry in entries] == [user.id]
        assert entries[0].connections == 1
        # Counts and identity only — no topic, and therefore no case.
        assert str(legal_case_id) not in repr(entries)

    def test_a_lawyer_may_not_read_presence(
        self, api_client: TestClient, lawyer_token: str
    ) -> None:
        """Presence visualization is out of scope; this is an operator's view."""
        response = api_client.get(PRESENCE_URL, headers=bearer(lawyer_token))
        assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


class TestDependencyWiring:
    def test_the_application_wires_the_real_dispatcher_into_every_publisher(self) -> None:
        """The default publisher records nothing.

        If production ever took that default the whole feature would silently be
        a no-op while every unit test still passed — the same assertion the
        timeline makes about its own recorder.
        """
        from api.deps import get_event_publisher
        from services.events import EventDispatcher, get_event_dispatcher

        publisher = get_event_publisher()
        assert isinstance(publisher, EventDispatcher)
        assert publisher is get_event_dispatcher()

    def test_the_websocket_manager_is_the_dispatcher_s_subscriber(self) -> None:
        """Joined in the lifespan, and nowhere else.

        Asserted **inside a lifespan this test owns**, rather than against a
        shared client: registration is symmetric — startup subscribes and
        shutdown unsubscribes — so a test that looked at the registry outside one
        would be asserting about whichever lifespan happened to have exited last.
        Entering one here checks the thing that actually matters: that starting
        the application is what joins producers to consumers.
        """
        from main import app
        from services.events import get_event_dispatcher
        from websocket.manager import get_connection_manager

        manager = get_connection_manager()
        with TestClient(app):
            assert manager.name in get_event_dispatcher().subscriber_names

        # And that stopping it takes them apart again, so a second consumer
        # cannot be left holding a dead process's registration.
        assert manager.name not in get_event_dispatcher().subscriber_names

    def test_the_module_exposes_three_reads_and_nothing_that_writes(
        self, api_client: TestClient
    ) -> None:
        """The channel is read-only and its HTTP surface is administrative.

        Asserted against the OpenAPI schema rather than by walking `app.routes`,
        because this FastAPI version keeps included routers nested and a shallow
        walk would pass by finding nothing at all. The **socket** is absent from
        the schema by definition — WebSocket routes are not OpenAPI operations —
        and it is exercised by every other test in this file.

        A fourth path here, or any method other than GET, would mean somebody had
        added a way to influence the channel over HTTP, which is not what this
        module is for: every write on this platform goes through the feature's
        own authorized API.
        """
        from main import app

        realtime = {
            path: set(operations)
            for path, operations in app.openapi()["paths"].items()
            if path.startswith(f"{settings.API_V1_PREFIX}/realtime")
        }

        assert set(realtime) == {
            f"{settings.API_V1_PREFIX}/realtime/status",
            f"{settings.API_V1_PREFIX}/realtime/metrics",
            f"{settings.API_V1_PREFIX}/realtime/presence",
        }
        assert all(methods == {"get"} for methods in realtime.values())
