"""Document data access.

Single responsibility: reading and persisting :class:`~models.document.Document`
and :class:`~models.document.DocumentVersion` rows. No authorization rules, no
storage calls, and no lifecycle decisions — those belong to
``services/document.py``, ``services/document_access.py``, and
``services/document_storage.py`` respectively.

Search, filtering, sorting, and pagination all execute **in the database**, so
the cost of a page does not grow with the number of documents on the platform.
The one authorization concern that has to live here is the case scope:
restricting rows in Python would mean fetching every document in order to hide
most of them, and would make the page totals count documents the caller is not
entitled to know exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, time
from typing import Any

from sqlalchemy import Case as SqlCase
from sqlalchemy import Select, asc, case, desc, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import UnaryExpression

from core.documents import CATEGORY_RANK
from models.case import Case
from models.document import Document, DocumentCategory, DocumentVersion
from repositories.case import assigned_case_scope
from schemas.case import SortOrder
from schemas.document import DocumentListQuery, DocumentSortField


class DocumentRepository:
    """Queries and mutations for the ``documents`` and ``document_versions`` tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------- reading #

    def get_by_id(self, document_id: uuid.UUID, *, include_deleted: bool = False) -> Document | None:
        """Return the document with this id, or ``None``.

        Soft-deleted documents are excluded by default: everything that serves a
        document to a client must treat a deletion as final, and making that the
        *default* means an endpoint cannot expose one by forgetting a filter.
        ``include_deleted`` exists for the paths that legitimately need the row —
        the list endpoint's opt-in, and any future cleanup job.
        """
        statement = select(Document).where(Document.id == document_id)
        if not include_deleted:
            statement = statement.where(Document.deleted_at.is_(None))
        return self._session.execute(statement).scalar_one_or_none()

    def list_documents(
        self,
        query: DocumentListQuery,
        *,
        visible_to: uuid.UUID | None = None,
    ) -> tuple[list[Document], int]:
        """Return one page of documents and the total number matching the filters.

        Args:
            query: search term, filters, sort, and page.
            visible_to: when given, restrict the result to documents whose case
                this user is assigned to. ``None`` means no restriction —
                reserved for callers holding ``cases:view-all``, which the
                service decides.

        The total is counted over the *filtered and scoped* set but before
        pagination, from the same clause as the page itself, so it cannot drift
        when a filter is added later — and, more importantly, cannot reveal how
        many documents a lawyer is not allowed to see.
        """
        filtered = self._apply_filters(select(Document), query, visible_to=visible_to)

        total = self._session.execute(
            select(func.count()).select_from(filtered.subquery())
        ).scalar_one()

        page = (
            filtered.order_by(*self._order_by(query)).offset(query.offset).limit(query.page_size)
        )
        return list(self._session.execute(page).scalars().unique().all()), total

    def get_version(self, document_id: uuid.UUID, version: int) -> DocumentVersion | None:
        """Return one version of a document, or ``None``."""
        statement = select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version == version,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def highest_version(self, document_id: uuid.UUID) -> int:
        """The largest version number recorded for a document, or ``0`` if none.

        Read from ``document_versions`` rather than from ``documents.version``
        because the history is the authority: a version row is never deleted, so
        the next number can never collide with one already used.
        """
        statement = select(func.max(DocumentVersion.version)).where(
            DocumentVersion.document_id == document_id
        )
        return self._session.execute(statement).scalar_one() or 0

    # ------------------------------------------------------------- writing #

    def create(self, document: Document) -> Document:
        """Persist a new document and return it with its generated columns populated."""
        self._session.add(document)
        self._session.commit()
        self._session.refresh(document)
        return document

    def save(self, document: Document) -> Document:
        """Persist pending changes to an existing document."""
        self._session.commit()
        self._session.refresh(document)
        return document

    def rollback(self) -> None:
        """Discard pending changes.

        Needed when a write fails part-way: a failed flush leaves the session
        unusable until the transaction is rolled back.
        """
        self._session.rollback()

    # ------------------------------------------------------------- helpers #

    @staticmethod
    def _apply_filters(
        statement: Select[tuple[Document]],
        query: DocumentListQuery,
        *,
        visible_to: uuid.UUID | None,
    ) -> Select[tuple[Document]]:
        """Narrow a document query by the case scope, search term, and filters.

        Everything combines with AND, so the filters are freely combinable
        exactly as the spec requires — and the scope, being one more AND, cannot
        be widened by any combination of them.
        """
        if visible_to is not None:
            # A document is reachable exactly when its case is, so the scope is
            # the case scope, imported rather than restated. A subquery rather
            # than a join keeps the count query identical in shape to the page
            # query and cannot multiply rows.
            statement = statement.where(
                Document.case_id.in_(select(Case.id).where(assigned_case_scope(visible_to)))
            )

        if not query.include_deleted:
            statement = statement.where(Document.deleted_at.is_(None))

        if query.search:
            # ILIKE with escaped wildcards: a term containing % or _ must match
            # those characters literally rather than turning into a pattern.
            pattern = f"%{_escape_like(query.search)}%"
            clauses = [
                Document.original_filename.ilike(pattern, escape="\\"),
                Document.description.ilike(pattern, escape="\\"),
            ]

            # Category is a PostgreSQL enum, which ILIKE cannot be applied to
            # without a cast. Resolving the term to matching category *values* in
            # Python keeps the SQL an ordinary IN and works identically on every
            # dialect — the set is eight members, so the cost is nil.
            matching = _categories_matching(query.search)
            if matching:
                clauses.append(Document.category.in_(matching))

            statement = statement.where(or_(*clauses))

        if query.case_id is not None:
            statement = statement.where(Document.case_id == query.case_id)

        if query.category is not None:
            statement = statement.where(Document.category == query.category)

        if query.uploaded_by is not None:
            statement = statement.where(Document.uploaded_by == query.uploaded_by)

        if query.file_extension is not None:
            statement = statement.where(Document.file_extension == query.file_extension)

        # The bounds are calendar dates but the column is a timestamp, so the
        # upper bound has to cover the whole day — `<= 2026-07-30` must include a
        # document uploaded at 14:32 that day, which `<= 2026-07-30 00:00` would
        # silently exclude.
        if query.uploaded_from is not None:
            statement = statement.where(
                Document.created_at >= datetime.combine(query.uploaded_from, time.min)
            )
        if query.uploaded_to is not None:
            statement = statement.where(
                Document.created_at <= datetime.combine(query.uploaded_to, time.max)
            )

        return statement

    @classmethod
    def _order_by(cls, query: DocumentListQuery) -> tuple[UnaryExpression[object], ...]:
        """Build the ORDER BY clause for the requested sort.

        The primary key is appended as a tiebreaker: without it, rows sharing a
        sort value — every document in the same category, every document at
        version 1 — could come back in a different order on each request and be
        duplicated or skipped across page boundaries. Invisible until the case
        outgrows one page.
        """
        direction = asc if query.sort_order is SortOrder.ASC else desc

        # `Any` because the mapping mixes plain columns with the category CASE
        # expression; SQLAlchemy's own union of the two is not assignable to a
        # single annotation, and nothing here depends on the element type.
        columns: Sequence[Any] = {
            DocumentSortField.CREATED_AT: (Document.created_at,),
            DocumentSortField.ORIGINAL_FILENAME: (func.lower(Document.original_filename),),
            DocumentSortField.FILE_SIZE: (Document.file_size,),
            DocumentSortField.CATEGORY: (cls._category_rank(),),
            DocumentSortField.VERSION: (Document.version,),
        }[query.sort_by]

        return (*(direction(column) for column in columns), asc(Document.id))

    @staticmethod
    def _category_rank() -> SqlCase[int]:
        """Map each category onto its sort weight.

        Ordering by the stored value would sort alphabetically, which buries
        "Other" in the middle of the list and separates the two court-facing
        kinds. The weights come from :data:`~core.documents.CATEGORY_RANK`.

        Built as a *searched* CASE (``WHEN category = :value``) rather than the
        shorthand ``case({...}, value=Document.category)``. The shorthand binds
        its keys as plain strings, and PostgreSQL refuses to compare its
        ``document_category`` enum to ``character varying`` — a 500 on every
        sort-by-category request. Comparing against the column makes SQLAlchemy
        bind each value with the column's own type. This is the exact bug that
        shipped in the case repository's priority sort and was invisible to a
        SQLite test suite, so it is not repeated here.
        """
        return case(
            *((Document.category == category, rank) for category, rank in CATEGORY_RANK.items()),
            else_=0,
        )


def _categories_matching(term: str) -> list[DocumentCategory]:
    """Categories whose name contains ``term``, case-insensitively.

    Matches against the identifier both as stored (``court_decision``) and as
    read (``court decision``), so a user searching "court decision" finds them.
    """
    needle = term.strip().lower()
    if not needle:
        return []
    return [
        category
        for category in DocumentCategory
        if needle in category.value or needle in category.value.replace("_", " ")
    ]


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search term is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
