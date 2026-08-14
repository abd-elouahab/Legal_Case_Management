"""Dashboard data access: the aggregate reads behind every widget.

Single responsibility: answering "how many", "which five", and "how much" over
rows that other modules own. Nothing here decides *whether* a caller may see a
figure — that is :mod:`services.dashboard_access` and the widget catalog in
:mod:`core.dashboard` — and nothing here formats one.

Three properties of this module are load-bearing, and each is a requirement of
``19-dashboard-analytics.md`` made structural rather than promised:

* **Every query is scoped, and the scope is a parameter with no default.** Each
  read takes ``visible_to``: a user identifier restricts it to the cases that
  person is party to, and ``None`` means the whole platform. There is no
  unscoped convenience variant to reach for by accident, and the predicate is
  :func:`~repositories.case.assigned_case_scope` — the *same* clause Case
  Management and Document Management apply — rather than a second copy of the
  rule. The spec's *"aggregated metrics must never leak unauthorized
  information"* is therefore one function, used everywhere.

* **Every aggregate executes in the database.** Counting in Python would mean
  loading every case on the platform to report how many are open, which is the
  spec's "minimize database queries / aggregate data efficiently" read
  backwards. The lists are ``LIMIT``-ed for the same reason: a widget shows five
  rows, so it reads five.

* **Nothing is invented.** Every figure here is ``COUNT``, ``SUM``, or a bounded
  ``SELECT`` over real rows. Where there is no data the answer is zero or an
  empty list, and where an average has no observations the answer is ``None`` —
  never an estimate, a trend, or a smoothed series. That is the whole of the
  spec's "Analytics Data Integrity" section, and it is why this module has no
  arithmetic in it beyond division by a count it just measured.

**Why the dashboard has its own repository at all**, rather than calling the
feature repositories: those answer *page* questions — a filtered, sorted,
paginated set of rows described by a query schema — and a widget asks a
fundamentally different one. Building a ``CaseListQuery`` to fetch five rows, or
a ``DocumentListQuery`` to count them, would be constructing a page in order to
throw it away. The one exception is deliberate and goes the other way: the
notifications widget reads through
:class:`~repositories.notification.NotificationRepository`, because every read
there is keyed by recipient and adding a second place that queries the feed would
be a second place for that rule to be got wrong.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Select, func, select, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from core.dashboard import Bucket
from core.timeline import category_for
from models.case import Case, CaseStatus
from models.conversation import Conversation, ConversationStatus
from models.document import Document, DocumentVersion
from models.indexing import DocumentIndex, IndexStatus
from models.ocr import OcrResult, OcrStatus
from models.report import Report, ReportStatus
from models.timeline import TimelineEvent
from models.user import User, UserRole, UserStatus
from repositories.case import assigned_case_scope

# --------------------------------------------------------------------------- #
# Result rows
# --------------------------------------------------------------------------- #
#
# Frozen dataclasses rather than tuples or dicts, for the reason every other
# repository here returns one: a widget loader reading `.open_cases` cannot
# silently start reading a different column when a query grows a field, and the
# service never has to remember what position something was in.


@dataclass(frozen=True, slots=True)
class CaseAnalytics:
    """Case counts over one window, plus the standing totals."""

    #: Cases in the caller's scope, whatever their status. Includes archived
    #: ones: they are still cases, and a total that moved when somebody tidied up
    #: would be measuring housekeeping — the same reasoning
    #: :meth:`~repositories.notification.NotificationRepository.statistics`
    #: records for archived notifications.
    total: int
    #: Neither closed nor archived. The spec's "active cases".
    active: int
    #: Standing count, not windowed: "how many closed cases exist".
    closed: int
    archived: int
    #: Created inside the window. The spec's "newly created cases".
    created_in_window: int
    #: Moved to ``closed`` inside the window — approximated by ``updated_at``,
    #: which is the only timestamp a case carries for it. Stated rather than
    #: hidden: a closed case edited afterwards counts in the window it was edited
    #: in, and a *closed-at* column is the fix, which belongs to Case Management
    #: rather than to a read-only dashboard.
    closed_in_window: int


@dataclass(frozen=True, slots=True)
class HearingSummary:
    """How the caller's hearing diary falls across the next few weeks."""

    #: Scheduled for today.
    today: int
    #: Within the next seven days, today included.
    next_7_days: int
    #: Within the next thirty days, today included.
    next_30_days: int
    #: In the past and still on a case that is not closed or archived — a hearing
    #: date nobody has moved on from. Reported because *"what requires my
    #: attention?"* is the dashboard's first question and this is the most
    #: reliable answer to it.
    overdue: int


