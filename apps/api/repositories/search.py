"""Data access for semantic search.

Single responsibility: answering the three questions a vector query needs
PostgreSQL for, and nothing else. No authorization *decisions* live here — those
belong to ``services/search_access.py`` — and no ranking, no embedding, and no
Qdrant.

Why a search feature needs a relational repository at all, when its corpus lives
in a vector database: **the filters it must honour are relational facts.**

* *Which cases may this caller reach?* is the assignment predicate
  :func:`~repositories.case.assigned_case_scope` already defines, and it decides
  what the vector filter is allowed to match. The answer must be a set of case
  ids, because a Qdrant filter cannot join.
* *Which documents are contracts?* / *are PDFs?* is a column on ``documents``.
  The vector payload deliberately does not carry a document's category or file
  type — Document Indexing stores what a *chunk* is, not what its document is —
  so a filter on either resolves to a set of document ids here and is pushed into
  the vector query as one condition.
* *What is this document called?* is what turns a result carrying an identifier
  into one a lawyer can read, and it is answered **once per page** rather than
  once per result.

Every query is scoped and bounded. Nothing here can return a case the caller is
not party to, because the scope is one more AND in the same statement.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from models.case import Case
from models.document import Document, DocumentCategory
from repositories.case import assigned_case_scope


class SearchRepository:
    """Relational lookups behind a vector search."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --------------------------------------------------------------- scope #

    def accessible_case_ids(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        """Every case ``user_id`` is party to, as lawyer or court representative.

        The authorization scope, materialised. Every other feature pushes this
        predicate into the query it is already running (`documents`, `ocr`,
        `indexing`, `timeline` all do); search cannot, because the rows it
        filters are in Qdrant — so the scope has to cross the boundary as a set
        of identifiers.

        That is the whole reason every vector payload carries ``case_id``:
        Document Indexing wrote it so this set could become a filter rather than
        a second authorization scheme. Expressed against the shared
        :func:`~repositories.case.assigned_case_scope` clause rather than a
        second copy of the predicate, so the five cannot drift apart.

        An **empty list is meaningful and must not be treated as "no filter"**: a
        lawyer assigned to nothing may search nothing. See
        :class:`~services.vector_search.SearchFilters`, which distinguishes the
        two shapes explicitly.
        """
        statement = select(Case.id).where(assigned_case_scope(user_id))
        return list(self._session.execute(statement).scalars())

    def case_ids_in(
        self, case_ids: Collection[uuid.UUID], *, visible_to: uuid.UUID | None
    ) -> list[uuid.UUID]:
        """Narrow a set of case ids to those that exist and are in scope.

        Used when a caller filters by case: the intersection is computed **in
        SQL**, so a request naming a case the caller is not party to comes back
        short rather than being trusted and pushed into the vector filter.
        """
        if not case_ids:
            return []

        statement = select(Case.id).where(Case.id.in_(list(case_ids)))
        if visible_to is not None:
            statement = statement.where(assigned_case_scope(visible_to))
        return list(self._session.execute(statement).scalars())

    # ------------------------------------------------------------- filters #

    def document_ids_matching(
        self,
        *,
        case_ids: Collection[uuid.UUID] | None,
        document_ids: Collection[uuid.UUID] | None,
        categories: Collection[DocumentCategory] | None,
        file_types: Collection[str] | None,
        visible_to: uuid.UUID | None,
        limit: int,
    ) -> list[uuid.UUID]:
        """Resolve the document-level filters to a bounded set of document ids.

        The spec's "document type" filter, made to work against a payload that
        does not carry one — and made to work **without re-indexing**, which
        matters because re-embedding the corpus to add a filter would be an
        absurd price for a dropdown.

        Soft-deleted documents are excluded, exactly as every other read path
        excludes them: a withdrawn document's passages are withdrawn with it,
        because a search result is a *reading* of the document and the document is
        no longer readable.

        ``limit`` is read as "one more than the ceiling" by the caller, which is
        how an overflow is detected without a second ``COUNT`` — the service asks
        for ``ceiling + 1`` rows and refuses when it gets them. Refusing rather
        than truncating is deliberate: a silently shortened set would drop
        matching documents with nothing to indicate it.
        """
        statement = select(Document.id).where(Document.deleted_at.is_(None))

        if visible_to is not None:
            statement = statement.where(
                Document.case_id.in_(select(Case.id).where(assigned_case_scope(visible_to)))
            )

        if case_ids is not None:
            statement = statement.where(Document.case_id.in_(list(case_ids)))

        if document_ids is not None:
            statement = statement.where(Document.id.in_(list(document_ids)))

        if categories:
            statement = statement.where(Document.category.in_(list(categories)))

        if file_types:
            # Stored lowercase without a dot (see `core/documents.py`); the
            # service normalises the request the same way, so the comparison is
            # exact rather than case-dependent.
            statement = statement.where(Document.file_extension.in_(list(file_types)))

        return list(self._session.execute(statement.limit(limit)).scalars())

    # -------------------------------------------------------------- output #

    def documents_by_id(self, document_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Document]:
        """Load the documents behind a page of results, in **one** query.

        A result carries a document identifier because that is what the vector
        payload holds; a reader needs a filename. Resolving it per result would
        be one query per hit — the spec's "minimize unnecessary database
        requests" violated on the platform's most frequent operation — so the
        whole page is fetched at once and matched up in memory.

        The case is eagerly loaded because the summary reports it and a lazy load
        would reintroduce exactly the per-row query this method exists to avoid.

        Soft-deleted documents are **excluded**, which means a result whose
        document was deleted between indexing and searching resolves to no
        summary. The service drops those results rather than showing a passage of
        a withdrawn document — the vectors outlive the deletion because deletion
        is logical, so this is the point at which that has to be handled.
        """
        if not document_ids:
            return {}

        statement = (
            select(Document)
            .where(Document.id.in_(list(document_ids)), Document.deleted_at.is_(None))
            .options(selectinload(Document.case))
        )
        return {document.id: document for document in self._session.execute(statement).scalars()}


__all__ = ["SearchRepository"]
