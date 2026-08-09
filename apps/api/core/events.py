"""The platform's domain-event vocabulary.

``15-real-time-synchronization.md`` asks for *"strongly typed events"* and
explicitly forbids *"generic unstructured payloads"*. This module is where that
type lives, and — like :mod:`core.permissions` and :mod:`models.timeline` — it is
the **single place** an event is named. A publisher imports a
:class:`DomainEventType` member rather than writing a string, so a typo is a
static error rather than an event nobody subscribed to.

Nothing here performs I/O, holds a connection, or knows what a WebSocket is. The
module is pure vocabulary plus the two derivations everything downstream depends
on:

* **What an event is about** — its :class:`EventScope` and the resource it names,
  together an :class:`EventTopic`. That pair, and nothing else, is what the
  authorization layer decides on: an event about a case is deliverable to the
  people party to that case, and the rule is looked up rather than restated per
  event type. A new event type is one member below plus one line in
  :data:`EVENT_SCOPES`.
* **What may travel in a payload** — :func:`normalize_payload`, which bounds the
  size, forces the value JSON-safe, and **strips the keys that carry document
  contents**. The spec's payload rule ("avoid sensitive information, confidential
  document contents; events should remain lightweight") is enforced here rather
  than trusted to each publisher, because a publisher that forgets is a
  confidentiality incident and not a bug report.

**Why the topic is derived rather than supplied by the client.** The spec is
explicit: *"Never trust client-provided subscription information."* A client
names a topic when it subscribes and the server authorizes *that*; an event's own
topic is computed by its publisher from the row it just wrote. The two meet in
the routing table, so a client can only ever receive events on a topic it was
granted — there is no path by which a subscription string reaches a delivery
decision unchecked.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

# --------------------------------------------------------------------------- #
# Scopes and topics
# --------------------------------------------------------------------------- #


class EventScope(StrEnum):
    """The kind of resource an event is about, which decides who may receive it.

    Four, and each maps onto an authorization rule the platform already has:

    * :attr:`CASE` — party to the case (``services.case_access``);
    * :attr:`DOCUMENT` — party to the document's case, which is the same rule one
      hop further out (``services.document_access``);
    * :attr:`REPORT` — the report's own author, because a report is private work
      product and ``14-ai-report-agent.md`` keeps its history user-specific;
    * :attr:`USER` — the named user and nobody else. Identity equality, so it
      needs no database at all.

    There is deliberately **no "platform" or "broadcast" scope**. Every event this
    system carries is about something somebody owns, and a scope with no owner
    would be a scope with no authorization rule.
    """

    CASE = "case"
    DOCUMENT = "document"
    REPORT = "report"
    USER = "user"


#: Separator between a topic's scope and its resource identifier (``case:<uuid>``).
TOPIC_SEPARATOR: Final[str] = ":"


class InvalidTopicError(ValueError):
    """A topic string is not ``<scope>:<uuid>`` for a known scope.

    A plain ``ValueError`` rather than an :class:`~core.exceptions.AppException`:
    this module is pure vocabulary and has no HTTP opinion. The WebSocket layer
    translates it into a protocol error naming the offending subscription.
    """


@dataclass(frozen=True, slots=True)
class EventTopic:
    """What an event is about: a scope plus the identifier of one resource.

    Frozen and hashable, because it is a dictionary key in the routing table and
    a member of every connection's subscription set. Rendering and parsing are
    both here so the wire format is defined exactly once.
    """

    scope: EventScope
    resource_id: uuid.UUID

    @property
    def key(self) -> str:
        """The wire form, e.g. ``case:6f3e...``."""
        return f"{self.scope.value}{TOPIC_SEPARATOR}{self.resource_id}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.key

    @classmethod
    def parse(cls, value: str) -> EventTopic:
        """Resolve a client-supplied topic string.

        Raises:
            InvalidTopicError: unknown scope, missing separator, or an
                identifier that is not a UUID. Rejected rather than coerced —
                a topic the server cannot name is a topic it cannot authorize,
                and silently ignoring one would leave a client believing it is
                subscribed to something.
        """
        scope_name, separator, raw_id = value.partition(TOPIC_SEPARATOR)
        if not separator:
            raise InvalidTopicError(f"Topic {value!r} is not '<scope>:<id>'.")

        try:
            scope = EventScope(scope_name)
        except ValueError as exc:
            raise InvalidTopicError(f"Unknown topic scope {scope_name!r}.") from exc

        try:
            resource_id = uuid.UUID(raw_id)
        except ValueError as exc:
            raise InvalidTopicError(f"Topic {value!r} does not name a valid identifier.") from exc

        return cls(scope=scope, resource_id=resource_id)


def case_topic(case_id: uuid.UUID) -> EventTopic:
    """The topic carrying everything that happens to one case."""
    return EventTopic(scope=EventScope.CASE, resource_id=case_id)


def document_topic(document_id: uuid.UUID) -> EventTopic:
    """The topic carrying everything that happens to one document."""
    return EventTopic(scope=EventScope.DOCUMENT, resource_id=document_id)


def report_topic(report_id: uuid.UUID) -> EventTopic:
    """The topic carrying one report's generation progress and outcome."""
    return EventTopic(scope=EventScope.REPORT, resource_id=report_id)