@dataclass(frozen=True, slots=True)
class DocumentAnalytics:
    """Document, extraction, and indexing counts over one window."""

    total: int
    uploaded_in_window: int
    ocr_completed: int
    ocr_failed: int
    indexed: int
    indexing_failed: int
    #: Bytes stored across the documents in scope. ``0`` when there are none.
    total_bytes: int


@dataclass(frozen=True, slots=True)
class StorageUsage:
    """Platform-wide storage, as the administrator's widget reports it."""

    document_count: int
    total_bytes: int
    #: Bytes held by superseded versions. Legal documents are versioned and
    #: nothing is deleted, so this is the part of the bill that only grows, and
    #: reporting it separately is the difference between "we store a lot" and "we
    #: keep a lot of history".
    version_bytes: int
    average_bytes: float | None
    by_category: tuple[Bucket, ...]


@dataclass(frozen=True, slots=True)
class UserActivity:
    """Who has an account and who has used it lately."""

    total: int
    active: int
    inactive: int
    suspended: int
    #: Signed in inside the recency window. The spec's "active users", measured
    #: from ``users.last_login_at`` — a fact the platform records rather than an
    #: inference from a socket, so it survives a restart and does not count a
    #: browser tab somebody left open in a hotel.
    recently_signed_in: int
    by_role: tuple[Bucket, ...]


@dataclass(frozen=True, slots=True)
class QueueDepths:
    """Work the platform has accepted and not yet finished.

    Counted from **persisted lifecycle rows**, never from the in-process thread
    pools. A pool's depth is one API instance's opinion, resets on deploy, and is
    zero on the instance that happens to serve the dashboard; a row that says
    ``pending`` is the platform's own record of work it owes, and it is the same
    number from every instance.
    """

    ocr_pending: int
    ocr_processing: int
    indexing_pending: int
    indexing_processing: int
    report_pending: int
    report_processing: int

    @property
    def total(self) -> int:
        """Everything queued or in flight, across all three pipelines."""
        return (
            self.ocr_pending
            + self.ocr_processing
            + self.indexing_pending
            + self.indexing_processing
            + self.report_pending
            + self.report_processing
        )


@dataclass(frozen=True, slots=True)
class ReportAnalytics:
    """The caller's own report history over one window."""

    total: int
    completed: int
    failed: int
    in_progress: int
    generated_in_window: int


@dataclass(frozen=True, slots=True)
class ConversationAnalytics:
    """The caller's own assistant usage over one window."""

    total: int
    active: int
    started_in_window: int
    messages: int


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #


