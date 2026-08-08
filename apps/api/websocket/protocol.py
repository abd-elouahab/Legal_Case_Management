"""Frame encoding and decoding for the real-time channel.

Pure functions over strings and dataclasses: no socket, no user, no database, no
framework. That is what makes the protocol testable without either end of a
connection, and it is the same reason :mod:`core.rag` holds the pipeline's rules
rather than :mod:`services.rag`.

**The wire format is JSON text frames**, one object per frame, always carrying a
``type``. Binary frames are refused rather than interpreted: nothing this channel
carries is binary, and accepting both would mean two decoders where a client only
ever needs one.

``ensure_ascii=False`` throughout, for the reason
:mod:`api.v1.assistant.router` gives for its SSE frames: an Arabic case title
escaped into ``\\uXXXX`` triples in size and is unreadable in a network
inspector, and the transport is already UTF-8.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from core.events import DomainEvent, EventTopic, InvalidTopicError
from core.realtime import (
    MAX_CLIENT_FRAME_BYTES,
    MAX_TOPICS_PER_FRAME,
    ClientFrameType,
    RealtimeErrorCode,
    ServerFrameType,
    error_message,
)

# --------------------------------------------------------------------------- #
# Inbound
# --------------------------------------------------------------------------- #


class FrameError(ValueError):
    """A client frame could not be decoded, carrying the code to report.

    A code rather than a message, because the message is derived from it
    (:func:`~core.realtime.error_message`) and a decoder that wrote prose would
    be a second place for the client-facing wording to live.
    """

    def __init__(self, code: RealtimeErrorCode, detail: str) -> None:
        self.code = code
        #: For the log. Never sent — a decoder's complaint can quote the frame.
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ClientCommand:
    """One decoded client frame.

    A single type with optional fields rather than a class per command: there are
    five commands, each carrying at most two values, and five dataclasses plus a
    union would be more machinery than the thing it models. The endpoint branches
    on :attr:`type`, which is exhaustive over the enum.
    """

    type: ClientFrameType
    #: ``authenticate`` only.
    token: str = ""
    #: ``subscribe`` / ``unsubscribe`` only. Already parsed and de-duplicated, so
    #: the endpoint never sees a raw string and cannot forget to validate one.
    topics: tuple[EventTopic, ...] = ()
    #: ``resume`` only. The highest sequence the client already holds.
    last_sequence: int | None = None
    #: Topic strings that could not be parsed, kept so the client can be told
    #: *which* of the topics it asked for were rejected rather than having the
    #: whole frame refused. A single typo must not cost a client its other
    #: subscriptions.
    invalid_topics: tuple[str, ...] = field(default=())


def decode_client_frame(raw: str) -> ClientCommand:
    """Parse one client frame.

    Raises:
        FrameError: the frame is oversized, is not a JSON object, has no known
            ``type``, or is missing a field its type requires.
    """
    if len(raw.encode("utf-8")) > MAX_CLIENT_FRAME_BYTES:
        raise FrameError(
            RealtimeErrorCode.MALFORMED_FRAME,
            f"Frame exceeds {MAX_CLIENT_FRAME_BYTES} bytes.",
        )

    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise FrameError(RealtimeErrorCode.MALFORMED_FRAME, "Frame is not valid JSON.") from exc

    if not isinstance(decoded, dict):
        raise FrameError(RealtimeErrorCode.MALFORMED_FRAME, "Frame is not a JSON object.")

    raw_type = decoded.get("type")
    if not isinstance(raw_type, str):
        raise FrameError(RealtimeErrorCode.MALFORMED_FRAME, "Frame has no 'type'.")

    try:
        frame_type = ClientFrameType(raw_type)
    except ValueError as exc:
        raise FrameError(
            RealtimeErrorCode.MALFORMED_FRAME, f"Unknown frame type {raw_type!r}."
        ) from exc

    if frame_type is ClientFrameType.AUTHENTICATE:
        return _decode_authenticate(decoded)

    if frame_type in {ClientFrameType.SUBSCRIBE, ClientFrameType.UNSUBSCRIBE}:
        return _decode_subscription(frame_type, decoded)

    if frame_type is ClientFrameType.RESUME:
        return _decode_resume(decoded)

    return ClientCommand(type=ClientFrameType.PING)


def _decode_authenticate(decoded: dict[str, Any]) -> ClientCommand:
    token = decoded.get("token")
    if not isinstance(token, str) or not token.strip():
        raise FrameError(RealtimeErrorCode.INVALID_TOKEN, "authenticate carries no token.")
    return ClientCommand(type=ClientFrameType.AUTHENTICATE, token=token.strip())


def _decode_subscription(frame_type: ClientFrameType, decoded: dict[str, Any]) -> ClientCommand:
    raw_topics = decoded.get("topics")
    if not isinstance(raw_topics, list):
        raise FrameError(RealtimeErrorCode.MALFORMED_FRAME, "'topics' must be a list.")

    if len(raw_topics) > MAX_TOPICS_PER_FRAME:
        raise FrameError(
            RealtimeErrorCode.TOO_MANY_SUBSCRIPTIONS,
            f"At most {MAX_TOPICS_PER_FRAME} topics per frame.",
        )

    topics: list[EventTopic] = []
    invalid: list[str] = []
    seen: set[str] = set()

    for entry in raw_topics:
        if not isinstance(entry, str):
            invalid.append(repr(entry))
            continue
        if entry in seen:
            # De-duplicated here rather than on the connection, so a client that
            # names the same case twice pays for one authorization lookup.
            continue
        seen.add(entry)
        try:
            topics.append(EventTopic.parse(entry))
        except InvalidTopicError:
            invalid.append(entry)

    return ClientCommand(
        type=frame_type, topics=tuple(topics), invalid_topics=tuple(invalid)
    )


def _decode_resume(decoded: dict[str, Any]) -> ClientCommand:
    raw_sequence = decoded.get("last_sequence")
    if raw_sequence is None:
        return ClientCommand(type=ClientFrameType.RESUME)
    if not isinstance(raw_sequence, int) or isinstance(raw_sequence, bool) or raw_sequence < 0:
        raise FrameError(
            RealtimeErrorCode.MALFORMED_FRAME, "'last_sequence' must be a non-negative integer."
        )
    return ClientCommand(type=ClientFrameType.RESUME, last_sequence=raw_sequence)


# --------------------------------------------------------------------------- #
# Outbound
# --------------------------------------------------------------------------- #


def encode_frame(frame_type: ServerFrameType, body: dict[str, Any]) -> str:
    """Render one server frame."""
    return json.dumps({"type": frame_type.value, **body}, ensure_ascii=False)


def encode_ready(
    *,
    connection_id: str,
    user_id: str,
    role: str,
    sequence: int,
    heartbeat_seconds: int,
) -> str:
    """The frame that says the connection is authenticated and usable.

    Carries the server's **current sequence**, which is what makes a later
    ``resume`` meaningful: a client that reconnects knows both what it last saw
    and where the server is now, so it can tell "nothing happened while I was
    away" from "I missed eleven events and must refetch".

    ``heartbeat_seconds`` is published rather than assumed, so a client's own
    liveness timer is derived from the server's configuration instead of a
    hard-coded constant that would silently disagree after a deployment change.
    """
    return encode_frame(
        ServerFrameType.READY,
        {
            "connection_id": connection_id,
            "user_id": user_id,
            "role": role,
            "sequence": sequence,
            "heartbeat_seconds": heartbeat_seconds,
        },
    )


def encode_event(event: DomainEvent) -> str:
    """Render one domain event.

    The payload is already screened and normalized by the dispatcher, so nothing
    is filtered here — filtering in two places is how the two come to disagree.

    ``case_id`` travels beside the topic even for document and report events,
    because it is what tells a client *which case workspace is now stale*.
    Resolving that on the client would be one request per event, which is exactly
    the traffic this channel exists to remove.
    """
    return encode_frame(
        ServerFrameType.EVENT,
        {
            "id": str(event.event_id),
            "sequence": event.sequence,
            "event": event.event_type.value,
            "topic": event.topic.key,
            "scope": event.scope.value,
            "case_id": str(event.case_id) if event.case_id is not None else None,
            "actor_id": str(event.actor_id) if event.actor_id is not None else None,
            "occurred_at": event.occurred_at.isoformat(),
            "payload": dict(event.payload),
        },
    )


def encode_subscriptions(
    *,
    granted: list[str],
    refused: list[str],
    active: list[str],
) -> str:
    """The answer to a ``subscribe`` or ``unsubscribe``.

    All three lists, always. A client that learns only what was granted cannot
    tell a refusal from a lost frame, and one that learns only the delta cannot
    reconcile after a reconnect — so the **complete active set** is echoed and is
    authoritative.
    """
    return encode_frame(
        ServerFrameType.SUBSCRIPTIONS,
        {"granted": granted, "refused": refused, "active": active},
    )


def encode_resumed(*, last_sequence: int, current_sequence: int, gap: bool) -> str:
    """The answer to a ``resume``.

    ``gap`` is the whole point: it tells a client whether the events it missed
    matter. The server does **not** replay them — it holds no history, by design
    (see :class:`~services.events.EventDispatcher`) — so the honest answer to a
    gap is "refetch", which is authoritative where a replay would only be a hint.
    """
    return encode_frame(
        ServerFrameType.RESUMED,
        {"last_sequence": last_sequence, "current_sequence": current_sequence, "gap": gap},
    )


def encode_pong(*, sequence: int) -> str:
    """The answer to a ``ping``, and the server's own keepalive.

    Carries the current sequence so an idle client can detect, from a heartbeat
    alone, that it has fallen behind — without the server having to track what
    each client has seen.
    """
    return encode_frame(ServerFrameType.PONG, {"sequence": sequence})


def encode_error(code: RealtimeErrorCode, *, topics: list[str] | None = None) -> str:
    """A failure the connection survives.

    The message is derived from the code rather than passed in, so a client-facing
    sentence is never written at a call site. ``topics`` names *which*
    subscriptions a refusal applies to — which is safe, because the client just
    sent them.
    """
    body: dict[str, Any] = {"error": code.value, "message": error_message(code)}
    if topics:
        body["topics"] = topics
    return encode_frame(ServerFrameType.ERROR, body)


__all__ = [
    "ClientCommand",
    "FrameError",
    "decode_client_frame",
    "encode_error",
    "encode_event",
    "encode_frame",
    "encode_pong",
    "encode_ready",
    "encode_resumed",
    "encode_subscriptions",
]