def user_topic(user_id: uuid.UUID) -> EventTopic:
    """The topic carrying events addressed to one user personally."""
    return EventTopic(scope=EventScope.USER, resource_id=user_id)


# --------------------------------------------------------------------------- #
# Event types
# --------------------------------------------------------------------------- #


class DomainEventType(StrEnum):
    """Every event the platform publishes, named once.

    The groups follow the spec's own "Domain Events" list. Extending it is **one
    member here plus one line in** :data:`EVENT_SCOPES` — nothing in the
    dispatcher, the connection manager, the authorization policy, or the client
    changes, which is the *"allow future event types without redesign"*
    requirement made structural rather than promised.

    Note what is **not** here: **email and WhatsApp** delivery events. Those
    features remain out of scope, and defining their events now would be
    inventing their vocabulary before their behaviour. The notification events
    below arrived with ``16-notifications.md`` and describe only what that
    feature actually does.
    """

    # --- Case lifecycle ----------------------------------------------------- #
    CASE_CREATED = "case.created"
    CASE_UPDATED = "case.updated"
    CASE_ARCHIVED = "case.archived"
    CASE_RESTORED = "case.restored"
    CASE_STATUS_CHANGED = "case.status_changed"
    CASE_PRIORITY_CHANGED = "case.priority_changed"
    CASE_ASSIGNMENT_CHANGED = "case.assignment_changed"

    # --- Documents ---------------------------------------------------------- #
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_UPDATED = "document.updated"
    DOCUMENT_REPLACED = "document.replaced"
    DOCUMENT_DELETED = "document.deleted"

    # --- OCR ---------------------------------------------------------------- #
    OCR_STARTED = "ocr.started"
    OCR_COMPLETED = "ocr.completed"
    OCR_FAILED = "ocr.failed"

    # --- Document indexing --------------------------------------------------- #
    INDEXING_STARTED = "indexing.started"
    INDEXING_COMPLETED = "indexing.completed"
    INDEXING_FAILED = "indexing.failed"

    # --- AI report generation ------------------------------------------------ #
    #
    # ``REPORT_PROGRESS`` is the spec's "Streaming" section met with the same
    # envelope as everything else rather than a second mechanism: a report is
    # produced section by section, and each completed section is one more event on
    # the report's own topic. Scoped to the report — therefore to its author —
    # because progress on a report is as private as the report.
    REPORT_STARTED = "report.started"
    REPORT_PROGRESS = "report.progress"
    REPORT_GENERATED = "report.generated"
    REPORT_FAILED = "report.failed"

    # --- Timeline ------------------------------------------------------------ #
    #
    # One event rather than one per timeline entry type. The timeline is already a
    # registry of what happened; a client receiving this refetches the case's
    # history, and duplicating that registry here would create two vocabularies
    # that have to be kept in step.
    TIMELINE_UPDATED = "timeline.updated"

    # --- Accounts ------------------------------------------------------------ #
    #
    # What happened to *one person's* account, published on that person's own
    # topic. Added by ``16-notifications.md``, which names account activation, a
    # password reset, and a role change among the events a notification is
    # created for — and they are the first events on this platform whose audience
    # is a single named individual rather than everyone party to a resource.
    #
    # The user service publishes them **without knowing that notifications
    # exist**, exactly as the case and document services publish theirs: it holds
    # an `EventPublisher`, which has one method and no way to ask who is
    # listening. Nothing consumed them before this feature and nothing has to.
    USER_ACTIVATED = "user.activated"
    USER_DEACTIVATED = "user.deactivated"
    USER_ROLE_CHANGED = "user.role_changed"
    USER_PASSWORD_RESET = "user.password_reset"

    # --- Notifications -------------------------------------------------------- #
    #
    # The Notification Service's own events, and the reason it never touches a
    # socket. ``16-notifications.md`` requires that *"the Notification Service
    # must never communicate directly with clients"* and that *"all delivery
    # should use the existing event infrastructure"*; these are how that is
    # honoured. The service persists a notification and then publishes here, on
    # the recipient's own topic, and the connection manager routes it like
    # anything else.
    #
    # They are deliberately **not** in `EVENT_RULES` (`core/notifications.py`),
    # which is what makes a feedback loop impossible rather than merely unlikely:
    # the notification subscriber receives its own events and has no rule for
    # them.
    NOTIFICATION_CREATED = "notification.created"
    #: Read state changed somewhere. Published so a second tab's unread badge
    #: follows the tab the person actually read the notification in, which is the
    #: whole of what a client does with it.
    NOTIFICATION_READ = "notification.read"

    # --- Presence ------------------------------------------------------------ #
    #
    # Published to a user's own topic when their connection count changes, so a
    # second tab learns that the first one opened or closed. Presence
    # *visualization* is out of scope; this is the signal a future feature reads.
    PRESENCE_CHANGED = "presence.changed"

    @property
    def group(self) -> str:
        """The family part of the identifier (``case``, ``ocr``, ``report``)."""
        return self.value.split(".", 1)[0]


