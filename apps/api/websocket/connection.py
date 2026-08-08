"""One connected client.

A connection owns four things, and each of them is one of the spec's
requirements made into state that can be inspected and tested:

* **an outbound queue**, so publishing an event never waits on a socket. A
  publisher is inside a request or a worker job that has already earned its
  response; handing the event to a bounded queue and returning is what keeps
  ``EventDispatcher.publish`` O(subscribers) rather than O(clients x network);
* **its subscriptions, with the moment each was authorized**, which is how
  "authorization is enforced before every event is delivered" is honoured without
  a database query per event (see :mod:`services.realtime_access` for the bound
  that creates and why it is acceptable);
* **a window of recently delivered event ids**, which is the server's half of
  "duplicate events are avoided" — the client keeps the other half, because only
  it knows what survived a reconnect;
* **its liveness**, so a client that has silently gone away stops consuming a
  task and a queue.

**The queue is bounded and overflow closes the connection.** The alternatives are
worse: dropping the oldest event silently desynchronizes a client that believes
it is live, and an unbounded queue makes one client that stopped reading into the
API process's memory problem. Closing says plainly "you are behind, reconnect and
refetch", which is the only outcome that leaves the client *correct*.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from core.events import EventTopic
from core.realtime import RealtimeErrorCode
from models.user import User, UserRole

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ConnectionIdentity:
    """Who a connection belongs to.

    A **snapshot** taken at authentication, deliberately, and it holds no
    ``User``: an ORM instance detached from the session that loaded it is a
    stale-read waiting to happen, and a connection lives for hours. Everything a
    delivery decision needs about the *account* is re-resolved from the database
    at the moment it is needed; what is kept here is only what identifies the
    connection in a log.

    :attr:`session_generation` is kept for the same reason the token carries it:
    it is what makes "sign out everywhere" reach an already-open socket.
    """

    user_id: uuid.UUID
    role: UserRole
    session_generation: int
    #: When this connection authenticated. The presence timestamp.
    connected_at: datetime


@dataclass(slots=True)
class TopicGrant:
    """A subscription, and when it was last authorized.

    The timestamp is what makes the grant *expire*. Without it a subscription
    granted once would be honoured forever, and a lawyer removed from a case
    would keep receiving its events until they closed the tab.
    """

    topic: EventTopic
    #: ``time.monotonic()`` rather than wall-clock: this is measuring an elapsed
    #: interval, and a clock adjustment must not make a grant look fresh — or
    #: expire every grant in the process at once.
    authorized_at: float


class ClientConnection:
    """One authenticated WebSocket client.

    Deliberately **transport-agnostic**: it holds an :class:`asyncio.Queue` of
    encoded frames and never touches a socket. The endpoint owns the socket and
    drains the queue, which is what lets every rule here be tested without a
    network — a connection is exercised by pushing events in and reading strings
    out.
    """

    def __init__(
        self,
        identity: ConnectionIdentity,
        *,
        queue_size: int,
        max_subscriptions: int,
        dedupe_window: int,
        connection_id: uuid.UUID | None = None,
    ) -> None:
        self.id = connection_id or uuid.uuid4()
        self.identity = identity
        self._max_subscriptions = max(1, max_subscriptions)
        self._dedupe_window = max(0, dedupe_window)

        #: Encoded frames waiting to be written. Bounded — see the module
        #: docstring for why overflow is fatal rather than lossy.
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max(1, queue_size))

        self._grants: dict[EventTopic, TopicGrant] = {}
        #: Insertion-ordered so the oldest id is the one evicted. A `set` would
        #: have no oldest, and a `deque` alone would make the membership test
        #: linear on the hottest path in the module.
        self._delivered: OrderedDict[uuid.UUID, None] = OrderedDict()

        #: Frame timestamps inside the current rate-limit window.
        self._frame_times: deque[float] = deque()

        self._last_seen = time.monotonic()
        #: Highest sequence written towards this client. Reported on ``pong`` so
        #: an idle client can notice it has fallen behind.
        self.last_sequence = 0
        self.closed = False
        #: Set when the connection is closed for a reason the client should be
        #: told; read by the endpoint on its way out.
        self.close_reason: RealtimeErrorCode | None = None

    # ------------------------------------------------------------- identity #

    @property
    def user_id(self) -> uuid.UUID:
        """The account this connection belongs to."""
        return self.identity.user_id

    @property
    def log_fields(self) -> dict[str, str]:
        """Identifiers for a log line. Never a topic, never a payload."""
        return {
            "connection_id": str(self.id),
            "user_id": str(self.identity.user_id),
            "role": self.identity.role.value,
        }

    # -------------------------------------------------------- subscriptions #

    @property
    def topics(self) -> list[EventTopic]:
        """Every topic currently followed."""
        return list(self._grants)

    @property
    def topic_keys(self) -> list[str]:
        """Every topic currently followed, in wire form and sorted.

        Sorted because it is echoed to the client on every subscription change,
        and an unordered set would make two identical states look different.
        """
        return sorted(topic.key for topic in self._grants)

    @property
    def subscription_count(self) -> int:
        """How many topics this connection follows."""
        return len(self._grants)

    def has_capacity_for(self, count: int) -> bool:
        """Whether ``count`` more topics fit inside the per-connection ceiling."""
        return len(self._grants) + count <= self._max_subscriptions

    def grant(self, topic: EventTopic) -> None:
        """Record that ``topic`` was authorized for this connection, now."""
        self._grants[topic] = TopicGrant(topic=topic, authorized_at=time.monotonic())

    def revoke(self, topic: EventTopic) -> bool:
        """Stop following ``topic``. Returns whether it was being followed."""
        return self._grants.pop(topic, None) is not None

    def is_following(self, topic: EventTopic) -> bool:
        """Whether this connection has a grant for ``topic``, fresh or stale."""
        return topic in self._grants

    def grant_is_fresh(self, topic: EventTopic, *, ttl_seconds: float) -> bool:
        """Whether ``topic``'s grant is recent enough to deliver on without a re-check.

        ``ttl_seconds <= 0`` always answers ``False``, which is what makes
        ``REALTIME_AUTHORIZATION_TTL_SECONDS=0`` mean *re-authorize every single
        delivery* rather than *never expire* — the safe reading of a zero, and
        the one an operator setting it to zero intends.
        """
        grant = self._grants.get(topic)
        if grant is None:
            return False
        if ttl_seconds <= 0:
            return False
        return (time.monotonic() - grant.authorized_at) < ttl_seconds

    # ----------------------------------------------------------- deduplication #

    def should_deliver(self, event_id: uuid.UUID) -> bool:
        """Whether this event has not already been sent to this client.

        Records the id as delivered when the answer is ``True``, so the check and
        the bookkeeping cannot come apart — a caller that had to remember to mark
        it afterwards is a caller that will eventually forget on one branch.

        The window is bounded (:data:`~core.config.Settings.REALTIME_DEDUPE_WINDOW`)
        because a per-connection set that grew with traffic would be a leak that
        scales with how busy the platform is. A duplicate older than the window
        is delivered again — which is why the client keeps its own check, and why
        every event carries a stable id for it to use.
        """
        if self._dedupe_window <= 0:
            return True

        if event_id in self._delivered:
            return False

        self._delivered[event_id] = None
        while len(self._delivered) > self._dedupe_window:
            self._delivered.popitem(last=False)
        return True

    # ---------------------------------------------------------------- output #

    def enqueue(self, frame: str, sequence: int | None = None) -> bool:
        """Queue one encoded frame for writing.

        Non-blocking and **synchronous**, which is what lets the dispatcher call
        it from a request thread or a background worker without an event loop of
        its own: :meth:`asyncio.Queue.put_nowait` is safe to call from another
        thread only when the loop is not concurrently resizing the queue, which
        is why the manager marshals every call onto the loop first (see
        :meth:`~websocket.manager.ConnectionManager.handle`).

        Returns:
            ``True`` when the frame was queued. ``False`` when the connection is
            closed or its queue is full — in which case it is **marked as a slow
            consumer**, and the endpoint closes it. See the module docstring for
            why overflow is fatal rather than lossy.
        """
        if self.closed:
            return False

        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.close_reason = RealtimeErrorCode.SLOW_CONSUMER
            self.closed = True
            logger.warning("realtime_slow_consumer", **self.log_fields)
            return False

        if sequence is not None:
            self.last_sequence = max(self.last_sequence, sequence)
        return True

    async def next_frame(self, *, timeout: float | None = None) -> str | None:
        """Await the next frame to write, or ``None`` if none arrived in time.

        The timeout is what turns a blocking read into the heartbeat clock: the
        writer waits for work, and when none comes it is the server's cue to send
        a keepalive rather than sit silent until a proxy severs the connection.
        """
        if timeout is None:
            return await self._queue.get()

        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    @property
    def pending_frames(self) -> int:
        """How many frames are waiting to be written. For the monitoring view."""
        return self._queue.qsize()

    # -------------------------------------------------------------- liveness #

    def touch(self) -> None:
        """Record that the client is alive."""
        self._last_seen = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        """How long since anything was heard from this client."""
        return time.monotonic() - self._last_seen

    def is_idle(self, *, timeout_seconds: float) -> bool:
        """Whether the client has been silent past the tolerated window."""
        return self.idle_seconds > timeout_seconds

    def within_rate_limit(self, *, max_frames_per_minute: int) -> bool:
        """Whether one more inbound frame fits the connection's budget.

        A sliding window rather than a fixed one, because a fixed window lets a
        client send its whole minute's budget in the last instant of one window
        and again in the first instant of the next — twice the intended rate,
        which is the classic hole in the naive implementation.
        """
        now = time.monotonic()
        cutoff = now - 60.0
        while self._frame_times and self._frame_times[0] < cutoff:
            self._frame_times.popleft()

        if len(self._frame_times) >= max_frames_per_minute:
            return False

        self._frame_times.append(now)
        return True

    # ----------------------------------------------------------------- close #

    def mark_closed(self, reason: RealtimeErrorCode | None = None) -> None:
        """Stop accepting frames. Idempotent."""
        self.closed = True
        if reason is not None and self.close_reason is None:
            self.close_reason = reason


def identity_from_user(user: User, *, session_generation: int) -> ConnectionIdentity:
    """Snapshot what a connection needs to know about the authenticated user.

    A function rather than a constructor on the dataclass, so the dataclass stays
    free of any dependency on the ORM — it is a value the tests can build in one
    line without a database.
    """
    return ConnectionIdentity(
        user_id=user.id,
        role=user.role,
        session_generation=session_generation,
        connected_at=datetime.now(UTC),
    )


__all__ = [
    "ClientConnection",
    "ConnectionIdentity",
    "TopicGrant",
    "identity_from_user",
]
