"""Unit tests for the WebSocket wire protocol (`websocket/protocol.py`).

The decoder is the only part of this feature that reads bytes an unauthenticated
client sent, so its rejections are load-bearing: everything downstream assumes a
frame that reaches it is well-formed, bounded, and carries parsed topics rather
than strings.
"""

from __future__ import annotations

import json
import uuid

import pytest

from core.events import DomainEvent, DomainEventType, case_topic, document_topic
from core.realtime import (
    MAX_CLIENT_FRAME_BYTES,
    MAX_TOPICS_PER_FRAME,
    ClientFrameType,
    RealtimeErrorCode,
    ServerFrameType,
)
from websocket.protocol import (
    FrameError,
    decode_client_frame,
    encode_error,
    encode_event,
    encode_pong,
    encode_ready,
    encode_resumed,
    encode_subscriptions,
)


def _body(frame: str) -> dict:
    return json.loads(frame)


class TestDecodingAuthenticate:
    def test_a_token_is_extracted_and_trimmed(self) -> None:
        command = decode_client_frame(json.dumps({"type": "authenticate", "token": " abc "}))
        assert command.type is ClientFrameType.AUTHENTICATE
        assert command.token == "abc"

    @pytest.mark.parametrize("token", ["", "   ", None, 42])
    def test_a_missing_or_blank_token_is_refused(self, token: object) -> None:
        with pytest.raises(FrameError) as caught:
            decode_client_frame(json.dumps({"type": "authenticate", "token": token}))
        assert caught.value.code is RealtimeErrorCode.INVALID_TOKEN


class TestDecodingSubscriptions:
    def test_topics_are_parsed_into_typed_values(self) -> None:
        """The endpoint never sees a raw string, so it cannot forget to validate one."""
        identifier = uuid.uuid4()
        command = decode_client_frame(
            json.dumps({"type": "subscribe", "topics": [f"case:{identifier}"]})
        )
        assert command.topics == (case_topic(identifier),)
        assert command.invalid_topics == ()

    def test_one_bad_topic_does_not_cost_a_client_the_others(self) -> None:
        """A single typo must not silently drop a whole subscribe frame."""
        good = uuid.uuid4()
        command = decode_client_frame(
            json.dumps({"type": "subscribe", "topics": [f"case:{good}", "case:nonsense"]})
        )
        assert command.topics == (case_topic(good),)
        assert command.invalid_topics == ("case:nonsense",)

    def test_duplicate_topics_are_collapsed(self) -> None:
        """So naming the same case twice costs one authorization lookup, not two."""
        identifier = uuid.uuid4()
        command = decode_client_frame(
            json.dumps(
                {"type": "subscribe", "topics": [f"case:{identifier}", f"case:{identifier}"]}
            )
        )
        assert len(command.topics) == 1

    def test_a_non_list_topics_field_is_refused(self) -> None:
        with pytest.raises(FrameError) as caught:
            decode_client_frame(json.dumps({"type": "subscribe", "topics": "case:x"}))
        assert caught.value.code is RealtimeErrorCode.MALFORMED_FRAME

    def test_too_many_topics_in_one_frame_are_refused(self) -> None:
        """Bounds the authorization work a single frame can demand."""
        topics = [f"case:{uuid.uuid4()}" for _ in range(MAX_TOPICS_PER_FRAME + 1)]
        with pytest.raises(FrameError) as caught:
            decode_client_frame(json.dumps({"type": "subscribe", "topics": topics}))
        assert caught.value.code is RealtimeErrorCode.TOO_MANY_SUBSCRIPTIONS


class TestDecodingOtherFrames:
    def test_ping_decodes(self) -> None:
        assert decode_client_frame('{"type": "ping"}').type is ClientFrameType.PING

    def test_resume_carries_a_sequence(self) -> None:
        command = decode_client_frame(json.dumps({"type": "resume", "last_sequence": 41}))
        assert command.last_sequence == 41

    def test_resume_without_a_sequence_is_accepted(self) -> None:
        """A first connection has nothing to resume from."""
        assert decode_client_frame('{"type": "resume"}').last_sequence is None

    @pytest.mark.parametrize("value", [-1, "41", True, 1.5])
    def test_an_invalid_sequence_is_refused(self, value: object) -> None:
        with pytest.raises(FrameError):
            decode_client_frame(json.dumps({"type": "resume", "last_sequence": value}))

    def test_there_is_no_mutating_frame(self) -> None:
        """The channel is read-only by design.

        A socket that could change state would be a second, thinner door onto the
        same business logic — so every write goes through the authorized REST API.
        """
        assert {frame.value for frame in ClientFrameType} == {
            "authenticate",
            "subscribe",
            "unsubscribe",
            "ping",
            "resume",
        }