#: Which scope each event type is authorized against.
#:
#: Read-only at runtime, and **exhaustive by test**: a member with no entry here
#: would be an event nobody could decide on, so :mod:`tests` asserts the mapping
#: covers the enum rather than leaving the omission to be discovered in
#: production.
EVENT_SCOPES: Mapping[DomainEventType, EventScope] = MappingProxyType(
    {
        DomainEventType.CASE_CREATED: EventScope.CASE,
        DomainEventType.CASE_UPDATED: EventScope.CASE,
        DomainEventType.CASE_ARCHIVED: EventScope.CASE,
        DomainEventType.CASE_RESTORED: EventScope.CASE,
        DomainEventType.CASE_STATUS_CHANGED: EventScope.CASE,
        DomainEventType.CASE_PRIORITY_CHANGED: EventScope.CASE,
        DomainEventType.CASE_ASSIGNMENT_CHANGED: EventScope.CASE,
        DomainEventType.DOCUMENT_UPLOADED: EventScope.DOCUMENT,
        DomainEventType.DOCUMENT_UPDATED: EventScope.DOCUMENT,
        DomainEventType.DOCUMENT_REPLACED: EventScope.DOCUMENT,
        DomainEventType.DOCUMENT_DELETED: EventScope.DOCUMENT,
        DomainEventType.OCR_STARTED: EventScope.DOCUMENT,
        DomainEventType.OCR_COMPLETED: EventScope.DOCUMENT,
        DomainEventType.OCR_FAILED: EventScope.DOCUMENT,
        DomainEventType.INDEXING_STARTED: EventScope.DOCUMENT,
        DomainEventType.INDEXING_COMPLETED: EventScope.DOCUMENT,
        DomainEventType.INDEXING_FAILED: EventScope.DOCUMENT,
        DomainEventType.REPORT_STARTED: EventScope.REPORT,
        DomainEventType.REPORT_PROGRESS: EventScope.REPORT,
        DomainEventType.REPORT_GENERATED: EventScope.REPORT,
        DomainEventType.REPORT_FAILED: EventScope.REPORT,
        DomainEventType.TIMELINE_UPDATED: EventScope.CASE,
        # Every one of these is about, and addressed to, exactly one person, so
        # the user scope is not a convenience — it is the whole authorization
        # rule, and it is identity equality rather than a database lookup.
        DomainEventType.USER_ACTIVATED: EventScope.USER,
        DomainEventType.USER_DEACTIVATED: EventScope.USER,
        DomainEventType.USER_ROLE_CHANGED: EventScope.USER,
        DomainEventType.USER_PASSWORD_RESET: EventScope.USER,
        DomainEventType.NOTIFICATION_CREATED: EventScope.USER,
        DomainEventType.NOTIFICATION_READ: EventScope.USER,
        DomainEventType.PRESENCE_CHANGED: EventScope.USER,
    }
)


