"""Unit tests for the domain-event vocabulary (`core/events.py`).

The rules here are the ones every other part of the feature is built on top of,
and each of them is a security or correctness property rather than a formatting
detail: what a topic *is*, which scope an event is authorized against, and — the
one this module cares most about — what may and may not travel in a payload.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from core.events import (
    CASE_FANOUT_SCOPES,
    EVENT_SCOPES,
    FORBIDDEN_PAYLOAD_KEYS,
    MAX_PAYLOAD_KEYS,
    MAX_PAYLOAD_VALUE_LENGTH,
    DomainEvent,
    DomainEventType,
    EventScope,
    EventTopic,
    InvalidEventPayloadError,
    InvalidTopicError,
    case_topic,
    document_topic,
    normalize_payload,
    report_topic,
    scope_for,
    screen_payload,
    user_topic,
)


class TestTopics:
    def test_a_topic_renders_as_scope_colon_id(self) -> None:
        identifier = uuid.uuid4()
        assert case_topic(identifier).key == f"case:{identifier}"

    def test_a_topic_round_trips_through_its_wire_form(self) -> None:
        original = document_topic(uuid.uuid4())
        assert EventTopic.parse(original.key) == original

    def test_topics_are_hashable_so_they_can_index_the_routing_table(self) -> None:
        identifier = uuid.uuid4()
        assert len({case_topic(identifier), case_topic(identifier)}) == 1

    def test_the_same_id_under_two_scopes_is_two_topics(self) -> None:
        """A document and a case that share an identifier must not share a channel.

        They cannot in practice — the ids are independent UUIDs — but the type
        must not *rely* on that, because the scope is what decides which
        authorization rule applies.
        """
        identifier = uuid.uuid4()
        assert case_topic(identifier) != document_topic(identifier)

    @pytest.mark.parametrize(
        "value",
        [
            "case",  # no separator
            "conversation:" + str(uuid.uuid4()),  # scope that does not exist
            "case:not-a-uuid",
            "",
            ":" + str(uuid.uuid4()),
        ],
    )
    def test_an_unparseable_topic_is_refused_rather_than_coerced(self, value: str) -> None:
        """A topic the server cannot name is a topic it cannot authorize."""
        with pytest.raises(InvalidTopicError):
            EventTopic.parse(value)

    def test_there_is_no_conversation_scope(self) -> None:
        """The strongest way to honour "never another user's conversations".

        The spec names conversations among the things a user must never receive
        another's events for. There is no channel that could carry one.
        """
        assert "conversation" not in {scope.value for scope in EventScope}


class TestEventScopes:
    def test_every_event_type_has_a_scope(self) -> None:
        """Exhaustive by test rather than by hope.

        A member added to the enum without an entry here would be an event
        nobody could make an authorization decision about — so the omission is
        caught now rather than in production, where it fails closed and silently
        stops being delivered.
        """
        missing = [event for event in DomainEventType if event not in EVENT_SCOPES]
        assert missing == []

    def test_report_events_are_scoped_to_the_report_not_the_case(self) -> None:
        """A report is its author's private work product."""
        for event in (
            DomainEventType.REPORT_STARTED,
            DomainEventType.REPORT_PROGRESS,
            DomainEventType.REPORT_GENERATED,
            DomainEventType.REPORT_FAILED,
        ):
            assert scope_for(event) is EventScope.REPORT

    def test_a_report_is_never_fanned_into_its_case(self) -> None:
        """The one exclusion that keeps private work product private."""
        assert EventScope.REPORT not in CASE_FANOUT_SCOPES
        assert EventScope.USER not in CASE_FANOUT_SCOPES

    def test_documents_are_fanned_into_their_case(self) -> None:
        """Because document access follows case access exactly."""
        assert EventScope.DOCUMENT in CASE_FANOUT_SCOPES
        assert EventScope.CASE in CASE_FANOUT_SCOPES