class DashboardRepository:
    """Aggregate reads for the dashboard's widgets."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ----------------------------------------------------------------- cases #

    def case_status_breakdown(self, *, visible_to: uuid.UUID | None) -> tuple[Bucket, ...]:
        """How many cases sit in each lifecycle state.

        Every status is returned, including the ones with no cases, so the widget
        renders a stable set of bars rather than one that gains and loses
        segments as work moves. A zero here is a measured zero.
        """
        rows = self._session.execute(
            self._scoped(select(Case.status, func.count().label("count")), visible_to)
            .group_by(Case.status)
        ).all()

        counts = {str(_value(status)): int(count) for status, count in rows}
        return tuple(
            Bucket(key=status.value, count=counts.get(status.value, 0))
            for status in CaseStatus
        )

    def case_analytics(
        self, *, visible_to: uuid.UUID | None, start: datetime, end: datetime
    ) -> CaseAnalytics:
        """Case totals, plus what happened inside the window.

        One statement with conditional aggregates rather than six ``COUNT``
        queries: the filters differ, the scope does not, and the database can
        answer all of them in a single pass over the same rows.
        """
        closed_or_archived = (CaseStatus.CLOSED, CaseStatus.ARCHIVED)
        windowed = (Case.updated_at >= start) & (Case.updated_at < end)

        row = self._session.execute(
            self._scoped(
                select(
                    func.count().label("total"),
                    func.count()
                    .filter(Case.status.notin_(closed_or_archived))
                    .label("active"),
                    func.count().filter(Case.status == CaseStatus.CLOSED).label("closed"),
                    func.count()
                    .filter(Case.status == CaseStatus.ARCHIVED)
                    .label("archived"),
                    func.count()
                    .filter((Case.created_at >= start) & (Case.created_at < end))
                    .label("created_in_window"),
                    func.count()
                    .filter((Case.status == CaseStatus.CLOSED) & windowed)
                    .label("closed_in_window"),
                ).select_from(Case),
                visible_to,
            )
        ).one()

        return CaseAnalytics(
            total=int(row.total or 0),
            active=int(row.active or 0),
            closed=int(row.closed or 0),
            archived=int(row.archived or 0),
            created_in_window=int(row.created_in_window or 0),
            closed_in_window=int(row.closed_in_window or 0),
        )

    def assigned_cases(self, *, user_id: uuid.UUID, limit: int) -> list[Case]:
        """The cases this person is personally on, most urgent first.

        **Always scoped to the individual**, even for a caller holding
        ``cases:view-all``: "my cases" is a question about assignment rather than
        about visibility, and answering it with the whole caseload would make an
        administrator's dashboard useless at its own first question.

        Ordered by the nearest hearing and then by the most recently touched.
        Cases with no hearing scheduled sort last — ``NULLS LAST`` is expressed as
        a boolean sort key rather than with the PostgreSQL clause, because SQLite
        (the test database) does not accept it and a dashboard whose ordering is
        only correct in production is a dashboard nobody could test.
        """
        statement = (
            select(Case)
            .where(assigned_case_scope(user_id))
            .where(Case.status.notin_((CaseStatus.CLOSED, CaseStatus.ARCHIVED)))
            .order_by(
                Case.next_hearing_date.is_(None),
                Case.next_hearing_date.asc(),
                Case.updated_at.desc(),
                Case.id.asc(),
            )
            .limit(limit)
        )
        return list(self._session.execute(statement).scalars().all())

    def recent_cases(self, *, visible_to: uuid.UUID | None, limit: int) -> list[Case]:
        """The cases touched most recently, within the caller's scope.

        Archived cases are excluded: this widget answers *"what changed
        recently?"*, and an archive operation is the last thing that will ever
        change one.
        """
        statement = (
            self._scoped(select(Case), visible_to)
            .where(Case.status != CaseStatus.ARCHIVED)
            .order_by(Case.updated_at.desc(), Case.id.asc())
            .limit(limit)
        )
        return list(self._session.execute(statement).scalars().all())

    def upcoming_hearings(
        self,
        *,
        visible_to: uuid.UUID | None,
        today: date,
        horizon_days: int,
        limit: int,
    ) -> list[Case]:
        """Cases with a hearing between today and the horizon, soonest first.

        Today is **included**: a hearing this afternoon is the most upcoming
        hearing there is, and a diary that dropped it at midnight would be wrong
        for the one day it mattered.
        """
        horizon = today + _days(horizon_days)
        statement = (
            self._scoped(select(Case), visible_to)
            .where(Case.next_hearing_date.is_not(None))
            .where(Case.next_hearing_date >= today)
            .where(Case.next_hearing_date <= horizon)
            .where(Case.status != CaseStatus.ARCHIVED)
            .order_by(Case.next_hearing_date.asc(), Case.id.asc())
            .limit(limit)
        )
        return list(self._session.execute(statement).scalars().all())

    def hearing_summary(
        self, *, visible_to: uuid.UUID | None, today: date
    ) -> HearingSummary:
        """The shape of the hearing diary: today, this week, this month, overdue."""
        active = Case.status.notin_((CaseStatus.CLOSED, CaseStatus.ARCHIVED))

        row = self._session.execute(
            self._scoped(
                select(
                    func.count()
                    .filter(Case.next_hearing_date == today)
                    .label("today"),
                    func.count()
                    .filter(
                        (Case.next_hearing_date >= today)
                        & (Case.next_hearing_date <= today + _days(6))
                    )
                    .label("next_7"),
                    func.count()
                    .filter(
                        (Case.next_hearing_date >= today)
                        & (Case.next_hearing_date <= today + _days(29))
                    )
                    .label("next_30"),
                    func.count()
                    .filter((Case.next_hearing_date < today) & active)
                    .label("overdue"),
                ).select_from(Case),
                visible_to,
            ).where(Case.next_hearing_date.is_not(None))
        ).one()

        return HearingSummary(
            today=int(row.today or 0),
            next_7_days=int(row.next_7 or 0),
            next_30_days=int(row.next_30 or 0),
            overdue=int(row.overdue or 0),
        )

    # ------------------------------------------------------------- documents #

    def recent_documents(
        self, *, visible_to: uuid.UUID | None, limit: int
    ) -> list[Document]:
        """The newest documents in the caller's cases.

        Soft-deleted documents are excluded, exactly as Document Management's own
        list excludes them: they are retained for the audit trail, not for a
        dashboard.
        """
        statement = (
            self._scoped_documents(select(Document), visible_to)
            .order_by(Document.created_at.desc(), Document.id.asc())
            .limit(limit)
        )
        return list(self._session.execute(statement).scalars().all())

    def document_analytics(
        self, *, visible_to: uuid.UUID | None, start: datetime, end: datetime
    ) -> DocumentAnalytics:
        """Uploads, extraction, and indexing across the caller's documents.

        Three statements rather than one, and deliberately: extraction and
        indexing rows live in their own tables, and folding them into the document
        query with outer joins would multiply the document rows by their run
        history and make ``SUM(file_size)`` count the same file once per attempt.
        """
        documents = self._session.execute(
            self._scoped_documents(
                select(
                    func.count().label("total"),
                    func.count()
                    .filter((Document.created_at >= start) & (Document.created_at < end))
                    .label("uploaded"),
                    func.coalesce(func.sum(Document.file_size), 0).label("bytes"),
                ).select_from(Document),
                visible_to,
            )
        ).one()

        ocr = self._session.execute(
            self._current_ocr(
                select(
                    func.count()
                    .filter(OcrResult.status == OcrStatus.COMPLETED)
                    .label("completed"),
                    func.count()
                    .filter(OcrResult.status == OcrStatus.FAILED)
                    .label("failed"),
                ),
                visible_to,
            )
        ).one()

        indexing = self._session.execute(
            self._current_index(
                select(
                    func.count()
                    .filter(DocumentIndex.status == IndexStatus.INDEXED)
                    .label("indexed"),
                    func.count()
                    .filter(DocumentIndex.status == IndexStatus.FAILED)
                    .label("failed"),
                ),
                visible_to,
            )
        ).one()

        return DocumentAnalytics(
            total=int(documents.total or 0),
            uploaded_in_window=int(documents.uploaded or 0),
            ocr_completed=int(ocr.completed or 0),
            ocr_failed=int(ocr.failed or 0),
            indexed=int(indexing.indexed or 0),
            indexing_failed=int(indexing.failed or 0),
            total_bytes=int(documents.bytes or 0),
        )

    def ocr_status_breakdown(
        self, *, visible_to: uuid.UUID | None
    ) -> tuple[Bucket, ...]:
        """Where the caller's documents stand in the extraction pipeline.

        Counted over the **current version** of each document, which is what
        makes this a picture of the corpus rather than of the platform's workload:
        a file replaced three times has three extraction runs, and only the newest
        says whether that document is readable today.

        A document with no run at all is reported as ``not_started`` rather than
        omitted — that state is exactly what somebody looking at this widget wants
        to find, and dropping it would make the segments add up to less than the
        library.
        """
        rows = self._session.execute(
            self._current_ocr(select(OcrResult.status, func.count().label("count")), visible_to)
            .group_by(OcrResult.status)
        ).all()
        counts = {str(_value(status)): int(count) for status, count in rows}

        total_documents = self._session.execute(
            self._scoped_documents(
                select(func.count()).select_from(Document), visible_to
            )
        ).scalar_one()
        not_started = max(0, int(total_documents or 0) - sum(counts.values()))

        return (
            *(
                Bucket(key=status.value, count=counts.get(status.value, 0))
                for status in OcrStatus
            ),
            Bucket(key="not_started", count=not_started),
        )

    def storage_usage(self, *, visible_to: uuid.UUID | None) -> StorageUsage:
        """What the platform is holding, and where it is going.

        Scoped like everything else even though only an administrative widget
        reads it today. A method that ignored its scope would be the one place a
        future caller could accidentally publish the whole library's size to
        somebody entitled to one case.
        """
        totals = self._session.execute(
            self._scoped_documents(
                select(
                    func.count().label("documents"),
                    func.coalesce(func.sum(Document.file_size), 0).label("bytes"),
                ).select_from(Document),
                visible_to,
            )
        ).one()

        version_bytes = self._session.execute(
            select(func.coalesce(func.sum(DocumentVersion.file_size), 0))
            .select_from(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Case, Case.id == Document.case_id)
            .where(Document.deleted_at.is_(None))
            .where(_scope_predicate(visible_to))
        ).scalar_one()

        categories = self._session.execute(
            self._scoped_documents(
                select(Document.category, func.count().label("count")), visible_to
            ).group_by(Document.category)
        ).all()

        documents = int(totals.documents or 0)
        total_bytes = int(totals.bytes or 0)

        return StorageUsage(
            document_count=documents,
            total_bytes=total_bytes,
            version_bytes=int(version_bytes or 0),
            # `None` rather than `0` when there is nothing stored: an average over
            # no documents is undefined, while zero would read as "every file is
            # empty".
            average_bytes=round(total_bytes / documents, 2) if documents else None,
            by_category=tuple(
                Bucket(key=str(_value(category)), count=int(count))
                for category, count in sorted(
                    categories, key=lambda row: str(_value(row[0]))
                )
            ),
        )

    # ----------------------------------------------------------------- users #

    def user_activity(self, *, active_since: datetime) -> UserActivity:
        """Account totals and how many have signed in lately.

        Platform-wide by construction — accounts belong to nobody's case — which
        is why the widget that reads this requires ``users:view`` rather than a
        case capability.
        """
        row = self._session.execute(
            select(
                func.count().label("total"),
                func.count().filter(User.status == UserStatus.ACTIVE).label("active"),
                func.count()
                .filter(User.status == UserStatus.INACTIVE)
                .label("inactive"),
                func.count()
                .filter(User.status == UserStatus.SUSPENDED)
                .label("suspended"),
                func.count()
                .filter(User.last_login_at >= active_since)
                .label("recent"),
            ).select_from(User)
        ).one()

        roles = self._session.execute(
            select(User.role, func.count().label("count"))
            .where(User.status == UserStatus.ACTIVE)
            .group_by(User.role)
        ).all()
        by_role = {str(_value(role)): int(count) for role, count in roles}

        return UserActivity(
            total=int(row.total or 0),
            active=int(row.active or 0),
            inactive=int(row.inactive or 0),
            suspended=int(row.suspended or 0),
            recently_signed_in=int(row.recent or 0),
            by_role=tuple(
                Bucket(key=role.value, count=by_role.get(role.value, 0))
                for role in UserRole
            ),
        )

    # ------------------------------------------------------------- pipelines #

    def queue_depths(self) -> QueueDepths:
        """Outstanding work in the three pipelines that persist a lifecycle.

        Platform-wide and unscoped, because a backlog is not about anybody's
        case: it is a capacity figure, and the widget reading it requires the
        monitoring capabilities that already gate every other capacity view.
        """
        ocr = self._session.execute(
            select(
                func.count().filter(OcrResult.status == OcrStatus.PENDING).label("pending"),
                func.count()
                .filter(OcrResult.status == OcrStatus.PROCESSING)
                .label("processing"),
            ).select_from(OcrResult)
        ).one()

        indexing = self._session.execute(
            select(
                func.count()
                .filter(DocumentIndex.status == IndexStatus.PENDING)
                .label("pending"),
                func.count()
                .filter(DocumentIndex.status == IndexStatus.INDEXING)
                .label("processing"),
            ).select_from(DocumentIndex)
        ).one()

        reports = self._session.execute(
            select(
                func.count().filter(Report.status == ReportStatus.PENDING).label("pending"),
                func.count()
                .filter(Report.status == ReportStatus.PROCESSING)
                .label("processing"),
            )
            .select_from(Report)
            .where(Report.deleted_at.is_(None))
        ).one()

        return QueueDepths(
            ocr_pending=int(ocr.pending or 0),
            ocr_processing=int(ocr.processing or 0),
            indexing_pending=int(indexing.pending or 0),
            indexing_processing=int(indexing.processing or 0),
            report_pending=int(reports.pending or 0),
            report_processing=int(reports.processing or 0),
        )

    # -------------------------------------------------------------- timeline #

    def recent_activity(
        self, *, visible_to: uuid.UUID | None, limit: int
    ) -> list[TimelineEvent]:
        """The newest timeline entries across every case the caller may see."""
        statement = (
            select(TimelineEvent)
            .join(Case, Case.id == TimelineEvent.case_id)
            .where(_scope_predicate(visible_to))
            .order_by(TimelineEvent.created_at.desc(), TimelineEvent.id.asc())
            .limit(limit)
        )
        return list(self._session.execute(statement).scalars().all())

    def activity_breakdown(
        self, *, visible_to: uuid.UUID | None, start: datetime, end: datetime
    ) -> tuple[int, tuple[Bucket, ...]]:
        """Timeline volume in the window, grouped by category.

        Grouped **in Python from a SQL count per event type**, because the
        category is derived from the type (:func:`~core.timeline.category_for`)
        rather than stored — deliberately, so the two can never disagree. The set
        being folded is the registry of event types, which is bounded by the
        source code rather than by traffic.

        Returns the window's total beside the buckets, so a widget can say "312
        entries" without summing a list the API already summed.
        """
        rows = self._session.execute(
            select(TimelineEvent.event_type, func.count().label("count"))
            .join(Case, Case.id == TimelineEvent.case_id)
            .where(_scope_predicate(visible_to))
            .where(TimelineEvent.created_at >= start)
            .where(TimelineEvent.created_at < end)
            .group_by(TimelineEvent.event_type)
        ).all()

        totals: dict[str, int] = {}
        for event_type, count in rows:
            category = category_for(str(event_type)).value
            totals[category] = totals.get(category, 0) + int(count)

        buckets = tuple(
            Bucket(key=category, count=count)
            for category, count in sorted(totals.items())
        )
        return sum(totals.values()), buckets

    # ------------------------------------------------------------------- AI #

    def recent_reports(self, *, requested_by: uuid.UUID, limit: int) -> list[Report]:
        """The caller's newest reports.

        Keyed by the requester with no unscoped variant, exactly as
        :mod:`repositories.report` is: a report belongs to the person who asked
        for it, and there is no ``reports:view-all`` for a dashboard to have
        honoured.
        """
        statement = (
            select(Report)
            .where(Report.requested_by == requested_by)
            .where(Report.deleted_at.is_(None))
            .order_by(Report.created_at.desc(), Report.id.asc())
            .limit(limit)
        )
        return list(self._session.execute(statement).scalars().all())

    def report_analytics(
        self, *, requested_by: uuid.UUID, start: datetime, end: datetime
    ) -> ReportAnalytics:
        """The caller's own generation history."""
        row = self._session.execute(
            select(
                func.count().label("total"),
                func.count()
                .filter(Report.status == ReportStatus.COMPLETED)
                .label("completed"),
                func.count().filter(Report.status == ReportStatus.FAILED).label("failed"),
                func.count()
                .filter(
                    Report.status.in_((ReportStatus.PENDING, ReportStatus.PROCESSING))
                )
                .label("in_progress"),
                func.count()
                .filter(
                    (Report.status == ReportStatus.COMPLETED)
                    & (Report.created_at >= start)
                    & (Report.created_at < end)
                )
                .label("generated"),
            )
            .select_from(Report)
            .where(Report.requested_by == requested_by)
            .where(Report.deleted_at.is_(None))
        ).one()

        return ReportAnalytics(
            total=int(row.total or 0),
            completed=int(row.completed or 0),
            failed=int(row.failed or 0),
            in_progress=int(row.in_progress or 0),
            generated_in_window=int(row.generated or 0),
        )

    def recent_conversations(
        self, *, owner_id: uuid.UUID, limit: int
    ) -> list[Conversation]:
        """The caller's newest assistant threads.

        Ordered by last message rather than by creation, because a conversation
        resumed this morning is more recent than one opened last week and never
        returned to — and "recent conversations" is a working set, not a birth
        register.
        """
        statement = (
            select(Conversation)
            .where(Conversation.owner_id == owner_id)
            .where(Conversation.deleted_at.is_(None))
            .order_by(
                func.coalesce(Conversation.last_message_at, Conversation.created_at).desc(),
                Conversation.id.asc(),
            )
            .limit(limit)
        )
        return list(self._session.execute(statement).scalars().all())

    def conversation_analytics(
        self, *, owner_id: uuid.UUID, start: datetime, end: datetime
    ) -> ConversationAnalytics:
        """The caller's own assistant usage."""
        row = self._session.execute(
            select(
                func.count().label("total"),
                func.count()
                .filter(Conversation.status == ConversationStatus.ACTIVE)
                .label("active"),
                func.count()
                .filter(
                    (Conversation.created_at >= start) & (Conversation.created_at < end)
                )
                .label("started"),
                func.coalesce(func.sum(Conversation.message_count), 0).label("messages"),
            )
            .select_from(Conversation)
            .where(Conversation.owner_id == owner_id)
            .where(Conversation.deleted_at.is_(None))
        ).one()

        return ConversationAnalytics(
            total=int(row.total or 0),
            active=int(row.active or 0),
            started_in_window=int(row.started or 0),
            messages=int(row.messages or 0),
        )

    # --------------------------------------------------------------- helpers #

    @staticmethod
    def _scoped[T](statement: Select[T], visible_to: uuid.UUID | None) -> Select[T]:
        """Restrict a query over ``cases`` to what the caller may see."""
        return statement.where(_scope_predicate(visible_to))

    @staticmethod
    def _scoped_documents[T](
        statement: Select[T], visible_to: uuid.UUID | None
    ) -> Select[T]:
        """Restrict a query over ``documents`` to the caller's cases.

        A document is reachable exactly when its case is (see
        :mod:`services.document_access`), so the join is the whole of the rule and
        there is no second policy here to keep in step with the first.
        """
        return (
            statement.join(Case, Case.id == Document.case_id)
            .where(Document.deleted_at.is_(None))
            .where(_scope_predicate(visible_to))
        )

    @staticmethod
    def _current_ocr[T](statement: Select[T], visible_to: uuid.UUID | None) -> Select[T]:
        """Restrict an OCR query to the current version of each visible document."""
        return (
            statement.select_from(OcrResult)
            .join(Document, Document.id == OcrResult.document_id)
            .join(Case, Case.id == Document.case_id)
            .where(Document.deleted_at.is_(None))
            .where(OcrResult.document_version == Document.version)
            .where(_scope_predicate(visible_to))
        )

    @staticmethod
    def _current_index[T](
        statement: Select[T], visible_to: uuid.UUID | None
    ) -> Select[T]:
        """Restrict an index query to the current version of each visible document."""
        return (
            statement.select_from(DocumentIndex)
            .join(Document, Document.id == DocumentIndex.document_id)
            .join(Case, Case.id == Document.case_id)
            .where(Document.deleted_at.is_(None))
            .where(DocumentIndex.document_version == Document.version)
            .where(_scope_predicate(visible_to))
        )


