"""OCR data access.

Single responsibility: reading and persisting :class:`~models.ocr.OcrResult` and
:class:`~models.ocr.OcrPage` rows. No authorization rules, no engine calls, and
no lifecycle decisions — those belong to ``services/ocr_access.py``,
``services/ocr_engine.py``, and ``services/ocr.py`` respectively.

Two things here are load-bearing rather than routine:

* :meth:`OcrRepository.claim` is the platform's **concurrency guarantee**. It is
  a conditional ``UPDATE`` executed by the database, not a read-then-write in
  Python, which is what makes "only one worker may process a document at a time"
  true rather than likely.
* :meth:`OcrRepository.metrics` aggregates **in the database**, so the
  monitoring endpoint costs one query whatever the platform's history — the same
  reason every list query in this codebase pushes its filters into SQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Select, asc, delete, desc, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import UnaryExpression

from models.document import Document
from models.ocr import OcrPage, OcrResult, OcrStatus
from schemas.case import SortOrder
from schemas.ocr import OcrListQuery, OcrSortField


@dataclass(frozen=True, slots=True)
class OcrStatusCounts:
    """How many runs sit in each lifecycle state, plus the timing aggregate."""

    pending: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0
    #: Mean wall-clock duration of *completed* runs, in milliseconds. Failed runs
    #: are excluded: a run that timed out contributes the timeout, and a run that
    #: died on a missing binary contributes nothing meaningful — averaging either
    #: into "how long does extraction take" would answer a different question.
    average_duration_ms: float | None = None

    @property
    def total(self) -> int:
        """Every recorded run."""
        return self.pending + self.processing + self.completed + self.failed

    @property
    def finished(self) -> int:
        """Runs that reached a terminal state."""
        return self.completed + self.failed


class OcrRepository:
    """Queries and mutations for the ``ocr_results`` and ``ocr_pages`` tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------- reading #

    def get_by_id(self, result_id: uuid.UUID) -> OcrResult | None:
        """Return one run by its own identifier, or ``None``."""
        return self._session.execute(
            select(OcrResult).where(OcrResult.id == result_id)
        ).scalar_one_or_none()

    def get_for_version(self, document_id: uuid.UUID, version: int) -> OcrResult | None:
        """Return the run for one version of one document, or ``None``.

        The natural key: a document version has at most one run, which is what
        the unique constraint on the table enforces and what makes a retry
        idempotent.
        """
        return self._session.execute(
            select(OcrResult).where(
                OcrResult.document_id == document_id,
                OcrResult.document_version == version,
            )
        ).scalar_one_or_none()

    def list_for_document(self, document_id: uuid.UUID) -> list[OcrResult]:
        """Every run recorded for a document, oldest version first.

        A replacement produces a new version and therefore a new run, while the
        previous version's text stays valid — so a document accumulates one run
        per version, and this is the history of them.
        """
        return list(
            self._session.execute(
                select(OcrResult)
                .where(OcrResult.document_id == document_id)
                .order_by(asc(OcrResult.document_version))
            )
            .scalars()
            .unique()
            .all()
        )

    def list_results(
        self, query: OcrListQuery, *, visible_to: uuid.UUID | None = None
    ) -> tuple[list[OcrResult], int]:
        """Return one page of OCR runs and the total matching the filters.

        Args:
            query: filters, sort, and page.
            visible_to: when given, restrict the result to runs whose document's
                case this user is assigned to. ``None`` means no restriction —
                reserved for callers holding ``cases:view-all``, which the
                service decides.

        The total is counted over the *filtered and scoped* set but before
        pagination, from the same clause as the page itself, so it cannot drift
        when a filter is added later and cannot reveal how many runs a lawyer is
        not allowed to see.
        """
        filtered = self._apply_filters(select(OcrResult), query, visible_to=visible_to)

        total = self._session.execute(
            select(func.count()).select_from(filtered.subquery())
        ).scalar_one()

        page = (
            filtered.order_by(*self._order_by(query)).offset(query.offset).limit(query.page_size)
        )
        return list(self._session.execute(page).scalars().unique().all()), total

    def pending_results(self, limit: int = 100) -> list[OcrResult]:
        """Runs queued but not yet claimed, oldest first.

        Read at startup to re-queue work that was in flight when the process
        stopped: a job lives in an in-process queue, so an ungraceful shutdown
        would otherwise strand its row at ``pending`` forever with nothing left
        to pick it up.
        """
        return list(
            self._session.execute(
                select(OcrResult)
                .where(OcrResult.status == OcrStatus.PENDING)
                .order_by(asc(OcrResult.created_at))
                .limit(limit)
            )
            .scalars()
            .unique()
            .all()
        )

    # ------------------------------------------------------------- writing #

    def create(self, result: OcrResult) -> OcrResult:
        """Persist a new run and return it with its generated columns populated."""
        self._session.add(result)
        self._session.commit()
        self._session.refresh(result)
        return result

    def save(self, result: OcrResult) -> OcrResult:
        """Persist pending changes to an existing run."""
        self._session.commit()
        self._session.refresh(result)
        return result

    def rollback(self) -> None:
        """Discard pending changes.

        Needed when a write fails part-way: a failed flush leaves the session
        unusable until the transaction is rolled back.
        """
        self._session.rollback()

    def claim(self, result_id: uuid.UUID, *, started_at: datetime | None = None) -> bool:
        """Atomically move a run from ``pending`` to ``processing``.

        **This is the concurrency control the spec asks for**, and it is a single
        conditional ``UPDATE`` rather than a check followed by a write. The
        difference matters: two workers reading "status is pending" and both
        writing "processing" is a race that no amount of care in Python closes,
        whereas ``WHERE status = 'pending'`` is evaluated by the database under
        a row lock, so exactly one of them updates a row and the other updates
        none.

        No distributed lock, no Redis key, and nothing to expire or leak — the
        row itself is the lock, and it is held for exactly as long as the state
        says it is.

        Returns:
            ``True`` if this caller claimed the run; ``False`` if it was already
            claimed, already finished, or does not exist.
        """
        stamp = started_at or datetime.now(UTC)

        # `Session.execute` is annotated as returning `Result`, which has no
        # `rowcount`; a DML statement always yields a `CursorResult`, which does.
        # Narrowed rather than ignored, because the count *is* the return value
        # of this method and silencing the type here would silence a real change.
        outcome = cast("CursorResult[Any]", self._session.execute(
            update(OcrResult)
            .where(OcrResult.id == result_id, OcrResult.status == OcrStatus.PENDING)
            .values(
                status=OcrStatus.PROCESSING,
                started_at=stamp,
                finished_at=None,
                duration_ms=None,
                error_code=None,
                error_message=None,
                attempt_count=OcrResult.attempt_count + 1,
                updated_at=stamp,
            )
        ))
        self._session.commit()

        return bool(outcome.rowcount)

    def replace_pages(self, result: OcrResult, pages: Sequence[OcrPage]) -> None:
        """Swap a run's extracted text for a fresh set of pages.

        Deleted and re-inserted rather than merged, because a retry is a *new
        reading* of the same bytes: a merge would leave page 7 from an attempt
        that produced nine pages sitting behind an attempt that produced five,
        and the run would report text it did not extract.

        The delete is issued as a bulk statement rather than by emptying the
        relationship collection, so re-running extraction over a 100-page
        document is one statement instead of a hundred.
        """
        self._session.execute(delete(OcrPage).where(OcrPage.ocr_result_id == result.id))
        # The collection is already loaded (``selectin``), so it has to be told
        # about the bulk delete or the next flush would try to re-persist the
        # rows it still holds.
        result.pages.clear()
        for page in pages:
            page.ocr_result_id = result.id
            self._session.add(page)
            result.pages.append(page)

    # ---------------------------------------------------------- monitoring #

    def metrics(self, *, since: datetime | None = None) -> OcrStatusCounts:
        """Aggregate run counts and mean duration, in one query.

        Args:
            since: only count runs created at or after this instant. Absent means
                the platform's whole history.

        Computed in the database rather than by loading rows, for the same reason
        every list endpoint pushes its filters into SQL: the cost must not grow
        with the number of documents processed.
        """
        counts = select(
            OcrResult.status,
            func.count().label("count"),
            func.avg(OcrResult.duration_ms).label("average_duration_ms"),
        )
        if since is not None:
            counts = counts.where(OcrResult.created_at >= since)
        counts = counts.group_by(OcrResult.status)

        totals: dict[OcrStatus, int] = {}
        completed_duration: float | None = None

        for status, count, average in self._session.execute(counts):
            totals[OcrStatus(status)] = int(count)
            if OcrStatus(status) is OcrStatus.COMPLETED and average is not None:
                completed_duration = float(average)

        return OcrStatusCounts(
            pending=totals.get(OcrStatus.PENDING, 0),
            processing=totals.get(OcrStatus.PROCESSING, 0),
            completed=totals.get(OcrStatus.COMPLETED, 0),
            failed=totals.get(OcrStatus.FAILED, 0),
            average_duration_ms=(
                round(completed_duration, 2) if completed_duration is not None else None
            ),
        )

    def failure_breakdown(self, *, since: datetime | None = None) -> dict[str, int]:
        """How many failed runs each error code accounts for.

        The operational half of the monitoring picture: a failure *rate* says
        something is wrong, and this says what — a missing Tesseract install and
        a stack of unreadable scans produce the same rate and need entirely
        different responses.
        """
        statement = select(OcrResult.error_code, func.count().label("count")).where(
            OcrResult.status == OcrStatus.FAILED
        )
        if since is not None:
            statement = statement.where(OcrResult.created_at >= since)
        statement = statement.group_by(OcrResult.error_code)

        return {
            (code or "unknown"): int(count)
            for code, count in self._session.execute(statement)
        }

    # ------------------------------------------------------------- helpers #

    @staticmethod
    def _apply_filters(
        statement: Select[tuple[OcrResult]],
        query: OcrListQuery,
        *,
        visible_to: uuid.UUID | None,
    ) -> Select[tuple[OcrResult]]:
        """Narrow an OCR query by the case scope and the filters.

        Everything combines with AND, and the scope — being one more AND —
        cannot be widened by any combination of the rest.
        """
        if visible_to is not None:
            # An OCR run is reachable exactly when its document is, which is
            # exactly when the document's case is. Expressed as a subquery
            # against the *document* scope rather than a second copy of the case
            # predicate, so the three cannot drift apart.
            from models.case import Case
            from repositories.case import assigned_case_scope

            statement = statement.where(
                OcrResult.document_id.in_(
                    select(Document.id).where(
                        Document.case_id.in_(select(Case.id).where(assigned_case_scope(visible_to)))
                    )
                )
            )

        if query.status is not None:
            statement = statement.where(OcrResult.status == query.status)

        if query.document_id is not None:
            statement = statement.where(OcrResult.document_id == query.document_id)

        if query.case_id is not None:
            statement = statement.where(
                OcrResult.document_id.in_(
                    select(Document.id).where(Document.case_id == query.case_id)
                )
            )

        if query.error_code is not None:
            statement = statement.where(OcrResult.error_code == query.error_code)

        return statement

    @staticmethod
    def _order_by(query: OcrListQuery) -> tuple[UnaryExpression[object], ...]:
        """Build the ORDER BY clause for the requested sort.

        The primary key is appended as a tiebreaker, for the same reason as in
        every other repository here: rows sharing a sort value — every run queued
        in the same second, every run that has not started — could otherwise come
        back in a different order per request and be duplicated or skipped across
        page boundaries.
        """
        direction = asc if query.sort_order is SortOrder.ASC else desc

        column = {
            OcrSortField.CREATED_AT: OcrResult.created_at,
            OcrSortField.STARTED_AT: OcrResult.started_at,
            OcrSortField.FINISHED_AT: OcrResult.finished_at,
            OcrSortField.DURATION_MS: OcrResult.duration_ms,
            OcrSortField.STATUS: OcrResult.status,
        }[query.sort_by]

        return (direction(column), asc(OcrResult.id))