class TestPayloadScreening:
    @pytest.mark.parametrize("key", sorted(FORBIDDEN_PAYLOAD_KEYS))
    def test_every_forbidden_key_is_dropped(self, key: str) -> None:
        """A publisher's mistake must not become a confidentiality incident."""
        screened, dropped = screen_payload({key: "some confidential material", "id": 1})
        assert key not in screened
        assert dropped == [key]
        assert screened == {"id": 1}

    def test_forbidden_keys_are_matched_case_insensitively(self) -> None:
        screened, dropped = screen_payload({"Full_Text": "…", "TEXT": "…"})
        assert screened == {}
        assert sorted(dropped) == ["Full_Text", "TEXT"]

    def test_a_similar_but_permitted_key_survives(self) -> None:
        """Matching is exact, so ordinary fields are not swallowed.

        A substring rule would drop `file_extension` and `context_turns`, and a
        rule nobody can predict is a rule publishers work around.
        """
        screened, dropped = screen_payload(
            {"file_extension": "pdf", "context_turns": 2, "text_direction": "rtl"}
        )
        assert dropped == []
        assert screened == {"file_extension": "pdf", "context_turns": 2, "text_direction": "rtl"}

    def test_uuids_and_datetimes_are_rendered_the_way_the_api_renders_them(self) -> None:
        identifier = uuid.uuid4()
        moment = datetime(2026, 8, 8, 12, 30, tzinfo=UTC)
        payload = normalize_payload({"document_id": identifier, "at": moment})
        assert payload == {"document_id": str(identifier), "at": moment.isoformat()}

    def test_a_list_of_scalars_is_allowed(self) -> None:
        """How a "these fields changed" event says which fields."""
        assert normalize_payload({"fields": ["title", "court"]}) == {
            "fields": ["title", "court"]
        }

    def test_a_nested_object_is_refused(self) -> None:
        """A payload is a notification, not a row being replicated."""
        with pytest.raises(InvalidEventPayloadError):
            normalize_payload({"case": {"title": "…"}})

    def test_a_list_of_objects_is_refused(self) -> None:
        with pytest.raises(InvalidEventPayloadError):
            normalize_payload({"sources": [{"page": 1}]})

    def test_an_oversized_value_is_refused(self) -> None:
        """The line between a filename and a page of extracted text."""
        with pytest.raises(InvalidEventPayloadError):
            normalize_payload({"note": "x" * (MAX_PAYLOAD_VALUE_LENGTH + 1)})

    def test_too_many_keys_are_refused(self) -> None:
        with pytest.raises(InvalidEventPayloadError):
            normalize_payload({f"k{index}": index for index in range(MAX_PAYLOAD_KEYS + 1)})

    def test_an_empty_payload_normalizes_to_an_empty_object(self) -> None:
        assert normalize_payload(None) == {}
        assert normalize_payload({}) == {}


class TestDomainEvent:
    def test_creating_an_event_stamps_an_identity_and_a_time(self) -> None:
        event = DomainEvent.create(
            event_type=DomainEventType.CASE_CREATED,
            topic=case_topic(uuid.uuid4()),
            sequence=7,
        )
        assert isinstance(event.event_id, uuid.UUID)
        assert event.sequence == 7
        assert event.occurred_at.tzinfo is not None

    def test_two_events_never_share_an_identity(self) -> None:
        """The client deduplicates on this, so it has to be unique."""
        topic = case_topic(uuid.uuid4())
        first = DomainEvent.create(
            event_type=DomainEventType.CASE_UPDATED, topic=topic, sequence=1
        )
        second = DomainEvent.create(
            event_type=DomainEventType.CASE_UPDATED, topic=topic, sequence=2
        )
        assert first.event_id != second.event_id

    def test_an_event_screens_its_own_payload(self) -> None:
        """Screening is not something a call site can forget to do."""
        event = DomainEvent.create(
            event_type=DomainEventType.OCR_COMPLETED,
            topic=document_topic(uuid.uuid4()),
            sequence=1,
            payload={"page_count": 4, "text": "the whole document"},
        )
        assert event.payload == {"page_count": 4}

    def test_an_event_is_frozen(self) -> None:
        """A statement about the past that a subscriber could edit is not one."""
        event = DomainEvent.create(
            event_type=DomainEventType.CASE_CREATED,
            topic=case_topic(uuid.uuid4()),
            sequence=1,
        )
        with pytest.raises((AttributeError, TypeError)):
            event.sequence = 99  # type: ignore[misc]

    def test_the_scope_comes_from_the_topic(self) -> None:
        event = DomainEvent.create(
            event_type=DomainEventType.REPORT_PROGRESS,
            topic=report_topic(uuid.uuid4()),
            sequence=1,
        )
        assert event.scope is EventScope.REPORT

    def test_a_user_topic_names_the_user(self) -> None:
        identifier = uuid.uuid4()
        assert user_topic(identifier).scope is EventScope.USER
        assert user_topic(identifier).resource_id == identifier