class TestDecodingRejections:
    @pytest.mark.parametrize(
        "raw",
        ["not json", "[]", '"a string"', "123", '{"no": "type"}', '{"type": "explode"}'],
    )
    def test_a_malformed_frame_is_refused(self, raw: str) -> None:
        with pytest.raises(FrameError) as caught:
            decode_client_frame(raw)
        assert caught.value.code is RealtimeErrorCode.MALFORMED_FRAME

    def test_an_oversized_frame_is_refused_before_it_is_parsed(self) -> None:
        """So a socket cannot make the process buffer megabytes."""
        with pytest.raises(FrameError):
            decode_client_frame("x" * (MAX_CLIENT_FRAME_BYTES + 1))


class TestEncoding:
    def test_ready_carries_what_a_client_needs_to_resume_later(self) -> None:
        body = _body(
            encode_ready(
                connection_id="c1",
                user_id="u1",
                role="lawyer",
                sequence=41,
                heartbeat_seconds=25,
            )
        )
        assert body["type"] == ServerFrameType.READY.value
        assert body["sequence"] == 41
        # Published rather than assumed, so a client's liveness timer follows the
        # server's configuration instead of a hard-coded constant.
        assert body["heartbeat_seconds"] == 25

    def test_an_event_frame_carries_its_id_sequence_and_case(self) -> None:
        case_id = uuid.uuid4()
        event = DomainEvent.create(
            event_type=DomainEventType.OCR_COMPLETED,
            topic=document_topic(uuid.uuid4()),
            sequence=7,
            case_id=case_id,
            payload={"page_count": 3},
        )

        body = _body(encode_event(event))

        assert body["type"] == ServerFrameType.EVENT.value
        assert body["id"] == str(event.event_id)
        assert body["sequence"] == 7
        # Present even though the event is scoped to a document: it is what tells
        # a client which case workspace is now stale.
        assert body["case_id"] == str(case_id)
        assert body["payload"] == {"page_count": 3}

    def test_an_event_frame_does_not_re_filter_the_payload(self) -> None:
        """Filtering in two places is how the two come to disagree."""
        event = DomainEvent.create(
            event_type=DomainEventType.OCR_COMPLETED,
            topic=document_topic(uuid.uuid4()),
            sequence=1,
            payload={"page_count": 3, "text": "screened by the dispatcher"},
        )
        assert _body(encode_event(event))["payload"] == {"page_count": 3}

    def test_arabic_survives_encoding_unescaped(self) -> None:
        """A `\\uXXXX`-escaped Arabic title triples in size and is unreadable."""
        event = DomainEvent.create(
            event_type=DomainEventType.CASE_CREATED,
            topic=case_topic(uuid.uuid4()),
            sequence=1,
            payload={"case_number": "قضية-2026-0001"},
        )
        frame = encode_event(event)
        assert "قضية" in frame

    def test_subscriptions_echo_the_complete_active_set(self) -> None:
        """A client told only the delta cannot reconcile after a reconnect."""
        body = _body(
            encode_subscriptions(granted=["case:a"], refused=["case:b"], active=["case:a"])
        )
        assert body["granted"] == ["case:a"]
        assert body["refused"] == ["case:b"]
        assert body["active"] == ["case:a"]

    def test_resumed_reports_a_gap(self) -> None:
        body = _body(encode_resumed(last_sequence=10, current_sequence=15, gap=True))
        assert body["gap"] is True
        assert body["current_sequence"] == 15

    def test_pong_carries_the_sequence(self) -> None:
        """So an idle client can notice it has fallen behind from a heartbeat alone."""
        assert _body(encode_pong(sequence=12))["sequence"] == 12

    def test_an_error_frame_carries_a_code_and_a_human_sentence(self) -> None:
        body = _body(encode_error(RealtimeErrorCode.TOPIC_FORBIDDEN, topics=["case:x"]))
        assert body["error"] == "topic_forbidden"
        assert body["message"]
        assert body["topics"] == ["case:x"]

    def test_an_error_message_never_names_a_socket_or_a_token(self) -> None:
        """The string can reach a toast."""
        for code in RealtimeErrorCode:
            message = _body(encode_error(code))["message"].lower()
            assert "websocket" not in message
            assert "socket" not in message
            assert "token" not in message