#: Scopes whose events may **also** be delivered to followers of their case.
#:
#: A case workspace shows a case, its documents, their extraction and indexing
#: state, and its timeline. Without this, keeping that screen live would mean one
#: subscription per document on it — a client re-subscribing on every page of a
#: document list, and the server authorizing each one. With it, the workspace
#: subscribes to ``case:<id>`` and receives everything that happens inside the
#: case.
#:
#: **It is safe for exactly the scopes listed and for no others**, and the reason
#: is a property of the platform's access model rather than a convenience:
#: `services/document_access.py` owns no policy of its own — a document is
#: reachable exactly when its case is — so a caller authorized for a case is, by
#: construction, authorized for every document in it. Delivering a document event
#: to an authorized case follower discloses nothing that following the case did
#: not already disclose.
#:
#: :attr:`EventScope.REPORT` is deliberately **absent**, and that absence is the
#: whole reason this is a set of scopes rather than a rule about ``case_id``: a
#: report *is* about a case and does carry one, but it belongs to the user who
#: generated it (``14-ai-report-agent.md``), so fanning it into the case topic
#: would put one lawyer's private work product in front of everyone on the
#: matter. :attr:`EventScope.USER` is absent for the same reason, one step
#: further: it is addressed to a person.
CASE_FANOUT_SCOPES: frozenset[EventScope] = frozenset(
    {EventScope.CASE, EventScope.DOCUMENT}
)


class UnknownEventScopeError(RuntimeError):
    """An event type has no entry in :data:`EVENT_SCOPES`.

    Always a programming fault — a member added to the enum without a scope — and
    it **fails closed**: the dispatcher drops the event rather than guessing a
    scope, because a wrong guess is an event delivered to the wrong people.
    """


def scope_for(event_type: DomainEventType) -> EventScope:
    """The scope ``event_type`` is authorized against.

    Raises:
        UnknownEventScopeError: the type has no policy entry.
    """
    try:
        return EVENT_SCOPES[event_type]
    except KeyError as exc:  # pragma: no cover - guarded by an exhaustiveness test
        raise UnknownEventScopeError(
            f"Event type {event_type.value!r} has no entry in EVENT_SCOPES."
        ) from exc


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #

#: Most keys one event payload may carry.
#:
#: The spec asks for lightweight events carrying *"only the information required
#: by clients"*. A payload past this bound is a publisher trying to replicate a
#: row over the socket instead of telling the client to read it.
MAX_PAYLOAD_KEYS: Final[int] = 20

#: Longest string value in a payload, in characters.
#:
#: Generous enough for a filename, a status, or a failure code, and far too small
#: for a page of extracted text — which is exactly the line it exists to draw.
MAX_PAYLOAD_VALUE_LENGTH: Final[int] = 500

#: Payload keys that are dropped outright, whatever their value.
#:
#: Every one of them names something the platform holds under access control and
#: has already decided must not travel in a log: extracted text, a chunk, a
#: question, an answer, a report section, a citation excerpt, a credential. A
#: publisher that puts one here is not asking for it to be delivered — it is
#: making a mistake — so the key is removed and the removal is recorded by the
#: caller, rather than the event being dropped and a client left waiting.
#:
#: Matching is on the key alone and is exact, because a substring rule
#: ("anything containing 'text'") would silently swallow ``context_turns`` and
#: ``file_extension``, and a rule nobody can predict is a rule publishers work
#: around.
FORBIDDEN_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "answer",
        "chunk",
        "chunks",
        "citation",
        "citations",
        "content",
        "excerpt",
        "extracted_text",
        "full_text",
        "message",
        "page_text",
        "pages",
        "passage",
        "passages",
        "password",
        "question",
        "secret",
        "section",
        "sections",
        "text",
        "token",
    }
)


class InvalidEventPayloadError(ValueError):
    """A payload cannot be represented as a bounded, JSON-safe object."""


