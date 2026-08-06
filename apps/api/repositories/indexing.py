"""Document-index data access.

Single responsibility: reading and persisting :class:`~models.indexing.DocumentIndex`
rows. No authorization rules, no chunking, no embedding, no vector writes — those
belong to ``services/indexing_access.py``, ``services/chunking.py``,
``services/embedding.py``, and ``services/vector_store.py`` respectively.

Two things here are load-bearing rather than routine, and both mirror
:mod:`repositories.ocr` because they solve the same problems:

* :meth:`IndexingRepository.claim` is the platform's **concurrency guarantee**.
  It is a conditional ``UPDATE`` executed by the database, not a read-then-write
  in Python, which is what makes "only one worker may index a document version at
  a time" true rather than likely.
* :meth:`IndexingRepository.metrics` aggregates **in the database**, so the
  monitoring endpoint costs one query whatever the platform's history — the same
  reason every list query in this codebase pushes its filters into SQL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Select, asc, desc, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import UnaryExpression

from models.indexing import DocumentIndex, IndexStatus
from schemas.case import SortOrder
from schemas.indexing import IndexListQuery, IndexSortField


@dataclass(frozen=True, slots=True)
class IndexStatusCounts:
    """How many runs sit in each lifecycle state, plus the two aggregates."""

    pending: int = 0
    indexing: int = 0
    indexed: int = 0
    failed: int = 0
    #: Mean wall-clock duration of *successful* runs, in milliseconds. Failed
    #: runs are excluded: a run that timed out contributes the timeout, and a run
    #: that died on a missing model contributes nothing meaningful — averaging
    #: either into "how long does indexing take" would answer a different
    #: question. Same reasoning as :class:`~repositories.ocr.OcrStatusCounts`.
    average_duration_ms: float | None = None
    #: Chunks across every successful run — the spec's "indexed chunks" metric.
    #: Summed over ``INDEXED`` rows only, because a failed run's partial count
    #: describes vectors that may or may not still be in Qdrant.
    total_chunks: int = 0

    @property
    def total(self) -> int:
        """Every recorded run."""
        return self.pending + self.indexing + self.indexed + self.failed

    @property
    def finished(self) -> int:
        """Runs that reached a terminal state."""
        return self.indexed + self.failed


class IndexingRepository:
    """Queries and mutations for the ``document_indexes`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------- reading #

    def get_by_id(self, index_id: uuid.UUID) -> DocumentIndex | None:
        """Return one run by its own identifier, or ``None``."""
        return self._session.execute(
            select(DocumentIndex).where(DocumentIndex.id == index_id)
        ).scalar_one_or_none()

    def get_for_version(self, document_id: uuid.UUID, version: int) -> DocumentIndex | None:
        """Return the index for one version of one document, or ``None``.

        The natural key: a document version has at most one index, which is what
        the unique constraint on the table enforces and what makes re-indexing
        idempotent.
        """
        return self._session.execute(
            select(DocumentIndex).where(
                DocumentIndex.document_id == document_id,
                DocumentIndex.document_version == version,
            )
        ).scalar_one_or_none()

    def list_for_document(self, document_id: uuid.UUID) -> list[DocumentIndex]:
        """Every index recorded for a document, oldest version first.

        A replacement produces a new version and therefore a new index, while the
        previous version's vectors stay valid for as long as that version is
        readable — so a document accumulates one index per version, and this is
        the history of them.
        """
        return list(
            self._session.execute(
                select(DocumentIndex)
                .where(DocumentIndex.document_id == document_id)
                .order_by(asc(DocumentIndex.document_version))
            )
            .scalars()
            .unique()
            .all()
        )

    def list_indexes(
        self, query: IndexListQuery, *, visible_to: uuid.UUID | None = None
    ) -> tuple[list[DocumentIndex], int]:
        """Return one page of indexing runs and the total matching the filters.

        Args:
            query: filters, sort, and page.
            visible_to: when given, restrict the result to indexes whose case
                this user is assigned to. ``None`` means no restriction —
                reserved for callers holding ``cases:view-all``, which the
                service decides.

        The total is counted over the *filtered and scoped* set but before
        pagination, from the same clause as the page itself, so it cannot drift
        when a filter is added later and cannot reveal how many documents a
        lawyer is not allowed to see.
        """
        filtered = self._apply_filters(select(DocumentIndex), query, visible_to=visible_to)

        total = self._session.execute(
            select(func.count()).select_from(filtered.subquery())
        ).scalar_one()

        page = (
            filtered.order_by(*self._order_by(query)).offset(query.offset).limit(query.page_size)
        )
        return list(self._session.execute(page).scalars().unique().all()), total

    def pending_indexes(self, limit: int = 100) -> list[DocumentIndex]:
        """Runs queued but not yet claimed, oldest first.

        Read at startup to re-queue work that was in flight when the process
        stopped: a job lives in an in-process queue, so an ungraceful shutdown
        would otherwise strand its row at ``pending`` forever with nothing left
        to pick it up.
        """
        return list(
            self._session.execute(
                select(DocumentIndex)
                .where(DocumentIndex.status == IndexStatus.PENDING)
                .order_by(asc(DocumentIndex.created_at))
                .limit(limit)
            )
            .scalars()
            .unique()
            .all()
        )

    # ------------------------------------------------------------- writing #

    def create(self, index: DocumentIndex) -> DocumentIndex:
        """Persist a new run and return it with its generated columns populated."""
        self._session.add(index)
        self._session.commit()
        self._session.refresh(index)
        return index

    def save(self, index: DocumentIndex) -> DocumentIndex:
        """Persist pending changes to an existing run."""
        self._session.commit()
        self._session.refresh(index)
        return index

    def rollback(self) -> None:
        """Discard pending changes.

        Needed when a write fails part-way: a failed flush leaves the session
        unusable until the transaction is rolled back.
        """
        self._session.rollback()

    def claim(self, index_id: uuid.UUID, *, started_at: datetime | None = None) -> bool:
        """Atomically move a run from ``pending`` to ``indexing``.

        **This is the concurrency control the spec's idempotency requirement
        rests on**, and it is a single conditional ``UPDATE`` rather than a check
        followed by a write. The difference matters: two workers reading "status
        is pending" and both writing "indexing" is a race that no amount of care
        in Python closes, whereas ``WHERE status = 'pending'`` is evaluated by
        the database under a row lock, so exactly one of them updates a row and
        the other updates none.

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
        outcome = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(DocumentIndex)
                .where(
                    DocumentIndex.id == index_id,
                    DocumentIndex.status == IndexStatus.PENDING,
                )
                .values(
                    status=IndexStatus.INDEXING,
                    started_at=stamp,
                    finished_at=None,
                    duration_ms=None,
                    error_code=None,
                    error_message=None,
                    attempt_count=DocumentIndex.attempt_count + 1,
                    updated_at=stamp,
                )
            ),
        )
        self._session.commit()

        return bool(outcome.rowcount)

    # ---------------------------------------------------------- monitoring #

    def metrics(self, *, since: datetime | None = None) -> IndexStatusCounts:
        """Aggregate run counts, mean duration, and total chunks, in one query.

        Args:
            since: only count runs created at or after this instant. Absent means
                the platform's whole history.

        Computed in the database rather than by loading rows, for the same reason
        every list endpoint pushes its filters into SQL: the cost must not grow
        with the number of documents indexed.
        """
        counts = select(
            DocumentIndex.status,
            func.count().label("count"),
            func.avg(DocumentIndex.duration_ms).label("average_duration_ms"),
            func.sum(DocumentIndex.chunk_count).label("total_chunks"),
        )
        if since is not None:
            counts = counts.where(DocumentIndex.created_at >= since)
        counts = counts.group_by(DocumentIndex.status)

        totals: dict[IndexStatus, int] = {}
        indexed_duration: float | None = None
        indexed_chunks = 0

        for status, count, average, chunks in self._session.execute(counts):
            state = IndexStatus(status)
            totals[state] = int(count)
            if state is IndexStatus.INDEXED:
                if average is not None:
                    indexed_duration = float(average)
                indexed_chunks = int(chunks or 0)

        return IndexStatusCounts(
            pending=totals.get(IndexStatus.PENDING, 0),
            indexing=totals.get(IndexStatus.INDEXING, 0),
            indexed=totals.get(IndexStatus.INDEXED, 0),
            failed=totals.get(IndexStatus.FAILED, 0),
            average_duration_ms=(
                round(indexed_duration, 2) if indexed_duration is not None else None
            ),
            total_chunks=indexed_chunks,
        )

    def failure_breakdown(self, *, since: datetime | None = None) -> dict[str, int]:
        """How many failed runs each error code accounts for.

        The operational half of the monitoring picture: a failure *rate* says
        something is wrong, and this says what — an unreachable Qdrant and a
        stack of documents with no extracted text produce the same rate and need
        entirely different responses.
        """
        statement = select(DocumentIndex.error_code, func.count().label("count")).where(
            DocumentIndex.status == IndexStatus.FAILED
        )
        if since is not None:
            statement = statement.where(DocumentIndex.created_at >= since)
        statement = statement.group_by(DocumentIndex.error_code)

        return {
            (code or "unknown"): int(count) for code, count in self._session.execute(statement)
        }

    # ------------------------------------------------------------- helpers #

    @staticmethod
    def _apply_filters(
        statement: Select[tuple[DocumentIndex]],
        query: IndexListQuery,
        *,
        visible_to: uuid.UUID | None,
    ) -> Select[tuple[DocumentIndex]]:
        """Narrow an index query by the case scope and the filters.

        Everything combines with AND, and the scope — being one more AND —
        cannot be widened by any combination of the rest.
        """
        if visible_to is not None:
            # An index is reachable exactly when its document is, which is
            # exactly when the document's case is. Expressed against the shared
            # `assigned_case_scope` clause rather than a second copy of the case
            # predicate, so the three cannot drift apart. The `case_id` column on
            # this table is what lets it be one subquery rather than two.
            from models.case import Case
            from repositories.case import assigned_case_scope

            statement = statement.where(
                DocumentIndex.case_id.in_(
                    select(Case.id).where(assigned_case_scope(visible_to))
                )
            )

        if query.status is not None:
            statement = statement.where(DocumentIndex.status == query.status)

        if query.document_id is not None:
            statement = statement.where(DocumentIndex.document_id == query.document_id)

        if query.case_id is not None:
            statement = statement.where(DocumentIndex.case_id == query.case_id)

        if query.error_code is not None:
            statement = statement.where(DocumentIndex.error_code == query.error_code)

        if query.embedding_model is not None:
            # The filter that makes "which documents still need re-indexing after
            # a model change?" answerable — `ai-architecture.md` says changing
            # models requires re-indexing, and this is how an operator finds the
            # stragglers.
            statement = statement.where(DocumentIndex.embedding_model == query.embedding_model)

        return statement

    @staticmethod
    def _order_by(query: IndexListQuery) -> tuple[UnaryExpression[object], ...]:
        """Build the ORDER BY clause for the requested sort.

        The primary key is appended as a tiebreaker, for the same reason as in
        every other repository here: rows sharing a sort value — every run queued
        in the same second, every run that has not started — could otherwise come
        back in a different order per request and be duplicated or skipped across
        page boundaries.
        """
        direction = asc if query.sort_order is SortOrder.ASC else desc

        column = {
            IndexSortField.CREATED_AT: DocumentIndex.created_at,
            IndexSortField.STARTED_AT: DocumentIndex.started_at,
            IndexSortField.FINISHED_AT: DocumentIndex.finished_at,
            IndexSortField.DURATION_MS: DocumentIndex.duration_ms,
            IndexSortField.CHUNK_COUNT: DocumentIndex.chunk_count,
            IndexSortField.STATUS: DocumentIndex.status,
        }[query.sort_by]

        return (direction(column), asc(DocumentIndex.id))
