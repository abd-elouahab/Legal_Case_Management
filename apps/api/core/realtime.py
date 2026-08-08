"""The WebSocket wire protocol, defined once.

:mod:`core.events` says what a *domain event* is. This module says what a
*connection* is: the frames the two sides exchange, the codes a socket is closed
with, and the failure vocabulary the metrics view groups by. It is pure — no
sockets, no database, no framework — for the same reason :mod:`core.search` and
:mod:`core.rag` are: the rules are testable without either end of a connection.

**Authentication is the first frame, not the URL, and that is a privacy decision
rather than a stylistic one.** A browser's ``WebSocket`` constructor cannot set an
``Authorization`` header, which leaves three options, and only one of them is
consistent with what this platform has already decided elsewhere:

* a **query parameter** (``/ws?token=…``) writes an access token into the reverse
  proxy's access log, the browser's history, and any referrer the page emits next
  — the same three logs ``11-semantic-search.md`` made search a ``POST`` to stay
  out of, and a bearer token is a great deal more dangerous there than a query;
* a **cookie** would make the socket CSRF-reachable, undoing the header-based
  scheme ``03-authentication.md`` chose precisely to avoid that;
* an **authenticate frame** sent immediately after the handshake costs one round
  trip and puts the credential in a request body, where every other credential on
  this platform already travels.

So the socket is accepted unauthenticated, may send exactly one kind of frame,
and is closed if it has not authenticated within
:data:`~core.config.Settings.REALTIME_AUTH_TIMEOUT_SECONDS`. That window is the
entire attack surface the choice creates, and it is bounded by the server rather
than by the client's good behaviour.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

# --------------------------------------------------------------------------- #
# Frame kinds
# --------------------------------------------------------------------------- #


class ClientFrameType(StrEnum):
    """What a client may send.

    Deliberately five. A client of this channel *reads*; every write on this
    platform goes through the authorized REST API, and a socket that could
    mutate state would be a second, thinner door onto the same business logic.
    """

    #: ``{"type": "authenticate", "token": "<access token>"}`` — the first frame.
    AUTHENTICATE = "authenticate"
    #: ``{"type": "subscribe", "topics": ["case:<uuid>", …]}``
    SUBSCRIBE = "subscribe"
    #: ``{"type": "unsubscribe", "topics": [...]}``
    UNSUBSCRIBE = "unsubscribe"
    #: ``{"type": "ping"}`` — the client's own liveness check.
    PING = "ping"
    #: ``{"type": "resume", "last_sequence": 41}`` — declares what the client
    #: already has, so the server can tell it whether it missed anything.
    RESUME = "resume"


class ServerFrameType(StrEnum):
    """What the server sends."""

    #: Sent once, after a successful ``authenticate``. Carries the connection's
    #: identifier and the server's current sequence.
    READY = "ready"
    #: One domain event.
    EVENT = "event"
    #: The outcome of a ``subscribe`` / ``unsubscribe``, naming what was granted
    #: and what was refused. A client is never left guessing which of the topics
    #: it asked for it actually holds.
    SUBSCRIPTIONS = "subscriptions"
    #: The answer to ``ping``, and the server's own keepalive.
    PONG = "pong"
    #: A gap was detected against a resumed client's ``last_sequence``.
    RESUMED = "resumed"
    #: A protocol or authorization failure the connection survives.
    ERROR = "error"


# --------------------------------------------------------------------------- #
# Close codes
# --------------------------------------------------------------------------- #
#
# 1000 and 1008 are RFC 6455's own; 4000+ is the range the RFC reserves for the
# application, so each of ours says something a client can act on differently. A
# client that cannot tell "your token expired" from "you talk too much" retries
# both the same way, and one of those retries is a loop.

#: Normal shutdown — the server is stopping. Reconnect after a backoff.
CLOSE_GOING_AWAY: Final[int] = 1001
#: Policy violation: a malformed frame, or a frame sent out of order.
CLOSE_POLICY_VIOLATION: Final[int] = 1008
#: The connection never authenticated inside the handshake window.
CLOSE_AUTH_TIMEOUT: Final[int] = 4001
#: The credential was missing, malformed, expired, or revoked. **Refresh, then
#: reconnect** — reconnecting with the same token would fail identically.
CLOSE_UNAUTHENTICATED: Final[int] = 4002
#: Authenticated, but the account does not hold ``realtime:connect``. Terminal:
#: a client that retries this is retrying a policy decision.
CLOSE_FORBIDDEN: Final[int] = 4003
#: The client fell too far behind its own send queue, or exceeded the frame
#: budget. Reconnect and re-subscribe; the client must refetch rather than
#: assume continuity.
CLOSE_OVERLOADED: Final[int] = 4004
#: The platform already holds as many connections for this account as it allows.
CLOSE_TOO_MANY_CONNECTIONS: Final[int] = 4005


class RealtimeErrorCode(StrEnum):
    """Why a frame was refused, or a connection closed.

    Machine-readable and stable, exactly as :class:`~core.search.SearchFailureCode`
    and :class:`~core.rag.RagFailureCode` are, so the monitoring view can group
    failures by cause and a client can branch without parsing a sentence.
    """

    #: The frame was not JSON, or not an object with a known ``type``.
    MALFORMED_FRAME = "malformed_frame"
    #: A frame arrived before ``authenticate`` succeeded.
    NOT_AUTHENTICATED = "not_authenticated"
    #: ``authenticate`` was sent on a connection that is already authenticated.
    ALREADY_AUTHENTICATED = "already_authenticated"
    #: The access token was missing, malformed, expired, or revoked.
    INVALID_TOKEN = "invalid_token"
    #: The account is disabled, or lacks ``realtime:connect``.
    FORBIDDEN = "forbidden"
    #: A subscription string is not ``<scope>:<uuid>`` for a known scope.
    INVALID_TOPIC = "invalid_topic"
    #: The caller is not entitled to the resource a topic names.
    TOPIC_FORBIDDEN = "topic_forbidden"
    #: The connection asked for more topics than it may hold.
    TOO_MANY_SUBSCRIPTIONS = "too_many_subscriptions"
    #: The client sent frames faster than the connection's budget allows.
    RATE_LIMITED = "rate_limited"
    #: The connection's outbound queue overflowed — the client is not reading.
    SLOW_CONSUMER = "slow_consumer"
    #: Anything unforeseen. Logged with a traceback; never described to the client.
    INTERNAL = "internal"


#: Human sentences for the codes a client shows a user.
#:
#: Written for a **user**, not an operator: none of them names a socket, a queue,
#: or a token, because the string can reach a toast. Codes with no entry are
#: protocol faults a user can do nothing about and are reported by code alone.
ERROR_MESSAGES: dict[RealtimeErrorCode, str] = {
    RealtimeErrorCode.INVALID_TOKEN: "Your session has expired. Sign in again to continue.",
    RealtimeErrorCode.FORBIDDEN: "You do not have permission to receive live updates.",
    RealtimeErrorCode.TOPIC_FORBIDDEN: "You do not have access to that case or document.",
    RealtimeErrorCode.TOO_MANY_SUBSCRIPTIONS: (
        "This connection is following too many cases at once."
    ),
    RealtimeErrorCode.RATE_LIMITED: "Too many requests on this connection. Slowing down.",
    RealtimeErrorCode.SLOW_CONSUMER: "Live updates fell behind and were reset.",
}


def error_message(code: RealtimeErrorCode) -> str:
    """The sentence for ``code``, or a generic one.

    Generic rather than absent, because an ``error`` frame with no message forces
    every client to carry its own copy of this table — and the copy is what goes
    stale.
    """
    return ERROR_MESSAGES.get(code, "This request could not be completed.")


# --------------------------------------------------------------------------- #
# Protocol shape
# --------------------------------------------------------------------------- #

#: Largest client frame accepted, in bytes.
#:
#: A ``subscribe`` naming a hundred topics is roughly 4 KB; nothing a client may
#: legitimately send comes near this. It exists so a socket cannot be used to
#: make the process buffer megabytes before the frame is even parsed.
MAX_CLIENT_FRAME_BYTES: Final[int] = 16 * 1024

#: Topics named in one ``subscribe`` frame. Bounds the authorization work a
#: single frame can ask for, which is the expensive half — each unknown topic is
#: a database lookup.
MAX_TOPICS_PER_FRAME: Final[int] = 50


def connection_log_fields(
    *,
    connection_id: str,
    user_id: str | None = None,
    role: str | None = None,
) -> dict[str, str]:
    """The fields every connection log line carries.

    A helper rather than a convention, so that "identifiers only" is something the
    code does rather than something each call site remembers. **No topic, no
    payload, and no token** — a topic names a case, which is a client matter, and
    ``code-standards.md`` keeps client-confidential material out of application
    logs. Counts of topics are logged instead, which is what an operator actually
    reads.
    """
    fields = {"connection_id": connection_id}
    if user_id is not None:
        fields["user_id"] = user_id
    if role is not None:
        fields["role"] = role
    return fields


__all__ = [
    "CLOSE_AUTH_TIMEOUT",
    "CLOSE_FORBIDDEN",
    "CLOSE_GOING_AWAY",
    "CLOSE_OVERLOADED",
    "CLOSE_POLICY_VIOLATION",
    "CLOSE_TOO_MANY_CONNECTIONS",
    "CLOSE_UNAUTHENTICATED",
    "ERROR_MESSAGES",
    "MAX_CLIENT_FRAME_BYTES",
    "MAX_TOPICS_PER_FRAME",
    "ClientFrameType",
    "RealtimeErrorCode",
    "ServerFrameType",
    "connection_log_fields",
    "error_message",
]
