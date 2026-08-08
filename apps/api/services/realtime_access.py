"""Per-resource authorization for real-time event delivery.

RBAC answers *"may this user open a live channel?"* — that is
``realtime:connect``, and on its own it grants nothing. It cannot answer *"may
this user receive **this** event"*, because that depends on data, and
``15-real-time-synchronization.md`` is unambiguous about the standard:

    Users must never receive events for unauthorized cases, unauthorized
    documents, unauthorized reports, or conversations belonging to other users.
    Authorization should be enforced before every event is delivered.

This module is where that is decided, and — exactly like
:mod:`services.document_access`, :mod:`services.ocr_access`,
:mod:`services.indexing_access`, and :mod:`services.search_access` — **it owns no
policy of its own.** It resolves the resource a topic names and then asks the
module that already owns the rule:

    realtime topic → case  → CaseAccessPolicy
                   → document → DocumentAccessPolicy → CaseAccessPolicy
                   → report → owned by the requester, or not visible at all
                   → user  → identity equality

A fifth rule would be a fifth place for the platform's access model to drift, so
there is not one. The chain is the same chain every derived artefact on this
platform follows, one hop further out: **an event about a thing can never be more
visible than the thing.**

**Two decisions worth stating explicitly.**

*A report topic is scoped to its author, not to the case.* A report is private
work product (``14-ai-report-agent.md``: *"history must remain user-specific"*),
and :meth:`~repositories.report.ReportRepository.get` is keyed by owner — so the
lookup that authorizes a subscription is the same query that serves the report,
and an administrator holding ``cases:view-all`` still cannot follow somebody
else's generation. There is deliberately no report topic scoped to a case.

*There is no conversation topic at all.* The spec names conversations among the
things a user must never receive another's events for, and the strongest way to
honour that is to define no channel that could carry one: :class:`EventScope` has
no ``CONVERSATION`` member, so an assistant event has no topic to be published
on. The assistant streams over its own authenticated SSE response, to the one
caller who asked, which is narrower than any subscription model could be.

**Freshness, stated rather than hidden.** Resolving a case row for every event
for every connection would be a database query per delivery — the spec's
performance section asks for the opposite. So a grant is cached on the connection
with a timestamp, and re-resolved once it is older than
``REALTIME_AUTHORIZATION_TTL_SECONDS`` (30 by default; ``0`` means *re-check
every time*, which is correct and expensive). The consequence is bounded and
worth naming: a lawyer un-assigned from a case may receive that case's events for
up to the TTL before their subscription is revoked. It is a bound an operator
sets, it applies only to *notifications that something changed* rather than to
the changed data itself — every REST read behind it is authorized afresh — and
the immediate revocation path is unaffected, because deactivating an account
bumps ``session_generation`` and the connection's own token check fails on the
next heartbeat.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

import structlog
from sqlalchemy.orm import Session

from core.events import EventScope, EventTopic
from models.user import User
from repositories.case import CaseRepository
from repositories.document import DocumentRepository
from repositories.report import ReportRepository
from services.case_access import CaseAccessPolicy
from services.document_access import DocumentAccessPolicy

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TopicDecision:
    """Whether a topic may be followed, and why not when it may not.

    A value rather than an exception, because this is asked in two very different
    situations and only one of them is a request:

    * when a client **subscribes**, a refusal is reported back on the socket as
      an ``error`` frame naming the topic — the client asked, and is told;
    * when an event is **delivered**, a refusal is silent. The client is not
      told that an event it cannot see exists, because saying so would be the
      disclosure the check exists to prevent.

    Raising would force the delivery path to catch an exception per event per
    connection on its hottest loop, to express something it has no intention of
    reporting.
    """

    allowed: bool
    #: Short, stable reason. Logged, never sent to a client — the client is told
    #: only that the topic was refused, for the same reason a 403 body never
    #: names the permission that would have admitted it.
    reason: str = ""

    @classmethod
    def allow(cls) -> TopicDecision:
        """The topic may be followed."""
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str) -> TopicDecision:
        """The topic may not be followed."""
        return cls(allowed=False, reason=reason)


class RealtimeAccessPolicy:
    """Decides which topics a user may follow.

    Takes a **session factory** rather than a session, because a WebSocket
    connection outlives any request: a session held for the life of a connection
    would hold a pooled database connection for hours and would serve
    increasingly stale rows from its identity map. Each decision opens a session,
    asks one question, and closes it.

    The factory yields a **context-managed** session, which ``SessionLocal``
    satisfies directly — a SQLAlchemy ``Session`` is its own context manager — and
    which a test satisfies with a factory that yields a session it owns and
    declines to close. That is what lets these rules be exercised against the real
    repositories and the real case and document policies rather than against
    mocks of them.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
        *,
        cases: CaseAccessPolicy | None = None,
        documents: DocumentAccessPolicy | None = None,
    ) -> None:
        # Imported lazily so that constructing a policy does not require an
        # importable engine — `db.session` builds one at import time, and a unit
        # test for this class supplies a factory of its own.
        if session_factory is None:
            from db.session import SessionLocal

            session_factory = SessionLocal

        self._session_factory: Callable[[], AbstractContextManager[Session]] = session_factory
        self._cases = cases or CaseAccessPolicy()
        self._documents = documents or DocumentAccessPolicy(self._cases)

    # ---------------------------------------------------------- the decision #

    def decide(self, user: User, topic: EventTopic) -> TopicDecision:
        """Whether ``user`` may follow ``topic``.

        Fails **closed** in every branch: an unresolvable resource, an unknown
        scope, and a database error are all refusals rather than exceptions or
        silent allowances. A channel whose authorization degrades to "allow" when
        the database is unreachable is a channel with no authorization.
        """
        if topic.scope is EventScope.USER:
            # No database at all: a user's own topic is identity equality, which
            # is both the cheapest check on the platform and the only one that
            # cannot go stale.
            return (
                TopicDecision.allow()
                if topic.resource_id == user.id
                else TopicDecision.deny("not_self")
            )

        try:
            with self._session_factory() as session:
                return self._decide_persisted(session, user, topic)
        except Exception:
            logger.exception(
                "realtime_authorization_failed",
                user_id=str(user.id),
                scope=topic.scope.value,
            )
            return TopicDecision.deny("lookup_failed")

    def recheck(self, user_id: uuid.UUID, topic: EventTopic) -> TopicDecision:
        """Re-authorize ``topic`` for an account identified only by id.

        The delivery path's entry point, and it is deliberately **not**
        :meth:`decide` with a cached ``User``. A connection lives for hours; the
        account behind it can be deactivated, have its role changed, or be
        un-assigned from a case in that time, and a decision made against a
        snapshot taken at connect time would honour none of those. So the user is
        re-read in the same session as the resource, and the two questions a
        long-lived connection has to keep re-asking are answered together:

        * *is this still an account that may receive anything?* — a disabled
          account is refused here, which is what stops an already-open socket
          outliving a deactivation by the length of its access token;
        * *may it still reach this resource?*

        Fails closed: an account that cannot be resolved receives nothing.
        """
        try:
            with self._session_factory() as session:
                user = session.get(User, user_id)
                if user is None:
                    return TopicDecision.deny("user_not_found")
                if not user.is_active:
                    return TopicDecision.deny("account_disabled")

                if topic.scope is EventScope.USER:
                    return (
                        TopicDecision.allow()
                        if topic.resource_id == user.id
                        else TopicDecision.deny("not_self")
                    )

                return self._decide_persisted(session, user, topic)
        except Exception:
            logger.exception(
                "realtime_authorization_failed",
                user_id=str(user_id),
                scope=topic.scope.value,
            )
            return TopicDecision.deny("lookup_failed")

    def _decide_persisted(
        self, session: Session, user: User, topic: EventTopic
    ) -> TopicDecision:
        """Resolve the resource a topic names and defer to its owning policy."""
        if topic.scope is EventScope.CASE:
            legal_case = CaseRepository(session).get_by_id(topic.resource_id)
            if legal_case is None:
                return TopicDecision.deny("case_not_found")
            return (
                TopicDecision.allow()
                if self._cases.can_view(user, legal_case)
                else TopicDecision.deny("not_assigned")
            )

        if topic.scope is EventScope.DOCUMENT:
            document = DocumentRepository(session).get_by_id(topic.resource_id)
            if document is None:
                # A soft-deleted document resolves to `None` here, exactly as it
                # does for every read path — so a client keeps no channel onto a
                # document that has been withdrawn from the case.
                return TopicDecision.deny("document_not_found")
            return (
                TopicDecision.allow()
                if self._documents.can_view(user, document)
                else TopicDecision.deny("not_assigned")
            )

        if topic.scope is EventScope.REPORT:
            # Keyed by owner, so "somebody else's report" and "no such report"
            # are the same answer — which is the concealment
            # `14-ai-report-agent.md` requires, inherited rather than restated.
            report = ReportRepository(session).get(topic.resource_id, owner_id=user.id)
            return (
                TopicDecision.allow() if report is not None else TopicDecision.deny("not_owner")
            )

        return TopicDecision.deny("unknown_scope")  # pragma: no cover - defensive


#: There is deliberately **no "which topics may I follow?" method** here, and its
#: absence is a decision rather than an omission. Answering it would mean sending
#: a caller's whole caseload down a socket at connect time — a list of the
#: matters someone is working on, which is precisely the material every other
#: part of this feature is careful not to broadcast — to save a client a request
#: to an endpoint it is already authorized on and already calls. Clients
#: subscribe to what is on screen; the server grants or refuses each one.


__all__ = ["RealtimeAccessPolicy", "TopicDecision"]
