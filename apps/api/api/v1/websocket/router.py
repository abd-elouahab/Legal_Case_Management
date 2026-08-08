"""Real-time synchronization endpoints.

One WebSocket and two administrative reads. Like every other router on this
platform, it is deliberately thin: it owns the *socket* — accepting it, reading
frames off it, writing frames to it, and closing it — and delegates every
decision to the layers behind it. Authorization is
:mod:`services.realtime_access`'s, routing is
:class:`~websocket.manager.ConnectionManager`'s, and encoding is
:mod:`websocket.protocol`'s. No business logic lives here, and **nothing here
knows what a case or a document is**.

**The connection is two tasks, not one loop**, and that is the decision the rest
of the file follows from. A single loop that read a frame and then wrote pending
events would deliver an event only when the client happened to send something —
which for a channel whose entire purpose is unsolicited updates is no channel at
all. So a reader task consumes client frames and a writer task drains the
connection's queue, and the endpoint waits for whichever finishes first.

**Authentication is the first frame.** :mod:`core.realtime` records why at
length; the short version is that a browser cannot set an ``Authorization``
header on a WebSocket, and the two alternatives — a token in the URL, or a cookie
— would respectively write a bearer credential into three logs the application
does not control, and undo the CSRF properties the header scheme was chosen for.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from api.authorization import require_permission
from api.deps import (
    CurrentUser,
    get_login_throttle,
    get_session_factory,
    get_token_revocation_store,
)
from core.config import settings
from core.exceptions import AppException
from core.permissions import Permission
from core.realtime import (
    CLOSE_AUTH_TIMEOUT,
    CLOSE_FORBIDDEN,
    CLOSE_GOING_AWAY,
    CLOSE_OVERLOADED,
    CLOSE_POLICY_VIOLATION,
    CLOSE_TOO_MANY_CONNECTIONS,
    CLOSE_UNAUTHENTICATED,
    ClientFrameType,
    RealtimeErrorCode,
)
from models.user import User
from repositories.user import UserRepository
from schemas.events import PresenceListRead, PresenceRead, RealtimeMetricsRead
from services.auth import AuthService
from services.authorization import AuthorizationService
from services.event_metrics import get_realtime_metrics
from services.events import EventDispatcher, get_event_dispatcher
from services.login_throttle import LoginThrottle
from services.token_revocation import TokenRevocationStore
from websocket.connection import ClientConnection, identity_from_user
from websocket.manager import ConnectionManager, get_connection_manager
from websocket.protocol import (
    ClientCommand,
    FrameError,
    decode_client_frame,
    encode_error,
    encode_pong,
    encode_ready,
    encode_resumed,
    encode_subscriptions,
)

logger = structlog.get_logger(__name__)

#: Mounted under ``/realtime``.
router = APIRouter()


# --------------------------------------------------------------------------- #
# Authorized callers (HTTP surface)
# --------------------------------------------------------------------------- #

RealtimeMonitor = Annotated[User, Depends(require_permission(Permission.REALTIME_MONITOR))]

_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {"description": "Missing, invalid, or expired access token."}
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "description": "The account is disabled or lacks `realtime:monitor`."
    }
}


def get_manager() -> ConnectionManager:
    """Provide the process-wide connection manager.

    A dependency rather than a direct import at the call sites so an integration
    test can substitute one and exercise the endpoints without a running loop —
    the same reason the job queues and metrics recorders are dependencies.
    """
    return get_connection_manager()


ManagerDep = Annotated[ConnectionManager, Depends(get_manager)]


def get_dispatcher() -> EventDispatcher:
    """Provide the process-wide event dispatcher."""
    return get_event_dispatcher()


DispatcherDep = Annotated[EventDispatcher, Depends(get_dispatcher)]

#: A callable yielding a **context-managed** session.
#:
#: ``SessionLocal`` satisfies it directly — a SQLAlchemy ``Session`` is its own
#: context manager and closes on exit — and so does a test factory that yields a
#: session it owns and declines to close. That is what lets an integration test
#: drive the real socket against its own database.
SessionFactoryDep = Annotated[
    Callable[[], AbstractContextManager[Session]], Depends(get_session_factory)
]

#: The denylist and the throttle, injected rather than constructed.
#:
#: A socket authenticates with exactly the credentials an HTTP request does, so it
#: must consult exactly the same denylist — a revoked token that this door
#: accepted would be a way around ``/auth/logout``. Reached through the same
#: dependencies as :func:`api.deps.get_auth_service` so the two can never be
#: configured apart, and so an integration test substitutes one double for both
#: surfaces rather than two that could disagree.
RevocationsDep = Annotated[TokenRevocationStore, Depends(get_token_revocation_store)]
ThrottleDep = Annotated[LoginThrottle, Depends(get_login_throttle)]


# --------------------------------------------------------------------------- #
# The socket
# --------------------------------------------------------------------------- #


@router.websocket("/ws")
async def realtime_channel(
    websocket: WebSocket,
    manager: ManagerDep,
    dispatcher: DispatcherDep,
    sessions: SessionFactoryDep,
    revocations: RevocationsDep,
    throttle: ThrottleDep,
) -> None:
    """The platform's live update channel.

    **Protocol, in the order it happens.**

    1. The client connects. Nothing is delivered yet, and the only frame accepted
       is ``{"type": "authenticate", "token": "<access token>"}``. A connection
       that has not authenticated within ``REALTIME_AUTH_TIMEOUT_SECONDS`` is
       closed with **4001**.
    2. On success the server sends ``ready``, carrying the connection identifier,
       the server's current event sequence, and the heartbeat interval the client
       should expect. A bad credential closes with **4002** (refresh, then
       reconnect); an account without ``realtime:connect`` closes with **4003**,
       which is terminal.
    3. The client subscribes: ``{"type": "subscribe", "topics": ["case:<uuid>"]}``.
       Every topic is authorized **individually and per resource** — a topic the
       caller is not party to is refused by name, and the whole frame is not lost
       because one entry was wrong. The reply echoes the complete active set, so a
       client never has to reconstruct it.
    4. Events arrive as they happen. Each carries a stable `id` (deduplicate on
       it) and a monotonic `sequence` (a gap means events were missed).
    5. `ping` / `pong` in either direction. The server sends one every
       ``heartbeat_seconds``; a client that says nothing for
       ``REALTIME_IDLE_TIMEOUT_SECONDS`` is dropped.
    6. On reconnect the client may send ``{"type": "resume", "last_sequence": N}``.
       The server answers `resumed` with whether anything was missed. **It does
       not replay** — it stores no history, deliberately — so a gap means
       *refetch*, which is authoritative where a replay would only be a hint.

    **What a client may never do:** change anything. There is no mutating frame,
    by design. Every write on this platform goes through the authorized REST API,
    and a socket that could mutate state would be a second, thinner door onto the
    same business logic.
    """
    if not settings.REALTIME_ENABLED:
        # Accepted and immediately closed, rather than refused at the handshake:
        # a client that is told *why* shows "live updates are off" and stops,
        # where a handshake failure is indistinguishable from the server being
        # down and provokes a reconnect loop.
        await websocket.accept()
        await _close(websocket, CLOSE_GOING_AWAY, RealtimeErrorCode.FORBIDDEN)
        return

    await websocket.accept()

    authenticated = await _authenticate(websocket, sessions, revocations, throttle)
    if authenticated is None:
        return

    user, session_generation = authenticated
    connection = ClientConnection(
        identity_from_user(user, session_generation=session_generation),
        queue_size=settings.REALTIME_SEND_QUEUE_SIZE,
        max_subscriptions=settings.REALTIME_MAX_SUBSCRIPTIONS,
        dedupe_window=settings.REALTIME_DEDUPE_WINDOW,
    )

    try:
        manager.register(connection)
    except RuntimeError:
        get_realtime_metrics().record_connection_rejected(
            RealtimeErrorCode.TOO_MANY_SUBSCRIPTIONS
        )
        await _close(websocket, CLOSE_TOO_MANY_CONNECTIONS, RealtimeErrorCode.RATE_LIMITED)
        return

    await websocket.send_text(
        encode_ready(
            connection_id=str(connection.id),
            user_id=str(user.id),
            role=user.role.value,
            sequence=dispatcher.current_sequence,
            heartbeat_seconds=settings.REALTIME_HEARTBEAT_SECONDS,
        )
    )

    reader = asyncio.create_task(_read_frames(websocket, connection, manager, user, dispatcher))
    writer = asyncio.create_task(_write_frames(websocket, connection, dispatcher))

    try:
        done, pending = await asyncio.wait(
            {reader, writer}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Surface a task's exception rather than letting it vanish into a Future
        # nobody reads — a reader that died on a bug would otherwise look exactly
        # like a client that hung up.
        for task in done:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                task.result()
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - defensive
        logger.exception("realtime_connection_failed", **connection.log_fields)
    finally:
        manager.unregister(connection.id)
        with contextlib.suppress(Exception):
            await websocket.close(code=_close_code_for(connection.close_reason))


# --------------------------------------------------------------------------- #
# Handshake
# --------------------------------------------------------------------------- #


async def _authenticate(
    websocket: WebSocket,
    sessions: Callable[[], AbstractContextManager[Session]],
    revocations: TokenRevocationStore,
    throttle: LoginThrottle,
) -> tuple[User, int] | None:
    """Read and verify the opening ``authenticate`` frame.

    Returns the authenticated user and the session generation its token was
    minted under, or ``None`` when the socket has already been closed.

    Every failure closes rather than replying with an error and waiting for a
    better attempt: an unauthenticated socket that could keep trying is a socket
    an attacker can use to test tokens at whatever rate the frame budget allows,
    and the login throttle that guards passwords does not cover token
    presentation.
    """
    metrics = get_realtime_metrics()

    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=settings.REALTIME_AUTH_TIMEOUT_SECONDS
        )
    except TimeoutError:
        metrics.record_connection_rejected(RealtimeErrorCode.NOT_AUTHENTICATED)
        logger.info("realtime_auth_timeout")
        await _close(websocket, CLOSE_AUTH_TIMEOUT, RealtimeErrorCode.NOT_AUTHENTICATED)
        return None
    except (WebSocketDisconnect, RuntimeError):
        return None

    try:
        command = decode_client_frame(raw)
    except FrameError as exc:
        metrics.record_connection_rejected(exc.code)
        await _close(websocket, CLOSE_POLICY_VIOLATION, exc.code)
        return None

    if command.type is not ClientFrameType.AUTHENTICATE:
        metrics.record_connection_rejected(RealtimeErrorCode.NOT_AUTHENTICATED)
        await _close(websocket, CLOSE_POLICY_VIOLATION, RealtimeErrorCode.NOT_AUTHENTICATED)
        return None

    # `resolve_access_token` is synchronous and touches PostgreSQL and Redis, so
    # it runs in a worker thread. Doing it inline would stall every other
    # connection in the process for the length of two round trips.
    resolved = await run_in_threadpool(
        _resolve_token, sessions, revocations, throttle, command.token
    )

    if resolved is None:
        metrics.record_connection_rejected(RealtimeErrorCode.INVALID_TOKEN)
        await _close(websocket, CLOSE_UNAUTHENTICATED, RealtimeErrorCode.INVALID_TOKEN)
        return None

    user, session_generation = resolved

    if not AuthorizationService().has_permission(user, Permission.REALTIME_CONNECT):
        metrics.record_connection_rejected(RealtimeErrorCode.FORBIDDEN)
        logger.warning(
            "realtime_connection_denied", user_id=str(user.id), role=user.role.value
        )
        await _close(websocket, CLOSE_FORBIDDEN, RealtimeErrorCode.FORBIDDEN)
        return None

    metrics.record_connected(resumed=False)
    return user, session_generation


def _resolve_token(
    sessions: Callable[[], AbstractContextManager[Session]],
    revocations: TokenRevocationStore,
    throttle: LoginThrottle,
    token: str,
) -> tuple[User, int] | None:
    """Turn an access token into an active user, or ``None``.

    Runs on a worker thread with a session of its own. Every authentication
    failure — malformed, expired, revoked, wrong generation, disabled account —
    collapses to ``None`` on purpose: the socket tells the client only that its
    credential was not accepted, exactly as :class:`~core.exceptions.AuthenticationError`
    keeps every HTTP reason behind one generic 401.

    The user is **detached** from its session before returning: the session closes
    here, and a connection lives for hours, so anything read lazily off it later
    would raise. Only the snapshot in
    :class:`~websocket.connection.ConnectionIdentity` survives; every later
    decision re-reads the account (see :meth:`~services.realtime_access.RealtimeAccessPolicy.recheck`).
    """
    with sessions() as session:
        auth = AuthService(UserRepository(session), revocations, throttle)
        try:
            user, payload = auth.resolve_access_token(token)
        except AppException:
            return None
        session.expunge(user)
        return user, payload.session_generation


# --------------------------------------------------------------------------- #
# The two tasks
# --------------------------------------------------------------------------- #


async def _read_frames(
    websocket: WebSocket,
    connection: ClientConnection,
    manager: ConnectionManager,
    user: User,
    dispatcher: EventDispatcher,
) -> None:
    """Consume client frames until the socket closes or the client misbehaves."""
    while not connection.closed:
        raw = await websocket.receive_text()
        connection.touch()

        if not connection.within_rate_limit(
            max_frames_per_minute=settings.REALTIME_MAX_FRAMES_PER_MINUTE
        ):
            logger.warning("realtime_rate_limited", **connection.log_fields)
            connection.mark_closed(RealtimeErrorCode.RATE_LIMITED)
            await _send(websocket, encode_error(RealtimeErrorCode.RATE_LIMITED))
            return

        try:
            command = decode_client_frame(raw)
        except FrameError as exc:
            logger.info("realtime_frame_rejected", **connection.log_fields, reason=exc.code.value)
            await _send(websocket, encode_error(exc.code))
            continue

        await _apply(websocket, connection, manager, user, dispatcher, command)


async def _apply(
    websocket: WebSocket,
    connection: ClientConnection,
    manager: ConnectionManager,
    user: User,
    dispatcher: EventDispatcher,
    command: ClientCommand,
) -> None:
    """Act on one decoded client frame."""
    if command.type is ClientFrameType.AUTHENTICATE:
        # Already authenticated. Refused rather than honoured: re-authenticating
        # mid-connection would let a client swap identities on a socket whose
        # subscriptions were granted to the first one.
        await _send(websocket, encode_error(RealtimeErrorCode.ALREADY_AUTHENTICATED))
        return

    if command.type is ClientFrameType.PING:
        await _send(websocket, encode_pong(sequence=dispatcher.current_sequence))
        return

    if command.type is ClientFrameType.RESUME:
        current = dispatcher.current_sequence
        last = command.last_sequence or 0
        await _send(
            websocket,
            encode_resumed(
                last_sequence=last, current_sequence=current, gap=last > 0 and current > last
            ),
        )
        get_realtime_metrics().record_connected(resumed=True)
        return

    if command.invalid_topics:
        await _send(
            websocket,
            encode_error(RealtimeErrorCode.INVALID_TOPIC, topics=list(command.invalid_topics)),
        )

    if not command.topics:
        return

    if command.type is ClientFrameType.SUBSCRIBE:
        # Authorization is database work; it runs in a worker thread so one
        # client's subscription frame cannot stall every other connection.
        granted, refused = await run_in_threadpool(
            manager.subscribe, connection, list(command.topics), user=user
        )
        await _send(
            websocket,
            encode_subscriptions(
                granted=granted, refused=refused, active=connection.topic_keys
            ),
        )
        if refused:
            await _send(
                websocket, encode_error(RealtimeErrorCode.TOPIC_FORBIDDEN, topics=refused)
            )
        return

    removed = manager.unsubscribe(connection, list(command.topics))
    await _send(
        websocket,
        encode_subscriptions(granted=[], refused=removed, active=connection.topic_keys),
    )


async def _write_frames(
    websocket: WebSocket, connection: ClientConnection, dispatcher: EventDispatcher
) -> None:
    """Drain the connection's queue, emitting a keepalive when it goes quiet.

    The keepalive is what keeps an idle connection alive through the reverse
    proxies between the API and a browser, most of which close a socket that has
    carried nothing for sixty seconds. A user reading a case for ten minutes is
    exactly the situation this channel exists for, and it is also the situation in
    which nothing is published.
    """
    while not connection.closed:
        frame = await connection.next_frame(timeout=settings.REALTIME_HEARTBEAT_SECONDS)

        if frame is None:
            if connection.is_idle(timeout_seconds=settings.REALTIME_IDLE_TIMEOUT_SECONDS):
                logger.info(
                    "realtime_idle_timeout",
                    **connection.log_fields,
                    idle_seconds=round(connection.idle_seconds, 1),
                )
                connection.mark_closed()
                return
            await _send(websocket, encode_pong(sequence=dispatcher.current_sequence))
            continue

        await _send(websocket, frame)

    if connection.close_reason is not None:
        # The client fell behind or misbehaved. It is told which, because
        # "reconnect and refetch" and "stop sending so much" are different
        # instructions and a client that cannot tell them apart does neither.
        with contextlib.suppress(Exception):
            await websocket.send_text(encode_error(connection.close_reason))


# --------------------------------------------------------------------------- #
# Socket helpers
# --------------------------------------------------------------------------- #


async def _send(websocket: WebSocket, frame: str) -> None:
    """Write one frame, treating a dead socket as a disconnect rather than a fault."""
    try:
        await websocket.send_text(frame)
    except (WebSocketDisconnect, RuntimeError) as exc:  # pragma: no cover - transport
        raise WebSocketDisconnect(code=CLOSE_GOING_AWAY) from exc


async def _close(websocket: WebSocket, code: int, reason: RealtimeErrorCode) -> None:
    """Tell the client why, then close.

    The ``error`` frame is sent *before* the close because close reasons are
    unreliable in browsers — several report only the code — and a client that
    knows it was refused for a permission behaves very differently from one that
    assumes the server restarted.
    """
    with contextlib.suppress(Exception):
        await websocket.send_text(encode_error(reason))
    with contextlib.suppress(Exception):
        await websocket.close(code=code)


def _close_code_for(reason: RealtimeErrorCode | None) -> int:
    """The close code matching a connection's recorded reason."""
    if reason is RealtimeErrorCode.SLOW_CONSUMER or reason is RealtimeErrorCode.RATE_LIMITED:
        return CLOSE_OVERLOADED
    if reason is RealtimeErrorCode.FORBIDDEN:
        return CLOSE_FORBIDDEN
    if reason is RealtimeErrorCode.INVALID_TOKEN:
        return CLOSE_UNAUTHENTICATED
    return CLOSE_GOING_AWAY


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