def _scope_predicate(visible_to: uuid.UUID | None) -> ColumnElement[bool]:
    """The case-visibility clause, or an always-true one for a platform view.

    Returning a predicate rather than branching at every call site is what keeps
    the scope a single ``AND`` that no other filter can widen — the same shape
    :func:`~repositories.case.assigned_case_scope` has in Case Management. The
    unrestricted branch is a literal ``TRUE`` rather than "no clause at all",
    because the caller composes it into a ``where()`` unconditionally and an
    optional predicate would be an optional restriction.
    """
    if visible_to is None:
        return true()
    return assigned_case_scope(visible_to)


def _days(count: int) -> timedelta:
    """``timedelta(days=count)``, named so the date arithmetic reads as prose."""
    return timedelta(days=count)


def _value(raw: object) -> object:
    """The stored value of an enum column, whichever form the driver returned.

    SQLAlchemy hands back a Python enum member for a mapped ``Enum`` column and a
    plain string for a ``VARCHAR`` one. Both appear in this module — ``case_status``
    is an enum, ``document_category`` is a string on some backends — so the
    breakdowns normalize rather than assuming one.
    """
    return getattr(raw, "value", raw)


__all__ = [
    "CaseAnalytics",
    "ConversationAnalytics",
    "DashboardRepository",
    "DocumentAnalytics",
    "HearingSummary",
    "QueueDepths",
    "ReportAnalytics",
    "StorageUsage",
    "UserActivity",
]