def normalize_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, JSON-safe copy of ``payload`` with forbidden keys removed.

    Four rules, and each is one of the spec's payload requirements made
    mechanical:

    * **flat** — nested objects and arrays of objects are refused. A client needs
      identifiers and a status; a nested structure is a row being replicated;
    * **bounded** — at most :data:`MAX_PAYLOAD_KEYS` keys, each string value at
      most :data:`MAX_PAYLOAD_VALUE_LENGTH` characters;
    * **JSON-safe** — UUIDs and datetimes are rendered the way the rest of the
      API renders them, so a client parses one shape rather than two;
    * **screened** — :data:`FORBIDDEN_PAYLOAD_KEYS` are dropped.

    Returns:
        The normalized payload. Dropped keys are **not** silently forgotten: they
        are returned to the caller through :func:`screen_payload` when it needs to
        log them.

    Raises:
        InvalidEventPayloadError: the payload is too large, too deep, or holds a
            value with no JSON representation.
    """
    return screen_payload(payload)[0]


def screen_payload(
    payload: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """:func:`normalize_payload`, plus the names of the keys that were dropped.

    Separate from :func:`normalize_payload` so the dispatcher can log a
    publisher's mistake — *which* key was screened, never its value — while
    ordinary callers keep a one-value signature.
    """
    if not payload:
        return {}, []

    if len(payload) > MAX_PAYLOAD_KEYS:
        raise InvalidEventPayloadError(
            f"An event payload may carry at most {MAX_PAYLOAD_KEYS} keys."
        )

    normalized: dict[str, Any] = {}
    dropped: list[str] = []

    for raw_key, value in payload.items():
        key = str(raw_key)
        if key.lower() in FORBIDDEN_PAYLOAD_KEYS:
            dropped.append(key)
            continue
        normalized[key] = _normalize_value(key, value)

    return normalized, dropped


def _normalize_value(key: str, value: Any) -> Any:
    """Render one payload value as JSON-safe data, or refuse it."""
    if value is None or isinstance(value, bool | int | float):
        return value

    if isinstance(value, uuid.UUID):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, StrEnum):
        return value.value

    if isinstance(value, str):
        if len(value) > MAX_PAYLOAD_VALUE_LENGTH:
            raise InvalidEventPayloadError(
                f"Payload value {key!r} exceeds {MAX_PAYLOAD_VALUE_LENGTH} characters."
            )
        return value

    if isinstance(value, list | tuple):
        # A list of scalars is how a "fields changed" event says which fields.
        # A list of objects is a result set, which is not what this channel is
        # for — the client reads those from the API it is already authorized on.
        return [_normalize_scalar(key, item) for item in value]

    raise InvalidEventPayloadError(
        f"Payload value {key!r} is not a scalar, a list of scalars, or null."
    )


def _normalize_scalar(key: str, value: Any) -> Any:
    """Render one list element, refusing anything that is not a scalar."""
    if isinstance(value, list | tuple | dict | Mapping):
        raise InvalidEventPayloadError(f"Payload value {key!r} may not nest collections.")
    return _normalize_value(key, value)


# --------------------------------------------------------------------------- #
# The event itself
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """One thing that happened, ready to be dispatched.

    Frozen, because an event is a statement about the past: a subscriber that
    could edit one would change what every later subscriber sees. Built through
    :meth:`create`, which is what stamps the identity and the timestamp — a
    publisher supplying its own would make deduplication a matter of publisher
    discipline.

    Two identity fields, and they answer different questions:

    * :attr:`event_id` is *this* event, forever. It is what a client deduplicates
      on, which is the spec's "duplicate events are avoided" requirement met at
      the only place it can be met — a reconnecting client will legitimately be
      offered an event it already has.
    * :attr:`sequence` is the dispatcher's monotonic counter. It is what makes
      out-of-order or missing delivery *detectable*: a client that sees 41 then 43
      knows it lost one, where two timestamps a microsecond apart tell it nothing.
    """

    event_id: uuid.UUID
    sequence: int
    event_type: DomainEventType
    topic: EventTopic
    occurred_at: datetime

    #: The case this event ultimately belongs to, when there is one. Carried
    #: beside the topic rather than derived from it because a *document* event is
    #: authorized on its document and still wants the client to know which case to
    #: refresh — and resolving that on the client would be a request per event.
    case_id: uuid.UUID | None = None

    #: Who caused it, when a user did. Identifier only: a name would be a
    #: snapshot the timeline already keeps, and one this channel has no business
    #: broadcasting to everyone party to a case.
    actor_id: uuid.UUID | None = None

    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        event_type: DomainEventType,
        topic: EventTopic,
        sequence: int,
        case_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> DomainEvent:
        """Build an event, stamping its identity, time, and normalized payload.

        Raises:
            InvalidEventPayloadError: the payload is unbounded or unrepresentable.
        """
        return cls(
            event_id=uuid.uuid4(),
            sequence=sequence,
            event_type=event_type,
            topic=topic,
            occurred_at=datetime.now(UTC),
            case_id=case_id,
            actor_id=actor_id,
            payload=normalize_payload(payload),
        )

    @property
    def scope(self) -> EventScope:
        """The scope this event is authorized against."""
        return self.topic.scope


__all__ = [
    "CASE_FANOUT_SCOPES",
    "EVENT_SCOPES",
    "FORBIDDEN_PAYLOAD_KEYS",
    "MAX_PAYLOAD_KEYS",
    "MAX_PAYLOAD_VALUE_LENGTH",
    "TOPIC_SEPARATOR",
    "DomainEvent",
    "DomainEventType",
    "EventScope",
    "EventTopic",
    "InvalidEventPayloadError",
    "InvalidTopicError",
    "UnknownEventScopeError",
    "case_topic",
    "document_topic",
    "normalize_payload",
    "report_topic",
    "scope_for",
    "screen_payload",
    "user_topic",
]