@router.get(
    "/metrics",
    response_model=RealtimeMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="Real-time channel metrics",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_realtime_metrics_view(
    actor: RealtimeMonitor,
    manager: ManagerDep,
    dispatcher: DispatcherDep,
) -> RealtimeMetricsRead:
    """Return platform-wide real-time health.

    The five figures the spec's Monitoring section names — **active connections,
    event throughput, average delivery latency, failed deliveries, and reconnect
    count** — plus the rates, the per-type throughput breakdown, and the failures
    by cause.

    Two halves with different provenance, stated rather than hidden. **Active
    connections and presence are exact** for this API instance, and exact *is* the
    right word: a socket is held by exactly one process, so there is no aggregate
    to be missing. **The counters are in-process**, so they reset on restart and
    each instance counts only its own traffic — `since` is the honest caveat.

    An operational view, so it is gated on `realtime:monitor` and reports
    **counts, rates, and configuration only** — never a topic, a case, a document,
    a payload, or who was connected to what.
    """
    snapshot = manager.metrics_snapshot()

    return RealtimeMetricsRead(
        since=snapshot.since,
        enabled=settings.REALTIME_ENABLED,
        active_connections=snapshot.active_connections,
        present_users=snapshot.present_users,
        total_connections=snapshot.total_connections,
        total_disconnections=snapshot.total_disconnections,
        reconnections=snapshot.reconnections,
        rejected_connections=snapshot.rejected_connections,
        subscribed_topics=manager.topic_count,
        pending_dispatches=manager.pending_dispatches,
        events_published=snapshot.events_published,
        events_rejected=snapshot.events_rejected,
        events_delivered=snapshot.events_delivered,
        events_denied=snapshot.events_denied,
        events_deduplicated=snapshot.events_deduplicated,
        failed_deliveries=snapshot.failed_deliveries,
        average_delivery_latency_ms=snapshot.average_delivery_latency_ms,
        delivery_success_rate=snapshot.delivery_success_rate,
        average_fanout=snapshot.average_fanout,
        events_by_type=snapshot.events_by_type,
        failures_by_code=snapshot.failures_by_code,
        subscriber_failures=snapshot.subscriber_failures,
        subscribers=dispatcher.subscriber_names,
    )


@router.get(
    "/presence",
    response_model=PresenceListRead,
    status_code=status.HTTP_200_OK,
    summary="Who is currently connected",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_presence(actor: RealtimeMonitor, manager: ManagerDep) -> PresenceListRead:
    """Return the accounts holding at least one live connection on this instance.

    The tracking half of the spec's Presence section. **Visualization is
    explicitly out of scope**, so this is deliberately an administrative read
    rather than something the case workspace calls: online indicators, active
    viewers, and collaborative editing are the features this exists to make
    possible, and each of them is a product decision about what one user may learn
    about another.

    Gated on `realtime:monitor` for that reason. It reports an account, its role,
    how many connections it holds, and since when — **never** what any of them are
    subscribed to, which would be a live index of who is working on which matter.

    Scoped to **this API process**, necessarily: a connection is held by one
    process, so a multi-instance deployment reads presence per instance until a
    shared registry is introduced alongside the cross-instance event bridge.
    """
    entries = manager.presence()

    return PresenceListRead(
        items=[
            PresenceRead(
                user_id=entry.user_id,
                role=entry.role,
                connections=entry.connections,
                since=entry.since,
            )
            for entry in entries
        ],
        total=len(entries),
    )


@router.get(
    "/status",
    status_code=status.HTTP_200_OK,
    summary="Whether live updates are available",
    responses={**_UNAUTHORIZED},
)
def get_realtime_status(actor: CurrentUser, manager: ManagerDep) -> dict[str, object]:
    """Report whether this deployment offers live updates, and on what terms.

    Unauthenticated-adjacent by design — it needs no permission beyond a valid
    session, because **every** client needs the answer before it decides whether
    to open a socket or fall back to polling, and a client that had to open one to
    find out would defeat the purpose. It discloses only configuration: nothing
    about who is connected, what is being followed, or what has happened.

    **A disabled deployment answers 200 with ``enabled: false``**, never 503:
    "the feature is off" is precisely the answer being asked for, and a 503 would
    send a client into the reconnect loop this endpoint exists to prevent.
    """
    return {
        "enabled": settings.REALTIME_ENABLED,
        "heartbeat_seconds": settings.REALTIME_HEARTBEAT_SECONDS,
        "max_subscriptions": settings.REALTIME_MAX_SUBSCRIPTIONS,
        "connected": manager.connection_count > 0,
    }


__all__ = ["router"]
