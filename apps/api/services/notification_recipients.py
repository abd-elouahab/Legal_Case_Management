"""Who receives a notification, and the authorization that decides it.

``16-notifications.md`` is unambiguous about the standard this module exists to
meet:

    Users should only receive notifications they are authorized to view. […] A
    document upload notification must never be created for a user who cannot
    access the document. **Authorization should be enforced before notification
    creation.**

Like :mod:`services.realtime_access`, :mod:`services.document_access`,
:mod:`services.ocr_access`, :mod:`services.indexing_access`, and
:mod:`services.search_access` before it, **this module owns no policy of its
own.** It resolves the audience an event's rule names and then asks the module
that already decides:

    audience → case      → CaseAccessPolicy
             → assignee  → CaseAccessPolicy (the assignee is party to the case)
             → report    → owned by the requester, or not visible at all
             → user      → identity, plus the account still being active

A fifth rule would be a fifth place for the platform's access model to drift, so
there is not one. The chain is the same chain every derived artefact on this
platform follows, one hop further out: **a notification about a thing can never
be more visible than the thing.**

**Two decisions worth stating explicitly.**

*Resolution and authorization are two passes, not one.* The audience says who
the platform *intends* to tell; the policy says who may actually be told, and it
is asked about each person individually and last. An audience that widened by
accident — a rule edited carelessly, a payload carrying an unexpected identifier
— would still be narrowed here, because the narrowing does not depend on the
audience having been right.

*A document event resolves through its case, and that is exact rather than
approximate.* :mod:`services.document_access` owns no policy either — a document
is reachable exactly when its case is — so "party to the case" and "may open the
document" are the same set by construction. Resolving through the case is
therefore not a shortcut past the document check; it *is* the document check,
without a second query per event.

**Why a session factory rather than a session.** This runs on the notification
worker thread, which has no request and therefore no request-scoped session — the
same situation :class:`~services.realtime_access.RealtimeAccessPolicy` is in, and
the same answer: open a session, ask one question, close it. A session held for
the life of the worker would pin a pooled database connection for the length of
the process and serve increasingly stale rows from its identity map.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import settings
from core.events import DomainEvent, EventScope
from core.notifications import NotificationAudience, NotificationRule
from models.case import Case
from models.user import User
from repositories.case import CaseRepository
from repositories.report import ReportRepository
from services.case_access import CaseAccessPolicy

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedAudience:
    """Who may be told about one event, and which case it concerns.

    A value rather than a bare list, because the *case* resolved along the way is
    something the notification itself carries — and resolving it twice, once to
    authorize and once to store, would be two queries to answer one question.
    """

    recipient_ids: tuple[uuid.UUID, ...] = ()
    case_id: uuid.UUID | None = None
    #: Set when the audience could not be resolved at all — the case, report, or
    #: account the event names could not be read. Distinguished from "nobody is
    #: entitled to this", which is an empty tuple and a perfectly ordinary
    #: outcome, because the first is a failure worth counting and the second is
    #: not.
    failed: bool = False
    #: Recipients dropped because the audience exceeded
    #: ``NOTIFICATION_MAX_RECIPIENTS``. Reported rather than silently trimmed.
    truncated: int = 0

    #: Short, stable reason a resolution produced nobody. Logged, never sent
    #: anywhere — the same contract
    #: :class:`~services.realtime_access.TopicDecision` keeps.
    reason: str = ""

    @property
    def is_empty(self) -> bool:
        """Whether there is nobody to notify."""
        return not self.recipient_ids


@dataclass(slots=True)
class _Candidates:
    """Intermediate result: who the audience names, before authorization."""

    user_ids: list[uuid.UUID] = field(default_factory=list)
    case: Case | None = None
    failed: bool = False
    reason: str = ""


class NotificationRecipientResolver:
    """Decides which accounts one event's notification is created for.

    Stateless apart from its collaborators. Every method opens its own session,
    asks one question, and closes it — see the module docstring for why.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
        *,
        cases: CaseAccessPolicy | None = None,
    ) -> None:
        # Imported lazily so that constructing a resolver does not require an
        # importable engine — `db.session` builds one at import time, and a unit
        # test for this class supplies a factory of its own. The same reasoning
        # `RealtimeAccessPolicy.__init__` records.
        if session_factory is None:
            from db.session import SessionLocal

            session_factory = SessionLocal

        self._session_factory: Callable[[], AbstractContextManager[Session]] = session_factory
        self._cases = cases or CaseAccessPolicy()

    # ------------------------------------------------------------ the answer #

    def resolve(self, event: DomainEvent, rule: NotificationRule) -> ResolvedAudience:
        """Who may be told about ``event``.

        Fails **closed** in every branch: an unresolvable resource, an unknown
        audience, and a database error all produce nobody rather than everybody.
        A notification system whose audience degrades to "the platform" when the
        database is unreachable is a notification system with no authorization.
        """
        try:
            with self._session_factory() as session:
                candidates = self._candidates(session, event, rule)
                if candidates.failed:
                    return ResolvedAudience(failed=True, reason=candidates.reason)

                if not candidates.user_ids:
                    return ResolvedAudience(
                        case_id=candidates.case.id if candidates.case else event.case_id,
                        reason=candidates.reason or "no_audience",
                    )

                authorized, truncated = self._authorize(
                    session, candidates.user_ids, case=candidates.case
                )
                return ResolvedAudience(
                    recipient_ids=tuple(authorized),
                    case_id=candidates.case.id if candidates.case else event.case_id,
                    truncated=truncated,
                    reason="" if authorized else "not_authorized",
                )
        except Exception:
            # Logged by *type* and never with the payload: an event's payload
            # carries a case number, and a resolver failure is an operational
            # fault rather than a reason to write one into a log.
            logger.exception(
                "notification_recipients_failed",
                event_type=event.event_type.value,
                scope=event.scope.value,
            )
            return ResolvedAudience(failed=True, reason="lookup_failed")

    # ------------------------------------------------------------ resolution #

    def _candidates(
        self, session: Session, event: DomainEvent, rule: NotificationRule
    ) -> _Candidates:
        """Name the accounts an audience refers to, before any policy is applied."""
        if rule.audience is NotificationAudience.SUBJECT_USER:
            # No database at all: an event on a user topic is *about* the account
            # the topic names, which is both the cheapest resolution on the
            # platform and the only one that cannot go stale.
            if event.scope is not EventScope.USER:
                return _Candidates(failed=True, reason="not_a_user_topic")
            return _Candidates(user_ids=[event.topic.resource_id])

        if rule.audience is NotificationAudience.RESOURCE_OWNER:
            return self._report_owner(session, event)

        if rule.audience is NotificationAudience.ASSIGNEE:
            return self._assignee(session, event)

        if rule.audience is NotificationAudience.CASE_PARTICIPANTS:
            return self._case_participants(session, event)

        return _Candidates(failed=True, reason="unknown_audience")  # pragma: no cover

    def _case_participants(self, session: Session, event: DomainEvent) -> _Candidates:
        """Everyone party to the case an event concerns.

        The assigned lawyer, the assigned court representative, and whoever
        created the case — **and deliberately not every administrator**. An
        administrator holds ``cases:view-all`` and could open any case, so
        notifying all of them would be authorized and would also be noise: a feed
        that reports every event on the platform is a feed nobody reads, and the
        spec asks for notifications about what concerns the reader.

        The **actor is excluded**. Telling somebody what they have just done is
        the fastest way to make a notification feed feel like an echo, and the
        confirmation they need is the response to the request they made.
        """
        case_id = event.case_id if event.case_id is not None else _case_id_of(event)
        if case_id is None:
            return _Candidates(failed=True, reason="no_case")

        legal_case = CaseRepository(session).get_by_id(case_id)
        if legal_case is None:
            return _Candidates(failed=True, reason="case_not_found")

        participants = [
            candidate
            for candidate in (
                legal_case.assigned_lawyer_id,
                legal_case.assigned_court_representative_id,
                legal_case.created_by,
            )
            if candidate is not None and candidate != event.actor_id
        ]
        return _Candidates(user_ids=_unique(participants), case=legal_case)

    def _assignee(self, session: Session, event: DomainEvent) -> _Candidates:
        """The person an assignment event names.

        The one audience read from a **payload** rather than from a row, and it
        is safe for a specific reason: the identifier is put there by
        :mod:`services.case`, from the assignment column it has just written —
        never by a client, which has no path to an event payload at all. It is
        still authorized like every other recipient, against the case it names.

        The actor is **not** excluded here, unlike a case-participant audience:
        an administrator who assigns a case to themselves has genuinely been
        assigned it, and the notification is the record of that.
        """
        assignee_id = _uuid_from(event.payload.get("assignee_id"))
        if assignee_id is None:
            return _Candidates(reason="no_assignee")

        case_id = event.case_id if event.case_id is not None else _case_id_of(event)
        if case_id is None:
            return _Candidates(failed=True, reason="no_case")

        legal_case = CaseRepository(session).get_by_id(case_id)
        if legal_case is None:
            return _Candidates(failed=True, reason="case_not_found")

        return _Candidates(user_ids=[assignee_id], case=legal_case)

    def _report_owner(self, session: Session, event: DomainEvent) -> _Candidates:
        """The user who asked for the report an event is about.

        Resolved through :meth:`~repositories.report.ReportRepository.get_for_worker`,
        which is the one unscoped read on that repository and is named so its
        legitimate callers are obvious — this is one: there is no caller here to
        scope against, because the resolver's whole job is to *find out* whose
        report it is.

        The case is deliberately **not** loaded, and the notification is
        therefore not authorized against it. That is the correct rule rather than
        a gap: ``14-ai-report-agent.md`` makes a report its author's private work
        product, and a lawyer unassigned from a matter still owns the report they
        generated while they were on it. Ownership *is* the authorization here,
        exactly as it is in :mod:`services.realtime_access` for a report topic.
        """
        report_id = _uuid_from(event.payload.get("report_id"))
        if report_id is None:
            return _Candidates(failed=True, reason="no_report")

        report = ReportRepository(session).get_for_worker(report_id)
        if report is None:
            return _Candidates(failed=True, reason="report_not_found")

        return _Candidates(user_ids=[report.requested_by])

    # --------------------------------------------------------- authorization #

    def _authorize(
        self, session: Session, user_ids: Sequence[uuid.UUID], *, case: Case | None
    ) -> tuple[list[uuid.UUID], int]:
        """Keep only the accounts that may actually be told.

        Two questions, asked of every candidate individually and asked **last**:

        * *is this an account that may receive anything?* — a disabled or deleted
          account is dropped here, which is what stops a suspended user
          accumulating a feed of a firm's matters to read if they are ever
          reinstated;
        * *may it reach the resource?* — delegated to
          :class:`~services.case_access.CaseAccessPolicy` when the event concerns
          a case. When it does not, the audience was identity or ownership and
          has already answered.

        One query for the whole batch, so authorizing a three-person case costs
        one statement rather than three.

        Returns:
            ``(authorized, truncated)``. The second is how many recipients were
            dropped for exceeding ``NOTIFICATION_MAX_RECIPIENTS`` — reported so
            the caller can log it, because an audience silently trimmed is an
            audience nobody notices has stopped being complete.
        """
        users = list(
            session.execute(select(User).where(User.id.in_(list(user_ids)))).scalars().all()
        )
        by_id = {user.id: user for user in users}

        authorized: list[uuid.UUID] = []
        for user_id in user_ids:
            user = by_id.get(user_id)
            if user is None or not user.is_active:
                continue
            if case is not None and not self._cases.can_view(user, case):
                continue
            authorized.append(user_id)

        ceiling = settings.NOTIFICATION_MAX_RECIPIENTS
        if len(authorized) > ceiling:
            return authorized[:ceiling], len(authorized) - ceiling
        return authorized, 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _case_id_of(event: DomainEvent) -> uuid.UUID | None:
    """The case an event concerns, from its topic when it is a case topic.

    :attr:`~core.events.DomainEvent.case_id` is set by every publisher that has
    one, so this is a fallback rather than the usual path — and it exists because
    a rule that expects a case and receives an event without one should produce
    nothing rather than an exception.
    """
    if event.scope is EventScope.CASE:
        return event.topic.resource_id
    return None


def _uuid_from(value: Any) -> uuid.UUID | None:
    """Read an identifier out of a payload.

    Payload values are JSON-safe by the time they reach here — the dispatcher
    renders a ``UUID`` as a string — so this is a parse rather than a cast.
    """
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _unique(values: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Collapse repeats, preserving order.

    One person can hold two positions on a case — the administrator who created
    it and later assigned it to themselves — and telling them twice about one
    change is the duplicate this feature is least excusable for.
    """
    seen: set[uuid.UUID] = set()
    unique: list[uuid.UUID] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


__all__ = ["NotificationRecipientResolver", "ResolvedAudience"]
