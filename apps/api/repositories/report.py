"""Report data access.

Single responsibility: reading and persisting :class:`~models.report.Report`
rows. No authorization rules, no retrieval, no generation, no rendering — those
belong to ``services/report_access.py``, ``services/rag.py``,
``services/report.py``, and ``services/report_export.py`` respectively.

Three things here are load-bearing rather than routine:

* **Every read takes an ``owner_id`` and puts it in the ``WHERE`` clause.**
  ``14-ai-report-agent.md`` requires that report history *"remain
  user-specific"* and that generated reports *"remain private"*, and this is
  where that is made true. There is deliberately **no method that resolves a
  report by identifier alone**, so no call site can forget to scope one — the
  same shape :mod:`repositories.conversation` has, and the reason neither
  feature needs a per-resource access module for *reading*.
* :meth:`ReportRepository.claim` is the platform's **concurrency guarantee** for
  this feature. It is a conditional ``UPDATE`` executed by the database, not a
  read-then-write in Python, which is what makes "only one worker may generate a
  report" true rather than likely — the same mechanism
  :meth:`~repositories.indexing.IndexingRepository.claim` uses.
* :meth:`ReportRepository.metrics` aggregates **in the database**, so the
  monitoring endpoint costs one query whatever the platform's history. Unlike
  search, RAG, and the assistant — which persist nothing and therefore count in
  the process — every figure ``14-ai-report-agent.md`` names is a property of
  these rows, so all six are exact and survive a restart.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Select, asc, desc, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import UnaryExpression

from models.report import Report, ReportStatus
from schemas.case import SortOrder
from schemas.report import ReportListQuery, ReportSortField


@dataclass(frozen=True, slots=True)
class ReportStatusCounts:
    """How many runs sit in each lifecycle state, plus the aggregates.

    Every figure ``14-ai-report-agent.md``'s "Monitoring" section names, in one
    value: generated reports, average generation time, export count, failed
    generations, average report size, and token usage.
    """

    pending: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0

    #: Mean wall-clock duration of *successful* runs, in milliseconds. Failed
    #: runs are excluded: a run that timed out against an unreachable provider
    #: contributes the timeout, and averaging that into "how long does a report
    #: take" answers a different question. Same reasoning as
    #: :class:`~repositories.indexing.IndexStatusCounts`.
    average_duration_ms: float | None = None

    #: Mean characters of generated prose per completed report — the spec's
    #: *"average report size"*. The size of the content, never the content.
    average_characters: float | None = None

    #: Sections written across every completed report, and how many of those the
    #: pipeline could ground in evidence. The report-level grounding rate is
    #: derived from the pair rather than stored, so the two cannot disagree.
    total_sections: int = 0
    grounded_sections: int = 0

    #: Exports served across every report, and how many reports have been
    #: exported at least once. Both, because "40 exports" over two reports and
    #: over forty are very different platforms.
    total_exports: int = 0
    exported_reports: int = 0

    #: Token usage summed over completed runs, when providers reported it.
    #: ``None`` rather than ``0`` when none ever has: zero would read as "this
    #: platform's reports are free" — the same distinction
    #: :class:`~services.rag_metrics.RagMetricsSnapshot` draws.
    total_prompt_tokens: int | None = None
    total_completion_tokens: int | None = None
    metered_reports: int = 0

    @property
    def total(self) -> int:
        """Every recorded run."""
        return self.pending + self.processing + self.completed + self.failed

    @property
    def finished(self) -> int:
        """Runs that reached a terminal state."""
        return self.completed + self.failed

    @property
    def active(self) -> int:
        """Runs queued or in flight right now."""
        return self.pending + self.processing


class ReportRepository:
    """Queries and mutations for the ``reports`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------- reading #

    def get(self, report_id: uuid.UUID, *, owner_id: uuid.UUID) -> Report | None:
        """Return one of this user's reports, or ``None``.

        **There is no unscoped variant of this method by design.** A report is
        one user's private work product, so a lookup that could return somebody
        else's is a query no part of the platform should be able to write.
        :meth:`get_for_worker` is the one exception, and it is named so that its
        single legitimate caller is obvious.
        """
        return self._session.execute(
            select(Report).where(
                Report.id == report_id,
                Report.requested_by == owner_id,
                Report.deleted_at.is_(None),
            )
        ).scalar_one_or_none()

    def get_for_worker(self, report_id: uuid.UUID) -> Report | None:
        """Return one report by identifier alone, for the background worker.

        The worker has no caller to scope against: it is executing work that was
        already authorized when it was queued, on a thread with no request. It is
        deliberately a **separate, differently named method** rather than an
        ``owner_id=None`` branch on :meth:`get`, so that an unscoped read cannot
        be reached by passing a null through the ordinary path — and so that
        every use of it is greppable.

        Deleted reports are included: a run already in flight when the user
        deleted it must still be able to record its own outcome rather than
        stranding the row at ``processing``.
        """
        return self._session.execute(
            select(Report).where(Report.id == report_id)
        ).scalar_one_or_none()

    def list_reports(
        self, query: ReportListQuery, *, owner_id: uuid.UUID
    ) -> tuple[list[Report], int]:
        """Return one page of **this user's** reports and the total matching the filters.

        The total is counted over the *filtered and scoped* set but before
        pagination, from the same clause as the page itself, so it cannot drift
        when a filter is added later and cannot reveal how many reports other
        users have generated.
        """
        filtered = self._apply_filters(select(Report), query, owner_id=owner_id)

        total = self._session.execute(
            select(func.count()).select_from(filtered.subquery())
        ).scalar_one()

        page = (
            filtered.order_by(*self._order_by(query)).offset(query.offset).limit(query.page_size)
        )
        return list(self._session.execute(page).scalars().unique().all()), total

    def count_active(self, *, owner_id: uuid.UUID) -> int:
        """How many of this user's reports are queued or generating right now.

        Read before a new run is accepted. Counted in the database rather than
        held in memory, because the ceiling has to hold across every API instance
        — a per-process counter would let a deployment behind a load balancer
        accept its limit once per instance.
        """
        return int(
            self._session.execute(
                select(func.count())
                .select_from(Report)
                .where(
                    Report.requested_by == owner_id,
                    Report.deleted_at.is_(None),
                    Report.status.in_([ReportStatus.PENDING, ReportStatus.PROCESSING]),
                )
            ).scalar_one()
        )

    def pending_reports(self, limit: int = 100) -> list[Report]:
        """Runs queued but not yet claimed, oldest first.

        Read at startup to re-queue work that was in flight when the process
        stopped: a job lives in an in-process queue, so an ungraceful shutdown
        would otherwise strand its row at ``pending`` forever with nothing left
        to pick it up. Not owner-scoped, and correctly so — it runs before any
        request exists.
        """
        return list(
            self._session.execute(
                select(Report)
                .where(Report.status == ReportStatus.PENDING, Report.deleted_at.is_(None))
                .order_by(asc(Report.created_at))
                .limit(limit)
            )
            .scalars()
            .unique()
            .all()
        )

    # ------------------------------------------------------------- writing #

    def create(self, report: Report) -> Report:
        """Persist a new run and return it with its generated columns populated."""
        self._session.add(report)
        self._session.commit()
        self._session.refresh(report)
        return report

    def save(self, report: Report) -> Report:
        """Persist pending changes to an existing run."""
        self._session.commit()
        self._session.refresh(report)
        return report

    def rollback(self) -> None:
        """Discard pending changes.

        Needed when a write fails part-way: a failed flush leaves the session
        unusable until the transaction is rolled back.
        """
        self._session.rollback()

    def soft_delete(self, report: Report) -> Report:
        """Withdraw a report without destroying it.

        Logical for the reason :class:`~models.report.Report` records: the
        report carries the citations of an analysis a lawyer may have acted on.
        """
        report.deleted_at = datetime.now(UTC)
        return self.save(report)

    def claim(self, report_id: uuid.UUID, *, started_at: datetime | None = None) -> bool:
        """Atomically move a run from ``pending`` to ``processing``.

        **This is the concurrency control the feature's idempotency rests on**,
        and it is a single conditional ``UPDATE`` rather than a check followed by
        a write. The difference matters: two workers reading "status is pending"
        and both writing "processing" is a race that no amount of care in Python
        closes, whereas ``WHERE status = 'pending'`` is evaluated by the database
        under a row lock, so exactly one of them updates a row and the other
        updates none.

        No distributed lock, no Redis key, and nothing to expire or leak — the
        row itself is the lock, held for exactly as long as the state says.

        The content columns are cleared as part of the claim, so a regenerated
        report never shows the previous run's sections beside the new run's
        progress bar.

        Returns:
            ``True`` if this caller claimed the run; ``False`` if it was already
            claimed, already finished, or does not exist.
        """
        stamp = started_at or datetime.now(UTC)

        # `Session.execute` is annotated as returning `Result`, which has no
        # `rowcount`; a DML statement always yields a `CursorResult`, which does.
        # Narrowed rather than ignored, because the count *is* the return value
        # of this method — the same note `IndexingRepository.claim` carries.
        outcome = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(Report)
                .where(Report.id == report_id, Report.status == ReportStatus.PENDING)
                .values(
                    status=ReportStatus.PROCESSING,
                    started_at=stamp,
                    finished_at=None,
                    duration_ms=None,
                    error_code=None,
                    error_message=None,
                    sections=[],
                    citations=[],
                    sections_completed=0,
                    attempt_count=Report.attempt_count + 1,
                    updated_at=stamp,
                )
            ),
        )
        self._session.commit()

        return bool(outcome.rowcount)

    def record_progress(self, report_id: uuid.UUID, *, completed: int, total: int) -> None:
        """Publish how far a run has got, without touching anything else.

        A targeted ``UPDATE`` rather than a mutation of the loaded object,
        deliberately: the worker holds a :class:`~models.report.Report` it is
        still building sections onto, and committing that object mid-run would
        publish half-written content to whoever is polling. This writes two
        integers and nothing else, which is exactly what the spec's *"progress
        should be queryable by the client"* needs and exactly what it does not.
        """
        self._session.execute(
            update(Report)
            .where(Report.id == report_id)
            .values(
                sections_completed=max(0, completed),
                sections_total=max(0, total),
                updated_at=datetime.now(UTC),
            )
        )
        self._session.commit()

    def record_export(self, report_id: uuid.UUID) -> None:
        """Count one export of a report.

        An increment computed by the database (``export_count + 1``) rather than
        in Python, so two concurrent downloads of the same report cannot both
        read 4 and both write 5.
        """
        stamp = datetime.now(UTC)
        self._session.execute(
            update(Report)
            .where(Report.id == report_id)
            .values(
                export_count=Report.export_count + 1,
                last_exported_at=stamp,
                updated_at=stamp,
            )
        )
        self._session.commit()

    # ---------------------------------------------------------- monitoring #

    def metrics(self, *, since: datetime | None = None) -> ReportStatusCounts:
        """Aggregate run counts, durations, sizes, exports, and token usage.

        Args:
            since: only count runs created at or after this instant. Absent means
                the platform's whole history.

        Computed in the database rather than by loading rows, for the same reason
        every list endpoint pushes its filters into SQL: the cost must not grow
        with the number of reports generated.

        Deleted reports are **included**, and that is not an oversight: the
        platform did the work, spent the tokens, and took the time, and a
        monitoring view whose figures fell when a user tidied their history would
        be measuring housekeeping rather than the platform.
        """
        counts = select(
            Report.status,
            # Deliberately not labelled ``count``: a SQLAlchemy ``Row`` is
            # tuple-like and already has a ``count`` *method*, so that label
            # would shadow it and read back as a bound method rather than a
            # number — silently, and only at runtime.
            func.count().label("report_count"),
            func.avg(Report.duration_ms).label("average_duration_ms"),
            func.avg(Report.character_count).label("average_characters"),
            func.sum(Report.sections_completed).label("total_sections"),
            func.sum(Report.grounded_sections).label("grounded_sections"),
            func.sum(Report.prompt_tokens).label("prompt_tokens"),
            func.sum(Report.completion_tokens).label("completion_tokens"),
            func.count(Report.total_tokens).label("metered"),
        )
        if since is not None:
            counts = counts.where(Report.created_at >= since)
        counts = counts.group_by(Report.status)

        totals: dict[ReportStatus, int] = {}
        duration: float | None = None
        characters: float | None = None
        sections = 0
        grounded = 0
        prompt_tokens = 0
        completion_tokens = 0
        metered = 0

        for row in self._session.execute(counts):
            state = ReportStatus(row.status)
            totals[state] = int(row.report_count)
            if state is not ReportStatus.COMPLETED:
                continue
            duration = float(row.average_duration_ms) if row.average_duration_ms is not None else None
            characters = (
                float(row.average_characters) if row.average_characters is not None else None
            )
            sections = int(row.total_sections or 0)
            grounded = int(row.grounded_sections or 0)
            prompt_tokens = int(row.prompt_tokens or 0)
            completion_tokens = int(row.completion_tokens or 0)
            metered = int(row.metered or 0)

        exports = self._session.execute(
            self._scoped(
                select(
                    func.coalesce(func.sum(Report.export_count), 0).label("total"),
                    func.count(Report.id)
                    .filter(Report.export_count > 0)
                    .label("exported"),
                ),
                since=since,
            )
        ).one()

        return ReportStatusCounts(
            pending=totals.get(ReportStatus.PENDING, 0),
            processing=totals.get(ReportStatus.PROCESSING, 0),
            completed=totals.get(ReportStatus.COMPLETED, 0),
            failed=totals.get(ReportStatus.FAILED, 0),
            average_duration_ms=round(duration, 2) if duration is not None else None,
            average_characters=round(characters, 2) if characters is not None else None,
            total_sections=sections,
            grounded_sections=grounded,
            total_exports=int(exports.total or 0),
            exported_reports=int(exports.exported or 0),
            total_prompt_tokens=prompt_tokens if metered > 0 else None,
            total_completion_tokens=completion_tokens if metered > 0 else None,
            metered_reports=metered,
        )

    def type_breakdown(self, *, since: datetime | None = None) -> dict[str, int]:
        """How many reports each type accounts for.

        The half of the monitoring picture that says *what* people are
        generating, which is what decides whether a sixth template is worth
        writing and which of the five is not earning its place.
        """
        statement = self._scoped(
            select(Report.report_type, func.count().label("count")), since=since
        ).group_by(Report.report_type)

        return {str(report_type.value): int(count) for report_type, count in self._session.execute(statement)}

    def failure_breakdown(self, *, since: datetime | None = None) -> dict[str, int]:
        """How many failed runs each error code accounts for.

        The operational half: a failure *rate* says something is wrong, and this
        says what — an unreachable vector database, a missing model credential,
        and a case with no indexed documents produce the same rate and need
        entirely different responses.
        """
        statement = self._scoped(
            select(Report.error_code, func.count().label("count")), since=since
        ).where(Report.status == ReportStatus.FAILED)
        statement = statement.group_by(Report.error_code)

        return {
            (code or "unknown"): int(count) for code, count in self._session.execute(statement)
        }

    # ------------------------------------------------------------- helpers #

    @staticmethod
    def _scoped(statement: Select[Any], *, since: datetime | None) -> Select[Any]:
        """Apply the monitoring window to an aggregate."""
        if since is not None:
            return statement.where(Report.created_at >= since)
        return statement

    @staticmethod
    def _apply_filters(
        statement: Select[tuple[Report]],
        query: ReportListQuery,
        *,
        owner_id: uuid.UUID,
    ) -> Select[tuple[Report]]:
        """Narrow a report query by its owner and the requested filters.

        The owner clause is applied **first and unconditionally**, and everything
        else combines with it using AND — so no combination of filters can widen
        the set beyond the caller's own history. Deleted reports are excluded
        here rather than by each caller, so a soft-deleted report cannot be
        resurrected by a filter nobody thought about.
        """
        statement = statement.where(
            Report.requested_by == owner_id, Report.deleted_at.is_(None)
        )

        if query.status is not None:
            statement = statement.where(Report.status == query.status)

        if query.report_type is not None:
            statement = statement.where(Report.report_type == query.report_type)

        if query.case_id is not None:
            statement = statement.where(Report.case_id == query.case_id)

        if query.search:
            # Matches the *title* only, and the title is derived from the report
            # type and the case number. Searching the sections would mean an
            # unindexed scan over generated prose about a client's matter — and
            # the platform already has a feature for searching content, with a
            # vector index behind it and its own permission.
            pattern = f"%{query.search.lower()}%"
            statement = statement.where(or_(func.lower(Report.title).like(pattern)))

        return statement

    @staticmethod
    def _order_by(query: ReportListQuery) -> tuple[UnaryExpression[object], ...]:
        """Build the ORDER BY clause for the requested sort.

        The primary key is appended as a tiebreaker, for the same reason as in
        every other repository here: rows sharing a sort value — every report
        queued in the same second, every report that has not finished — could
        otherwise come back in a different order per request and be duplicated or
        skipped across page boundaries.
        """
        direction = asc if query.sort_order is SortOrder.ASC else desc

        column = {
            ReportSortField.CREATED_AT: Report.created_at,
            ReportSortField.UPDATED_AT: Report.updated_at,
            ReportSortField.FINISHED_AT: Report.finished_at,
            ReportSortField.DURATION_MS: Report.duration_ms,
            ReportSortField.TITLE: Report.title,
            ReportSortField.STATUS: Report.status,
            ReportSortField.REPORT_TYPE: Report.report_type,
        }[query.sort_by]

        return (direction(column), asc(Report.id))


__all__ = ["ReportRepository", "ReportStatusCounts"]
