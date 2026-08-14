"""AI report generation business logic.

Owns the flow ``14-ai-report-agent.md`` defines, in exactly the order its diagram
gives:

.. code-block:: text

    User → Select Report Type → Report Generation Agent → RAG Pipeline
         → Retrieve Context → Generate Report → Persist Report → Export

Scope boundaries, kept deliberately sharp — the spec's own list of what this
feature must **not** implement is OCR, indexing, semantic search, prompt
construction, the chat interface, translation, compliance analysis, and the voice
assistant, and every one of them is absent here by construction:

* **Nothing is retrieved here, and nothing can be.** This service holds no vector
  searcher, no embedder, no search service, and no prompt library. Its only route
  to a passage is :meth:`~services.rag.RagService.answer`, which retrieves
  through :class:`~services.search.SearchService`, which scopes every result to
  the caller's cases *inside the vector query*. That single fact is the whole of
  this feature's *document* authorization story — the spec's *"it must never
  query Qdrant directly"* holds structurally rather than by discipline, and its
  *"generated reports must never contain unauthorized information"* is inherited
  rather than restated.
* **No prompt is built here, and no template is named.** A section is a
  *question*, put to the pipeline, which fences it inside its own versioned
  ``rag/answer`` template. See :mod:`core.reports` for why the section
  instructions are domain data rather than prompts.
* **No model is called here.** The pipeline calls one per section; this service
  relays what comes back.
* **No citation is constructed.** The citations are the pipeline's own objects,
  **renumbered** — which is not the same as modified: a marker is a position in a
  list, and a report is a different list from a single answer. See
  :class:`CitationLedger`.
* **Capability authorization is not decided here.** Whether the caller may
  generate a report at all is settled by the dependencies in
  :mod:`api.authorization` before a request reaches this service.
* **Per-resource authorization is applied here**, through
  :class:`~services.report_access.ReportAccessPolicy`, because it needs the case
  — a dependency cannot know whether a lawyer is assigned to a case it has not
  loaded.

What it *does* own are the rules nothing else can express: that a report belongs
to a *case* and to the *user who asked for it*, that only one worker may generate
it, that a status may only move along the legal transitions, that a failure never
touches the case or its documents, that a report with nothing grounded is a
failure rather than a document of empty headings, and that no section of a report
reaches a log.

It also **publishes to the timeline** — requested, generated, failed, and
exported are the report half of a case's history. As with the case, document,
OCR, and indexing services, publication happens after the change is committed,
describes rather than decides, and can never fail the operation that caused it.
Note what it publishes: *that* a report of a given type exists, to people already
party to the case, and never a line of its content — the report itself stays
readable only by the user who generated it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from core.config import settings
from core.events import DomainEventType, report_topic
from core.exceptions import (
    AppException,
    CaseNotFoundError,
    InvalidReportTransitionError,
    RagUnavailableError,
    ReportAlreadyRunningError,
    ReportExportUnavailableError,
    ReportNotFoundError,
    ReportNotReadyError,
    ReportsDisabledError,
    TooManyActiveReportsError,
)
from core.rag import CITATION_MARKER_PATTERN, RagFailureCode
from core.reports import (
    REPORT_TEMPLATE_VERSION,
    ReportFailureCode,
    ReportFormat,
    ReportSectionSpec,
    ReportTemplate,
    can_regenerate,
    can_transition,
    citation_key,
    default_report_title,
    failure_message,
    is_usable_section,
    no_content_message,
    normalize_error_message,
    remap_markers,
    report_disclaimer,
    resolve_report_language,
    template_for,
)
from models.case import Case
from models.report import Report, ReportStatus, ReportType
from models.timeline import TimelineEventType
from models.user import User
from repositories.case import CaseRepository
from repositories.report import ReportRepository, ReportStatusCounts
from schemas.rag import RagCitationRead, RagRequest
from schemas.report import ReportCreate, ReportListQuery
from schemas.search import SearchFilterInput
from services.events import EventPublisher, NullEventPublisher
from services.job_queue import JobQueue, NullJobQueue
from services.localization import LanguageDirectory, resolve_actor_language
from services.rag import RagOutcome, RagService
from services.report_access import ReportAccessPolicy
from services.report_export import (
    RenderableCitation,
    RenderableReport,
    RenderableSection,
    ReportExportError,
    ReportRendererUnavailableError,
    available_formats,
    export_filename,
    get_report_renderer,
)
from services.report_graph import ReportState, SectionDraft, build_report_graph
from services.timeline import NullTimelineRecorder, TimelineRecorder

logger = structlog.get_logger(__name__)

#: LangGraph's own ceiling on how many times a compiled graph may re-enter a
#: node. Raised above the library's default of 25 because the section loop
#: legitimately re-enters one node once per section; ``REPORT_MAX_SECTIONS`` is
#: the real bound, and this only has to be comfortably above it.
_RECURSION_HEADROOM = 10


@dataclass(frozen=True, slots=True)
class ReportJob:
    """One unit of background report work.

    Carries **identifiers only**, never an ORM instance. A job crosses a thread
    boundary and outlives the request that created it, and a detached SQLAlchemy
    object on the far side of either is a source of stale reads and cross-session
    errors — the same rule :class:`~services.indexing.IndexJob` follows. The
    worker re-reads the row it is about to change, which is also what makes the
    claim honest.
    """

    report_id: uuid.UUID
    case_id: uuid.UUID
    report_type: ReportType


@dataclass(frozen=True, slots=True)
class ReportPageResult:
    """One page of report history together with the total matching the filters."""

    results: list[Report]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class ReportExport:
    """One rendered report, ready to be streamed to the caller."""

    content: bytes
    filename: str
    media_type: str
    export_format: ReportFormat

    @property
    def size(self) -> int:
        """Bytes produced. Sent as ``Content-Length``, and logged."""
        return len(self.content)


@dataclass(frozen=True, slots=True)
class ReportMetrics:
    """Platform-wide report health, as the monitoring endpoint reports it."""

    counts: ReportStatusCounts
    reports_by_type: dict[str, int]
    failures_by_code: dict[str, int]
    window_days: int | None
    available_formats: list[ReportFormat]
    template_version: int
    llm_available: bool
    prompt_available: bool
    enabled: bool

    @property
    def success_rate(self) -> float:
        """Share of *finished* runs that produced a report."""
        if self.counts.finished <= 0:
            return 0.0
        return round(self.counts.completed / self.counts.finished * 100, 2)

    @property
    def failure_rate(self) -> float:
        """Share of finished runs that failed.

        Derived as the complement of the success rate rather than computed
        separately, so the two always sum to 100 for a non-empty window — two
        independent roundings would not.
        """
        if self.counts.finished <= 0:
            return 0.0
        return round(100.0 - self.success_rate, 2)

    @property
    def grounding_rate(self) -> float:
        """Share of written sections the pipeline could ground in evidence.

        Over *written sections* rather than over reports, because a report is
        grounded to a degree rather than absolutely — six sections of seven is a
        useful report with a gap, and rounding that to "grounded" or "not" would
        throw away the only number that says how well the corpus covers what
        reports ask of it.
        """
        if self.counts.total_sections <= 0:
            return 0.0
        return round(self.counts.grounded_sections / self.counts.total_sections * 100, 2)

    @property
    def average_total_tokens(self) -> float | None:
        """Mean tokens per metered report, prompt plus completion."""
        if self.counts.metered_reports <= 0:
            return None
        prompt = self.counts.total_prompt_tokens or 0
        completion = self.counts.total_completion_tokens or 0
        return round((prompt + completion) / self.counts.metered_reports, 2)


class CitationLedger:
    """Assigns one global numbering to the sources of a whole report.

    The problem it solves is created by reusing the pipeline, and is worth
    stating plainly. :meth:`~services.rag.RagService.answer` numbers *its own*
    sources ``[1]``…``[n]``, because an answer is the whole document it is
    numbering. A report is one document made of a dozen answers, so ``[1]`` in
    its Evidence section is a different contract from ``[1]`` in its Parties
    section — and a reader following the marker would land on the wrong one.

    So every section's citations are registered here as they are produced, and
    the section's prose is rewritten against the mapping this returns. Three
    properties, each of which the spec asks for in a different sentence:

    * **sources are de-duplicated** on document, version, and page, so two
      sections that both lean on page 7 of the same contract produce one
      reference rather than two lines that are the same line;
    * **nothing is invented** — the citation objects are the pipeline's own,
      copied with a new marker and otherwise untouched;
    * **the list is bounded** by ``REPORT_MAX_CITATIONS``. A source beyond the
      ceiling gets no marker, and :func:`~core.reports.remap_markers` therefore
      *removes* its reference from the prose rather than leaving one that
      resolves to nothing — which is the same treatment an invented marker gets
      in the pipeline, and for the same reason.
    """

    def __init__(self, *, limit: int) -> None:
        self._limit = max(1, limit)
        self._markers: dict[tuple[uuid.UUID, int, int], int] = {}
        self._citations: list[RagCitationRead] = []

    def register(self, citations: Sequence[RagCitationRead]) -> dict[int, int]:
        """Add one section's sources, and return its local → global marker map.

        A source already registered by an earlier section keeps its first marker,
        so the reference list reads in the order a reader meets the sources
        rather than in the order the sections happened to retrieve them.
        """
        mapping: dict[int, int] = {}

        for citation in citations:
            key = citation_key(
                citation.document_id, citation.document_version, citation.page_number
            )
            existing = self._markers.get(key)

            if existing is not None:
                mapping[citation.marker] = existing
                if citation.referenced:
                    # A source one section merely *had* and another actually
                    # *cited* is a cited source. Promoting it keeps the flag
                    # honest at the report level, where it is read.
                    self._promote(existing)
                continue

            if len(self._citations) >= self._limit:
                # Deliberately no mapping: the marker is removed from the prose
                # rather than pointed at a source the reference list does not
                # contain.
                continue

            marker = len(self._citations) + 1
            self._markers[key] = marker
            self._citations.append(citation.model_copy(update={"marker": marker}))
            mapping[citation.marker] = marker

        return mapping

    def _promote(self, marker: int) -> None:
        """Mark an already-registered source as one the report's prose cites."""
        index = marker - 1
        if 0 <= index < len(self._citations) and not self._citations[index].referenced:
            self._citations[index] = self._citations[index].model_copy(
                update={"referenced": True}
            )

    @property
    def citations(self) -> list[RagCitationRead]:
        """The report's reference list, in marker order."""
        return list(self._citations)


#: Timeline event → the domain event announced alongside it.
#:
#: Three of the four, and ``REPORT_EXPORTED`` is the omission. An export changes
#: nothing about the report — the same reasoning that keeps a document *download*
#: off the channel — and the one screen that would care is the one that just
#: triggered it and already has the file. `15-real-time-synchronization.md` asks
#: for exactly this restraint: *"deliver only relevant events"*.
#:
#: ``REPORT_REQUESTED`` maps to ``report.started`` rather than to a name of its
#: own, because from a client's point of view queuing a run *is* the run
#: starting: the row is `pending`, the progress bar has a denominator, and the
#: next thing that happens is a section landing.
_ANNOUNCED_REPORT_EVENTS: dict[TimelineEventType, DomainEventType] = {
    TimelineEventType.REPORT_REQUESTED: DomainEventType.REPORT_STARTED,
    TimelineEventType.REPORT_GENERATED: DomainEventType.REPORT_GENERATED,
    TimelineEventType.REPORT_FAILED: DomainEventType.REPORT_FAILED,
}


class ReportService:
    """Coordinates the report generation lifecycle.

    Implements :class:`~services.report_graph.ReportNodes`; the graph it compiles
    in :meth:`__init__` is what calls them, in the order the spec's flow diagram
    gives.
    """

    def __init__(
        self,
        reports: ReportRepository,
        cases: CaseRepository,
        rag: RagService,
        queue: JobQueue[ReportJob] | None = None,
        access: ReportAccessPolicy | None = None,
        *,
        timeline: TimelineRecorder | None = None,
        events: EventPublisher | None = None,
        languages: LanguageDirectory | None = None,
    ) -> None:
        self._reports = reports
        self._cases = cases
        self._rag = rag
        # See `IndexingService.__init__` for why the defaults record and queue
        # nothing; the application wires the real collaborators in
        # `api.deps.get_report_service`.
        self._queue: JobQueue[ReportJob] = queue or NullJobQueue(name="reports")
        self._access = access or ReportAccessPolicy()
        self._timeline: TimelineRecorder = timeline or NullTimelineRecorder()
        # Same pattern again — and this is the one service where the two are
        # genuinely *not* parallel. Every timeline entry has a matching event, but
        # `REPORT_PROGRESS` has no timeline entry at all: "section 3 of 7 is
        # written" is a fact about a run in flight, not a fact about the case, and
        # putting seven of them into a permanent audit trail per report would bury
        # the history it exists to preserve. That is the spec's Streaming section
        # met with the same infrastructure as everything else.
        self._events: EventPublisher = events or NullEventPublisher()
        # ``21-localization.md``: *"generated reports should use the user's
        # preferred language by default"*, while *"users should also be able to
        # explicitly request a report in another supported language"*. This is the
        # only collaborator that can answer what the default **is** — and it is the
        # one-method `LanguageDirectory` rather than a settings service, so the
        # report agent can ask which language somebody reads in and nothing else
        # about them. The same seam the assistant takes, so *"the user's preferred
        # language"* means one thing on both AI surfaces.
        self._languages = languages
        # Compiled once per instance rather than per report: compilation
        # validates every edge and builds the executor, and neither depends on
        # which report is being generated.
        self._graph = build_report_graph(self)

    # ---------------------------------------------------------- requesting #

    def request_report(self, payload: ReportCreate, *, actor: User) -> Report:
        """Queue one report for generation, and return immediately.

        ``14-ai-report-agent.md``: *"Generation should execute asynchronously
        […] Users should receive progress updates instead of waiting
        synchronously."* This method therefore does everything that can be
        decided **now** — the case exists, the caller may reach it, the platform
        has capacity, the language and title are settled — commits a ``pending``
        row, hands it to the queue, and returns. The client polls the row.

        Deciding authorization here rather than in the worker is the important
        half: a background thread has no request to refuse, so a report whose
        access was only checked at generation time would be a 202 followed by a
        silent failure the caller cannot interpret.

        Raises:
            ReportsDisabledError: report generation is switched off here.
            CaseNotFoundError: no case has this identifier.
            CaseAccessDeniedError: the caller is not party to that case.
            TooManyActiveReportsError: the caller already has as many runs in
                flight as the platform allows.
        """
        if not settings.REPORTS_ENABLED:
            logger.info("report_rejected", reason="disabled", actor_id=str(actor.id))
            raise ReportsDisabledError

        legal_case = self._require_case(payload.case_id)
        self._access.require_case_access(actor, legal_case)

        active = self._reports.count_active(owner_id=actor.id)
        if active >= settings.REPORT_MAX_ACTIVE_PER_USER:
            logger.info(
                "report_rejected",
                reason="too_many_active",
                actor_id=str(actor.id),
                active=active,
                limit=settings.REPORT_MAX_ACTIVE_PER_USER,
            )
            raise TooManyActiveReportsError(settings.REPORT_MAX_ACTIVE_PER_USER)

        # The request's language first, then the requester's stored preference —
        # which is the spec's default-and-override in one expression, and in the
        # safe direction: ``payload.language`` is a **parameter**, so asking for one
        # report in Arabic changes nothing about the account that asked. The
        # language is settled here, before the row is written, so it is a property
        # of the *run* rather than of whenever a worker got to it: a preference
        # changed while a report is generating cannot switch a document's language
        # halfway through its sections.
        language = resolve_report_language(
            resolve_actor_language(
                self._languages, actor.id, requested=payload.language
            )
        )
        template = template_for(payload.report_type)

        report = self._reports.create(
            Report(
                id=uuid.uuid4(),
                case_id=legal_case.id,
                conversation_id=payload.conversation_id,
                report_type=payload.report_type,
                title=payload.title
                or default_report_title(
                    payload.report_type,
                    case_number=legal_case.case_number,
                    language=language,
                ),
                language=language,
                status=ReportStatus.PENDING,
                template_version=REPORT_TEMPLATE_VERSION,
                # Published up front so a client can draw a progress bar with a
                # real denominator from the first poll, rather than showing an
                # indeterminate spinner until the worker happens to plan the run.
                sections_total=min(template.section_count, settings.REPORT_MAX_SECTIONS),
                sections_completed=0,
                requested_by=actor.id,
            )
        )

        logger.info("report_requested", **self._event(report, actor=actor))
        self._publish(
            legal_case,
            report,
            TimelineEventType.REPORT_REQUESTED,
            actor=actor,
            description=(
                f"{actor.full_name} requested a {template.title(language).lower()} for this case."
            ),
        )
        self._enqueue(report)
        return report

    def regenerate(self, report_id: uuid.UUID, *, actor: User) -> Report:
        """Produce this report again, re-using its row.

        **The row is re-used, not replaced**, exactly as
        :meth:`~services.indexing.IndexingService.reindex` re-uses an index's:
        same identifier, status back to ``pending``, content and timings cleared,
        ``attempt_count`` preserved. That keeps every link to the report working
        — a report someone bookmarked, referred to in an email, or attached to a
        matter note stays the same report — and it keeps the history a list of
        *reports* rather than of attempts at one.

        **The case is authorized again**, not merely the ownership: a lawyer
        unassigned from a matter since the first run must not be able to produce a
        fresh interpretation of it from a report they still hold a link to.

        Raises:
            ReportsDisabledError: report generation is switched off here.
            ReportNotFoundError: no such report belongs to this caller.
            ReportAlreadyRunningError: a run is already queued or generating.
            CaseNotFoundError: the case has since been removed.
            CaseAccessDeniedError: the caller is no longer party to the case.
            TooManyActiveReportsError: the caller already has as many runs in
                flight as the platform allows.
        """
        if not settings.REPORTS_ENABLED:
            logger.info("report_rejected", reason="disabled", actor_id=str(actor.id))
            raise ReportsDisabledError

        report = self.get_report(report_id, actor=actor)

        if not can_regenerate(report.status):
            logger.info(
                "report_regenerate_rejected",
                reason="already_running",
                **self._event(report, actor=actor),
            )
            raise ReportAlreadyRunningError(report.status.value)

        legal_case = self._require_case(report.case_id)
        self._access.require_case_access(actor, legal_case)

        active = self._reports.count_active(owner_id=actor.id)
        if active >= settings.REPORT_MAX_ACTIVE_PER_USER:
            raise TooManyActiveReportsError(settings.REPORT_MAX_ACTIVE_PER_USER)

        previous_status = report.status
        self._transition(report, ReportStatus.PENDING)
        # Cleared rather than kept: they describe the *previous* attempt, and
        # leaving them would make a queued run read as though it had already
        # finished. `attempt_count` is deliberately not reset — how many times a
        # report has been attempted is exactly what a regeneration preserves —
        # and neither is `export_count`, which is a fact about the report rather
        # than about this run of it.
        report.started_at = None
        report.finished_at = None
        report.duration_ms = None
        report.error_code = None
        report.error_message = None
        report.sections_completed = 0
        report.template_version = REPORT_TEMPLATE_VERSION
        saved = self._reports.save(report)

        logger.info(
            "report_regenerated",
            previous_status=previous_status.value,
            attempt=saved.attempt_count,
            **self._event(saved, actor=actor),
        )
        self._publish(
            legal_case,
            saved,
            TimelineEventType.REPORT_REQUESTED,
            actor=actor,
            description=f"{actor.full_name} requested this report to be generated again.",
            extra={"attempt": saved.attempt_count + 1},
        )
        self._enqueue(saved)
        return saved

    def requeue_pending(self, limit: int = 100) -> int:
        """Re-queue runs left ``pending`` by a previous process.

        A job lives in an in-process queue (see :mod:`services.job_queue`), so a
        restart loses the schedule but not the *record* of the work — the row is
        still ``pending``, and nothing else would ever pick it up. Running this at
        startup is what closes that gap, and it is safe to run repeatedly: a run
        that has since been claimed is no longer pending, and the claim itself is
        atomic, so a double-queued job processes exactly once.

        Returns:
            How many jobs were re-queued.
        """
        if not settings.REPORTS_ENABLED:
            return 0

        pending = self._reports.pending_reports(limit=limit)
        for report in pending:
            self._enqueue(report)

        if pending:
            logger.info("report_pending_requeued", count=len(pending))
        return len(pending)

    def _enqueue(self, report: Report) -> None:
        """Hand a committed run to the background queue."""
        self._queue.enqueue(
            ReportJob(
                report_id=report.id, case_id=report.case_id, report_type=report.report_type
            )
        )

    # ------------------------------------------------------------- reading #

    def get_report(self, report_id: uuid.UUID, *, actor: User) -> Report:
        """Return one of this user's reports.

        Raises:
            ReportNotFoundError: no such report belongs to this caller.
                Deliberately the same answer for "does not exist", "was deleted",
                and "belongs to someone else" — see the section note in
                :mod:`core.exceptions`.
        """
        report = self._reports.get(report_id, owner_id=actor.id)
        if report is None:
            logger.info(
                "report_access_denied",
                report_id=str(report_id),
                actor_id=str(actor.id),
                role=actor.role.value,
            )
            raise ReportNotFoundError
        return report

    def list_reports(self, query: ReportListQuery, *, actor: User) -> ReportPageResult:
        """Return one page of **this user's** report history.

        Filtering, sorting, pagination, **and the ownership scope** are all
        applied in the database, so the cost of a page does not grow with the
        platform's history and the totals describe only what the caller owns — a
        total that counted other people's reports would disclose how many of them
        exist.
        """
        results, total = self._reports.list_reports(query, owner_id=actor.id)
        return ReportPageResult(
            results=results, total=total, page=query.page, page_size=query.page_size
        )

    def delete_report(self, report_id: uuid.UUID, *, actor: User) -> None:
        """Withdraw a report.

        Logical, for the reason :class:`~models.report.Report` records: the
        sections carry the citations of an analysis a lawyer may have acted on.
        The report stops being readable and exportable through every endpoint
        immediately — the repository excludes deleted rows from every query — and
        a future retention job is what reclaims the storage.

        Idempotent: deleting an already-deleted report answers 404, because by
        then there is no report of that identifier belonging to anyone.

        Raises:
            ReportNotFoundError: no such report belongs to this caller.
        """
        report = self.get_report(report_id, actor=actor)
        self._reports.soft_delete(report)

        logger.info("report_deleted", **self._event(report, actor=actor))

    @staticmethod
    def templates() -> list[ReportTemplate]:
        """Every report the platform can produce, in declaration order.

        Served so a client never hard-codes the catalogue — see
        :class:`~schemas.report.ReportTemplateRead`. A ``staticmethod`` because it
        reads no state at all: the catalogue is a constant, and routing it through
        an instance would suggest it could depend on the caller.
        """
        from core.reports import REPORT_TEMPLATES

        return list(REPORT_TEMPLATES.values())

    def metrics(self, *, window_days: int | None = None) -> ReportMetrics:
        """Aggregate report health for the monitoring endpoint.

        No per-user scope is applied, deliberately: this is an operational view of
        the *platform*, gated on the administrative ``reports:monitor``
        permission, and it reports counts, durations, sizes, and configuration
        only — never a report, a title, a section, a citation, or whose it was.

        Availability is **probed** rather than inferred from the counters, for the
        same reason every other monitoring endpoint here probes it: a platform
        generating no reports because no credential is configured, one generating
        none because its prompts are missing, and one nobody has asked yet all
        show the same zeros.
        """
        since = (
            datetime.now(UTC) - timedelta(days=window_days) if window_days is not None else None
        )
        health = self._rag.health()

        return ReportMetrics(
            counts=self._reports.metrics(since=since),
            reports_by_type=self._reports.type_breakdown(since=since),
            failures_by_code=self._reports.failure_breakdown(since=since),
            window_days=window_days,
            available_formats=available_formats(),
            template_version=REPORT_TEMPLATE_VERSION,
            # Read from the pipeline rather than probed again here: a report
            # section *is* a pipeline run, so "can this platform generate a
            # report" and "can this platform answer a question" are the same
            # question, and asking it twice would let the two answers disagree.
            llm_available=health.llm_available,
            prompt_available=health.prompt_available,
            enabled=settings.REPORTS_ENABLED,
        )

    # -------------------------------------------------------------- export #

    def export(
        self, report_id: uuid.UUID, export_format: ReportFormat, *, actor: User
    ) -> ReportExport:
        """Render one of this user's reports as a file.

        **Rendered per request rather than served from storage**, which is what
        makes the spec's *"exported reports inherit the same permissions as their
        source case"* structural: there is no object anybody can be handed a URL
        to, and every byte is produced inside a request that has already resolved
        the report through an owner-scoped query. See
        :mod:`services.report_export` for the full argument.

        Raises:
            ReportNotFoundError: no such report belongs to this caller.
            ReportNotReadyError: the run has not completed.
            ReportExportUnavailableError: this deployment cannot produce that
                format.
        """
        report = self.get_report(report_id, actor=actor)

        if not report.is_exportable:
            logger.info(
                "report_export_rejected",
                reason="not_ready",
                export_format=export_format.value,
                **self._event(report, actor=actor),
            )
            raise ReportNotReadyError(report.status.value)

        renderer = get_report_renderer(export_format)

        try:
            content = renderer.render(self._renderable(report))
        except ReportRendererUnavailableError as exc:
            logger.info(
                "report_export_unavailable",
                export_format=export_format.value,
                report_id=str(report.id),
            )
            raise ReportExportUnavailableError(str(exc)) from exc
        except ReportExportError as exc:
            # The renderer has already logged the fault without quoting the text
            # it was laying out; all that is left is to answer with a message
            # that names a format the caller can actually use.
            raise ReportExportUnavailableError(
                failure_message(ReportFailureCode.EXPORT_FAILURE)
            ) from exc

        self._reports.record_export(report.id)

        export = ReportExport(
            content=content,
            filename=export_filename(report.title, extension=renderer.file_extension),
            media_type=renderer.media_type,
            export_format=export_format,
        )

        logger.info(
            "report_exported",
            export_format=export_format.value,
            size_bytes=export.size,
            **self._event(report, actor=actor),
        )
        # Published *after* the export succeeded, so the case history records
        # what left the platform rather than what somebody tried to make leave.
        legal_case = self._cases.get_by_id(report.case_id)
        if legal_case is not None:
            self._publish(
                legal_case,
                report,
                TimelineEventType.REPORT_EXPORTED,
                actor=actor,
                description=(
                    f"{actor.full_name} exported this report as "
                    f"{export_format.value.upper()}."
                ),
                extra={"export_format": export_format.value},
            )
        return export

    def _renderable(self, report: Report) -> RenderableReport:
        """Project a stored report onto the value an exporter is handed.

        The projection is where the export's *content* boundary is drawn, and it
        is written as a list of what to **keep** rather than of what to drop —
        the same reasoning :func:`~schemas.search.result_from_payload` records —
        so a column added to the table tomorrow does not silently start
        appearing in every exported PDF.
        """
        return RenderableReport(
            title=report.title,
            language=report.language,
            report_type=report.report_type.value,
            case_number=report.case.case_number if report.case is not None else "",
            generated_at=report.finished_at,
            disclaimer=report_disclaimer(report.language),
            sections=tuple(
                RenderableSection(
                    title=str(section.get("title", "")),
                    content=str(section.get("content", "")),
                    grounded=bool(section.get("grounded", False)),
                )
                for section in report.sections
            ),
            citations=tuple(
                RenderableCitation(
                    marker=int(citation.get("marker", 0)),
                    document_name=str(citation.get("document_name", "")),
                    document_version=int(citation.get("document_version", 1)),
                    page_number=int(citation.get("page_number", 1)),
                )
                for citation in report.citations
            ),
        )

    # ----------------------------------------------------------- processing #

    def process(self, job: ReportJob) -> Report | None:
        """Run one queued report to completion.

        The worker's entry point, and the only method here that is not driven by
        an HTTP request. It runs on a background thread with its own database
        session, so it re-reads everything it needs rather than trusting anything
        the request that queued it had loaded.

        The sequence is the spec's generation flow, in order, and it is the
        **graph** that declares that order — this method owns only what the graph
        cannot: claiming the run, the clock, the single place a failure is
        recorded, and the projection of the final state onto a saved report.

        **Never raises.** Every failure becomes a ``failed`` run carrying a
        machine-readable cause, because that is what the spec asks for: the case,
        its documents, and its indexes stay intact, and the run stays
        regenerable.
        """
        report = self._reports.get_for_worker(job.report_id)
        if report is None:
            logger.warning("report_job_orphaned", report_id=str(job.report_id))
            return None

        started_at = datetime.now(UTC)
        if not self._reports.claim(report.id, started_at=started_at):
            # Someone else got there first, or the run has already finished.
            # Not an error: this is the guard doing its job.
            logger.info(
                "report_job_skipped",
                reason="not_claimable",
                report_id=str(report.id),
                report_status=report.status.value,
            )
            return None

        report = self._reports.get_for_worker(report.id)
        if report is None:  # pragma: no cover - the row was just updated
            return None

        actor = report.requester
        if actor is None:
            # The requester's account was removed between the request and the
            # run. There is nobody to scope retrieval to, and generating "for the
            # platform" would build a report from documents nobody authorized —
            # so the run fails rather than proceeding unscoped. The `CASCADE` on
            # `requested_by` makes this all but unreachable; it is handled
            # because "all but" is not "never".
            logger.warning("report_job_orphaned", reason="no_requester", report_id=str(report.id))
            return self._fail(report, code=ReportFailureCode.UNKNOWN, actor=None, elapsed_ms=0)

        logger.info(
            "report_generation_started",
            attempt=report.attempt_count,
            **self._event(report, actor=actor),
        )

        monotonic_start = time.monotonic()

        try:
            final: ReportState = self._graph.invoke(
                ReportState(
                    request=self._request_for(report),
                    actor=actor,
                    report_id=report.id,
                    case_id=report.case_id,
                    language=report.language,
                    started=monotonic_start,
                    deadline=monotonic_start + settings.REPORT_TIMEOUT_SECONDS,
                    cursor=0,
                    drafts=[],
                ),
                {"recursion_limit": settings.REPORT_MAX_SECTIONS * 2 + _RECURSION_HEADROOM},
            )
        except _ReportRunFailed as failure:
            return self._fail(
                report,
                code=failure.code,
                actor=actor,
                elapsed_ms=self._elapsed_ms(monotonic_start),
            )
        except Exception:
            # A fault nothing above anticipated. Logged with a traceback, then
            # recorded as an ordinary failed run: an unexpected bug must not
            # leave a report stuck at `processing` forever, which is the one
            # state nothing can recover from without operator intervention.
            logger.exception(
                "report_unexpected_failure",
                report_id=str(report.id),
                case_id=str(report.case_id),
            )
            return self._fail(
                report,
                code=ReportFailureCode.UNKNOWN,
                actor=actor,
                elapsed_ms=self._elapsed_ms(monotonic_start),
            )

        return self._complete(
            report, final, actor=actor, elapsed_ms=self._elapsed_ms(monotonic_start)
        )

    # ----------------------------------------------------------- graph nodes #

    def plan_report(self, state: ReportState) -> ReportState:
        """Select the template and the sections this run will produce.

        The spec's *"selecting report template"*, and the whole of it: the
        sections and their order come from
        :data:`~core.reports.REPORT_TEMPLATES` and from nothing else, which is
        what *"section ordering should be template-driven"* means.

        The cap is the spec's "Large Cases" requirement met honestly, in the same
        shape ``INDEX_MAX_CHUNKS`` and ``OCR_MAX_PAGES`` meet theirs: a template
        longer than ``REPORT_MAX_SECTIONS`` produces a *truncated* report and says
        so in the log, which is strictly better than a run that exhausts its
        deadline and produces none.

        This is also where a future planner goes — one that reads the case's
        contents and drops sections it cannot cover, or adds one it can. It would
        write to the same ``plan`` key, and no other node would change.
        """
        template = template_for(state["request"].report_type)
        plan = list(template.sections)

        if len(plan) > settings.REPORT_MAX_SECTIONS:
            logger.warning(
                "report_sections_truncated",
                report_id=str(state["report_id"]),
                planned=len(plan),
                limit=settings.REPORT_MAX_SECTIONS,
            )
            plan = plan[: settings.REPORT_MAX_SECTIONS]

        self._reports.record_progress(state["report_id"], completed=0, total=len(plan))

        logger.info(
            "report_planned",
            report_id=str(state["report_id"]),
            report_type=template.report_type.value,
            section_count=len(plan),
            template_version=REPORT_TEMPLATE_VERSION,
        )
        return ReportState(template=template, plan=plan, cursor=0, drafts=[])

    def write_section(self, state: ReportState) -> ReportState:
        """Produce one section through the RAG pipeline, and advance the cursor.

        The spec's *"requesting relevant context"* and *"assembling report
        sections"*, and the node the whole design turns on. It does exactly three
        things:

        1. asks the pipeline the section's question, scoped to the report's case;
        2. records what came back — the prose, its citations, its timings, its
           token usage — as a draft;
        3. moves the cursor on, which is what the conditional edge reads.

        **A section the documents do not cover is not a failure.** The pipeline
        reports ``insufficient_evidence`` and the section is recorded as
        ungrounded, carrying the platform's own sentence rather than anything a
        model improvised — the same treatment, one layer up, that
        :meth:`~services.rag.RagService.report_no_evidence` gives an unanswerable
        question. A report with a gap in it is a true report.

        **A section whose dependency failed *is* a failure of the whole run**, and
        that is the deliberate half. The tempting alternative — skip the section
        and carry on — produces a legal report silently missing its Evidence
        section because one model call timed out, which is precisely the document
        that misleads a reader who has no way to know it is incomplete.

        Raises:
            _ReportRunFailed: the deadline passed, or the pipeline could not
                answer.
        """
        self._check_deadline(state)

        cursor = state.get("cursor", 0)
        section = state["plan"][cursor]
        language = state["language"]
        started = time.monotonic()

        outcome = self._answer_section(section, state)
        content = outcome.answer.strip()
        grounded = outcome.grounded and is_usable_section(content)

        if not grounded:
            # Covers both the pipeline declining and a model that answered with a
            # fragment. Either way the honest section is the platform's sentence:
            # a heading followed by nine characters reads as a rendering fault,
            # and presenting it as a finding would be worse.
            content = no_content_message(language)

        draft = SectionDraft(
            key=section.key,
            title=section.title(language),
            content=content,
            grounded=grounded,
            # Only meaningful on a section that actually says something: the
            # platform's own "not covered" sentence cannot be cut off.
            truncated=bool(outcome.truncated) and grounded,
            citations=list(outcome.citations) if grounded else [],
            retrieved_count=outcome.retrieved_count,
            context_count=outcome.context_count,
            duration_ms=self._elapsed_ms(started),
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            total_tokens=outcome.total_tokens,
            provider=outcome.provider,
            model=outcome.model,
            prompt_name=outcome.prompt_name,
            prompt_version=outcome.prompt_version,
        )

        drafts = [*state.get("drafts", []), draft]
        # Published as the run goes, which is the whole of the spec's "progress
        # should be queryable by the client": a targeted two-column UPDATE, so a
        # poller sees the counter move without ever seeing a half-written report.
        self._reports.record_progress(
            state["report_id"], completed=len(drafts), total=len(state["plan"])
        )
        # The streaming half, on the same envelope as every other event. A poll
        # every four seconds is what this replaces: a report is a burst of model
        # calls whose sections land seconds apart, so a client watching the bar
        # move is watching real work rather than a timer.
        self._announce_progress(
            report_id=state["report_id"],
            case_id=state["case_id"],
            owner=state["actor"],
            completed=len(drafts),
            total=len(state["plan"]),
            section_key=section.key,
            grounded=grounded,
        )

        logger.info(
            "report_section_written",
            report_id=str(state["report_id"]),
            # The section's *key* and its shape — never its title in the report's
            # language, and never a character of its prose. A section is a
            # generated interpretation of a client's file, and this platform logs
            # no such thing at any stage.
            section_key=section.key,
            section_index=cursor + 1,
            section_count=len(state["plan"]),
            grounded=grounded,
            truncated=bool(draft.get("truncated")),
            citation_count=len(draft.get("citations", [])),
            retrieved_count=outcome.retrieved_count,
            character_count=len(content),
            duration_ms=draft.get("duration_ms"),
        )

        return ReportState(drafts=drafts, cursor=cursor + 1)

    def assemble_report(self, state: ReportState) -> ReportState:
        """Merge the drafts into one document and renumber their citations.

        The spec's *"assembling report sections"* and *"generating citations"*.
        The merging is trivial — the drafts are already in template order — and
        the renumbering is the substance: see :class:`CitationLedger` for why a
        report cannot simply keep the markers each section arrived with.

        Every section's prose is rewritten against its own mapping, so a marker
        that the ledger could not place (because the report's citation ceiling was
        reached) is **removed** rather than left pointing at nothing. That is the
        spec's *"reports should never invent citations"* applied to the one place
        this feature could have invented one.
        """
        ledger = CitationLedger(limit=settings.REPORT_MAX_CITATIONS)
        sections: list[dict[str, Any]] = []

        for draft in state.get("drafts", []):
            citations = draft.get("citations", [])
            mapping = ledger.register(citations)
            content = remap_markers(draft.get("content", ""), mapping) if mapping else draft.get(
                "content", ""
            )

            sections.append(
                {
                    "key": draft.get("key", ""),
                    "title": draft.get("title", ""),
                    "content": content,
                    "grounded": bool(draft.get("grounded")),
                    "truncated": bool(draft.get("truncated")),
                    # The order a reader meets them in, which is what a
                    # "sources for this section" affordance renders.
                    "citation_markers": self._markers_in(content),
                    "retrieved_count": draft.get("retrieved_count", 0),
                    "context_count": draft.get("context_count", 0),
                    "duration_ms": draft.get("duration_ms"),
                }
            )

        citations = ledger.citations

        logger.info(
            "report_assembled",
            report_id=str(state["report_id"]),
            section_count=len(sections),
            citation_count=len(citations),
            grounded_sections=sum(1 for section in sections if section["grounded"]),
        )
        return ReportState(sections=sections, citations=citations)

    def validate_report(self, state: ReportState) -> ReportState:
        """Refuse a report nothing could be grounded in, and count what was.

        The spec's *"validating output"*, and it makes exactly one decision: a
        report in which **no section** could be grounded is a failure rather than
        a document. Every heading would carry the same sentence saying the
        documents do not cover it, which is not a report — it is a
        several-hundred-token way of saying "this case has no indexed documents",
        and a lawyer who received it as a completed report would reasonably
        conclude the platform had read the file and found nothing in it.

        A report with *some* grounded sections is left alone. Judging how many is
        enough would be the platform second-guessing the corpus, and six sections
        of seven is a useful report with a gap the reader can see.

        Raises:
            _ReportRunFailed: nothing in the report could be grounded.
        """
        sections = state.get("sections", [])
        grounded = sum(1 for section in sections if section.get("grounded"))
        characters = sum(len(str(section.get("content", ""))) for section in sections)

        if sections and grounded == 0:
            logger.warning(
                "report_insufficient_context",
                report_id=str(state["report_id"]),
                section_count=len(sections),
            )
            raise _ReportRunFailed(ReportFailureCode.INSUFFICIENT_CONTEXT)

        if not sections:  # pragma: no cover - only reachable with an empty plan
            raise _ReportRunFailed(ReportFailureCode.INSUFFICIENT_CONTEXT)

        return ReportState(grounded_sections=grounded, character_count=characters)

    def finalize_report(self, state: ReportState) -> ReportState:
        """The last node, and deliberately the emptiest one.

        The spec lists *"preparing exports"* among the agent's responsibilities,
        and this is where a design that pre-rendered every format would put that
        work. It does not: an export is a deterministic projection of the stored
        report, so rendering one now would produce bytes that go stale the moment
        the report is regenerated, need storage, need cleanup, and need an
        authorization story of their own. Preparing the export therefore means
        *leaving the report in a state any renderer can consume*, which
        :meth:`assemble_report` has already done — see
        :mod:`services.report_export` for the full argument.

        The node exists rather than being dropped because the graph's shape is
        the agent's declared responsibilities, and a future step that genuinely
        belongs at the end — a summary pass over the assembled draft, a
        notification, a scheduled delivery — attaches here rather than to
        whichever node happened to be last.
        """
        logger.info(
            "report_finalized",
            report_id=str(state["report_id"]),
            section_count=len(state.get("sections", [])),
            grounded_sections=state.get("grounded_sections", 0),
            character_count=state.get("character_count", 0),
        )
        return ReportState()

    # ------------------------------------------------------------- sections #

    def _answer_section(self, section: ReportSectionSpec, state: ReportState) -> RagOutcome:
        """Put one section's question to the pipeline, scoped to the report's case.

        The one place this feature touches the RAG pipeline, and therefore the one
        place its authorization enters. The filters are built here rather than
        taken from the request wholesale, and the **case is forced**: a report is
        about one matter, so a section that retrieved from another would be a
        report whose title and contents disagree. The caller's own case scope is
        applied underneath regardless, by the search service, so this narrows and
        can never widen.

        Raises:
            _ReportRunFailed: the pipeline could not answer.
        """
        request = state["request"]
        remaining = max(1.0, state["deadline"] - time.monotonic())

        try:
            return self._rag.answer(
                RagRequest(
                    question=section.question(state["language"]),
                    language=state["language"],
                    top_k=request.top_k or settings.REPORT_SECTION_TOP_K,
                    min_score=(
                        request.min_score
                        if request.min_score is not None
                        else settings.REPORT_SECTION_MIN_SCORE
                    ),
                    filters=self._filters(request, case_id=state["case_id"]),
                ),
                actor=state["actor"],
                # A section is prose, not a chat reply, and `gemini-2.5-flash`
                # charges its internal deliberation against the same budget — a
                # live run at the deployment's default returned 41 visible
                # tokens. See ``REPORT_SECTION_MAX_OUTPUT_TOKENS``.
                max_output_tokens=settings.REPORT_SECTION_MAX_OUTPUT_TOKENS,
            )
        except RagUnavailableError as exc:
            logger.error(
                "report_section_failed",
                report_id=str(state["report_id"]),
                section_key=section.key,
                cause=exc.error_code,
                # The section's remaining budget, so an operator can tell a
                # provider that is slow from one that is refusing.
                remaining_seconds=round(remaining, 1),
            )
            raise _ReportRunFailed(_translate_rag_failure(exc.error_code)) from exc
        except AppException as exc:
            # A rejected *request* rather than a dependency failure — an
            # inaccessible filter, a disabled pipeline. Recorded as a run failure
            # because there is no request left to answer with a 4xx: the caller
            # went away when this was queued, and the row is the only place left
            # to say what happened.
            logger.warning(
                "report_section_failed",
                report_id=str(state["report_id"]),
                section_key=section.key,
                cause=exc.error_code,
            )
            raise _ReportRunFailed(ReportFailureCode.UNKNOWN) from exc

    @staticmethod
    def _filters(request: ReportCreate, *, case_id: uuid.UUID) -> SearchFilterInput:
        """The retrieval filters one section runs under.

        The request's own, with the report's case applied over them. Copied rather
        than mutated, because the request is shared by every section of the run
        and a mutation would leak between them.
        """
        base = request.filters or SearchFilterInput()
        return base.model_copy(update={"case_id": case_id})

    @staticmethod
    def _markers_in(content: str) -> list[int]:
        """The citation markers a section's prose cites, in first-appearance order.

        Ordered by *position in the text* rather than numerically, because that is
        the order a reader meets them and therefore the order a "sources for this
        section" list should read in. :func:`~core.rag.cited_markers` returns a
        set, which is the right shape for its own caller and the wrong one here —
        so this walks the same compiled pattern rather than declaring a second
        one that could drift from it.
        """
        seen: dict[int, None] = {}
        for match in CITATION_MARKER_PATTERN.finditer(content):
            seen.setdefault(int(match.group(1)), None)
        return list(seen)

    # ------------------------------------------------------------- finishing #

    def _complete(
        self, report: Report, state: ReportState, *, actor: User, elapsed_ms: int
    ) -> Report:
        """Record what the run produced and mark it completed."""
        self._transition(report, ReportStatus.COMPLETED)

        drafts = state.get("drafts", [])
        sections = state.get("sections", [])

        report.sections = sections
        report.citations = [citation.model_dump(mode="json") for citation in state.get("citations", [])]
        report.sections_total = len(sections)
        report.sections_completed = len(sections)
        report.grounded_sections = state.get("grounded_sections", 0)
        report.character_count = state.get("character_count", 0)
        report.retrieved_count = sum(int(draft.get("retrieved_count", 0)) for draft in drafts)
        report.context_count = sum(int(draft.get("context_count", 0)) for draft in drafts)
        # Literal keys at the call site rather than a key passed into the
        # helpers: a ``TypedDict`` resolves ``draft["provider"]`` to ``str |
        # None`` and ``draft.get(key)`` — with ``key`` a variable — to ``object``,
        # so passing the name would throw the types away and buy nothing.
        report.provider = _first(draft.get("provider") for draft in drafts)
        report.model = _first(draft.get("model") for draft in drafts)
        report.prompt_name = _first(draft.get("prompt_name") for draft in drafts)
        report.prompt_version = _first(draft.get("prompt_version") for draft in drafts)
        report.prompt_tokens = _sum_optional(draft.get("prompt_tokens") for draft in drafts)
        report.completion_tokens = _sum_optional(
            draft.get("completion_tokens") for draft in drafts
        )
        report.total_tokens = _sum_optional(draft.get("total_tokens") for draft in drafts)
        report.finished_at = datetime.now(UTC)
        report.duration_ms = elapsed_ms
        report.error_code = None
        report.error_message = None

        saved = self._save(report)

        logger.info(
            "report_generated",
            section_count=len(sections),
            grounded_sections=saved.grounded_sections,
            citation_count=len(saved.citations),
            # The *size* of the report, never the report: `code-standards.md` and
            # the spec both forbid logging confidential document contents, and a
            # generated interpretation of a case file is at least as sensitive as
            # the passages it was built from.
            character_count=saved.character_count,
            retrieved_count=saved.retrieved_count,
            total_tokens=saved.total_tokens,
            duration_ms=saved.duration_ms,
            **self._event(saved, actor=actor),
        )

        legal_case = self._cases.get_by_id(saved.case_id)
        if legal_case is not None:
            template = template_for(saved.report_type)
            self._publish(
                legal_case,
                saved,
                TimelineEventType.REPORT_GENERATED,
                actor=actor,
                description=(
                    f"A {template.title(saved.language).lower()} was generated for this case — "
                    f"{len(sections)} section{'s' if len(sections) != 1 else ''}, "
                    f"{len(saved.citations)} source{'s' if len(saved.citations) != 1 else ''}."
                ),
                extra={
                    "section_count": len(sections),
                    "grounded_sections": saved.grounded_sections,
                    "citation_count": len(saved.citations),
                    "duration_ms": saved.duration_ms,
                },
            )
        return saved

    def _fail(
        self,
        report: Report,
        *,
        code: ReportFailureCode,
        actor: User | None,
        elapsed_ms: int,
    ) -> Report:
        """Record a failed run, leaving the case and its documents untouched.

        The guarantees the spec names hold structurally rather than by care: this
        method writes to ``reports`` only, so it *cannot* alter a case, delete a
        document, or lose a page of OCR text — it has no write path to any of
        them.

        Sections already written by this attempt are deliberately **discarded**,
        which is the opposite of the choice :meth:`~services.indexing.IndexingService._fail`
        makes about partial vectors, and for a reason particular to this feature:
        a partial index is a smaller index and still correct, while a partial
        *report* is a legal document missing sections with nothing on its face to
        say so. Half a report is not a smaller report.
        """
        self._transition(report, ReportStatus.FAILED)
        report.finished_at = datetime.now(UTC)
        report.duration_ms = elapsed_ms
        report.error_code = code.value
        report.error_message = normalize_error_message(failure_message(code))
        report.sections = []
        report.citations = []

        saved = self._save(report)

        logger.warning(
            "report_generation_failed",
            error_code=code.value,
            attempt=saved.attempt_count,
            duration_ms=saved.duration_ms,
            **self._event(saved, actor=actor),
        )

        legal_case = self._cases.get_by_id(saved.case_id)
        if legal_case is not None:
            self._publish(
                legal_case,
                saved,
                TimelineEventType.REPORT_FAILED,
                actor=actor,
                description=f"Report generation failed for this case — {saved.error_message}",
                extra={"error_code": code.value, "attempt": saved.attempt_count},
            )
        return saved

    def _save(self, report: Report) -> Report:
        """Commit a run's changes, rolling back and re-raising on failure."""
        try:
            return self._reports.save(report)
        except Exception:
            self._reports.rollback()
            logger.error(
                "report_write_failed", report_id=str(report.id), case_id=str(report.case_id)
            )
            raise

    # -------------------------------------------------------------- helpers #

    @staticmethod
    def _request_for(report: Report) -> ReportCreate:
        """Rebuild the request a stored run was created from.

        The graph takes a request rather than a row so that its nodes are
        testable without a database — and so that a future caller which never
        persists a report (a preview, a dry run) can drive the same graph. The
        retrieval controls are deliberately *not* stored on the row and therefore
        not restored: they are per-request tuning, and a regeneration months
        later should use the deployment's current defaults rather than a number
        somebody typed once.
        """
        return ReportCreate(
            case_id=report.case_id,
            report_type=report.report_type,
            language=report.language,
            title=report.title,
            conversation_id=report.conversation_id,
        )

    def _require_case(self, case_id: uuid.UUID) -> Case:
        """Load the case a report is about, or fail with a 404."""
        legal_case = self._cases.get_by_id(case_id)
        if legal_case is None:
            logger.info("report_case_lookup_failed", case_id=str(case_id))
            raise CaseNotFoundError
        return legal_case

    @staticmethod
    def _transition(report: Report, target: ReportStatus) -> None:
        """Move a run to a new state, refusing an illegal move.

        Checked even though every caller here is already correct, because the
        transition table is the definition of the lifecycle and a future caller —
        a scheduled report job, an admin tool — must not be able to write a status
        directly. A run that reached ``completed`` without passing through
        ``processing`` would make its duration and its start time a lie.

        Raises:
            InvalidReportTransitionError: the move is not in the transition table.
        """
        if not can_transition(report.status, target):
            raise InvalidReportTransitionError(report.status.value, target.value)
        report.status = target

    @staticmethod
    def _check_deadline(state: ReportState) -> None:
        """Refuse to begin a section after the run's deadline has passed.

        Checked **between** sections rather than inside one, for the reason
        :meth:`~services.rag.RagService._check_deadline` records: neither the
        search service nor a provider SDK accepts a deadline that can be moved
        mid-call, so the honest guarantee is *"no new section begins after the
        deadline"*, which bounds a run at one section's overrun instead of
        claiming a precision the libraries do not offer. The alternative — no
        deadline at all — is a worker thread that never returns.

        Raises:
            _ReportRunFailed: the deadline has passed.
        """
        if time.monotonic() <= state["deadline"]:
            return

        logger.error(
            "report_deadline_exceeded",
            report_id=str(state["report_id"]),
            section_index=state.get("cursor", 0) + 1,
            budget_seconds=settings.REPORT_TIMEOUT_SECONDS,
        )
        raise _ReportRunFailed(ReportFailureCode.TIMEOUT)

    @staticmethod
    def _elapsed_ms(monotonic_start: float) -> int:
        """Milliseconds since ``monotonic_start``.

        From :func:`time.monotonic` rather than from wall-clock timestamps: a
        clock adjustment mid-run would produce a negative duration, and a
        negative sample in a monitoring average is worse than a slightly
        imprecise one.
        """
        return max(0, int((time.monotonic() - monotonic_start) * 1000))

    def _publish(
        self,
        legal_case: Case,
        report: Report,
        event_type: TimelineEventType,
        *,
        actor: User | None,
        description: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Announce a report event on the case's timeline.

        Every report event carries the same identifying metadata, assembled here
        so four call sites cannot each remember a different subset of it;
        ``extra`` adds whatever is specific to one of them.

        **What is published, and what is not.** The event says that a report of a
        given type was requested, produced, failed, or exported — to the people
        already party to the case, which is the collaboration `architecture.md`
        invariants 3 and 9 ask for. It carries **no section, no citation, and not
        the report's title**, and it grants nothing: the report itself stays
        readable only through an owner-scoped query, because
        ``14-ai-report-agent.md`` requires history to remain user-specific.
        """
        self._timeline.record(
            case_id=legal_case.id,
            event_type=event_type,
            actor=actor,
            description=description,
            metadata={
                "report_id": report.id,
                "report_type": report.report_type.value,
                "report_status": report.status.value,
                "language": report.language,
                **(extra or {}),
            },
        )

        announced = _ANNOUNCED_REPORT_EVENTS.get(event_type)
        if announced is not None:
            self._announce(report, announced, actor=actor, **(extra or {}))

    def _announce(
        self,
        report: Report,
        event_type: DomainEventType,
        *,
        actor: User | None,
        **payload: Any,
    ) -> None:
        """Publish one domain event about this report.

        **On the report's own topic, never the case's**, and that is the one
        place this feature's routing deliberately diverges from every other. A
        report is its author's private work product — ``14-ai-report-agent.md``
        keeps history user-specific — so :data:`~core.events.CASE_FANOUT_SCOPES`
        excludes :attr:`~core.events.EventScope.REPORT` and only the requester's
        own connections can follow it. The case's participants still learn that a
        report was produced, through the timeline entry beside this call, which is
        exactly the asymmetry the spec asks for: the *fact* is shared, the
        *content* is not.

        ``case_id`` still travels, because the author's screen needs to know which
        case workspace to refresh — and it reaches nobody who is not already the
        author.

        The payload carries the run's shape and never its substance: no title, no
        section, no citation. :data:`~core.events.FORBIDDEN_PAYLOAD_KEYS` would
        strip the last two even if a future call site tried.
        """
        self._events.publish(
            event_type=event_type,
            topic=report_topic(report.id),
            case_id=report.case_id,
            actor_id=actor.id if actor is not None else report.requested_by,
            payload={
                "report_id": report.id,
                "report_type": report.report_type.value,
                "report_status": report.status.value,
                "language": report.language,
                **payload,
            },
        )

    def _announce_progress(
        self,
        *,
        report_id: uuid.UUID,
        case_id: uuid.UUID,
        owner: User,
        completed: int,
        total: int,
        section_key: str,
        grounded: bool,
    ) -> None:
        """Announce one section completing, mid-run.

        Takes identifiers rather than a :class:`~models.report.Report`, because
        it is called from inside the graph on a background thread and the row is
        deliberately not carried across one — the same reason
        :class:`~services.report_graph.ReportState` holds ids.

        ``section_key`` is the template's stable key (``parties``, ``timeline``),
        never the section's title in the report's language and emphatically never
        its prose: a client uses it to say *which* part is being written, and the
        title it renders comes from the template catalogue it already fetched.
        """
        self._events.publish(
            event_type=DomainEventType.REPORT_PROGRESS,
            topic=report_topic(report_id),
            case_id=case_id,
            actor_id=owner.id,
            payload={
                "report_id": report_id,
                "sections_completed": completed,
                "sections_total": total,
                "section_key": section_key,
                "grounded": grounded,
            },
        )

    @staticmethod
    def _event(report: Report, *, actor: User | None) -> dict[str, Any]:
        """The fields every report log entry carries.

        Identifiers, the status, and the shape of the work — **never the title,
        never a section, and never a citation**. The first names a case; the
        second is a generated interpretation of a client's file, which is at least
        as sensitive as the passages it was built from; and the third names the
        documents behind it. Every earlier stage of this pipeline refuses to log
        its own equivalent, and this one is no exception.
        """
        return {
            "report_id": str(report.id),
            "case_id": str(report.case_id),
            "report_type": report.report_type.value,
            "report_status": report.status.value,
            "language": report.language,
            "actor_id": str(actor.id) if actor is not None else None,
            "role": actor.role.value if actor is not None else None,
        }


class _ReportRunFailed(Exception):
    """Internal signal that a run failed, carrying the cause.

    Private to this module: a failed section, an exhausted deadline, or a report
    nothing could be grounded in are failures of the *run*, and they need to
    travel from a graph node to the one place that records a failure without
    pretending to be a library's exception. The same shape
    :class:`~services.indexing._IndexRunFailed` has.
    """

    def __init__(self, code: ReportFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


def _translate_rag_failure(value: str) -> ReportFailureCode:
    """Map a pipeline failure onto this feature's vocabulary.

    A translation rather than a pass-through, because the two vocabularies are
    deliberately not the same: the pipeline has a ``context_overflow`` that means
    *this question is too long*, which cannot happen to a section whose question
    the platform itself wrote, and this feature has an ``insufficient_context``
    and an ``export_failure`` the pipeline has no notion of. Anything unmapped
    becomes the catch-all rather than being invented into a member.
    """
    mapping = {
        RagFailureCode.RETRIEVAL_UNAVAILABLE.value: ReportFailureCode.RETRIEVAL_UNAVAILABLE,
        RagFailureCode.LLM_UNAVAILABLE.value: ReportFailureCode.LLM_UNAVAILABLE,
        RagFailureCode.TIMEOUT.value: ReportFailureCode.TIMEOUT,
        RagFailureCode.LLM_FAILURE.value: ReportFailureCode.LLM_FAILURE,
        RagFailureCode.MALFORMED_RESPONSE.value: ReportFailureCode.MALFORMED_RESPONSE,
        RagFailureCode.CONTEXT_OVERFLOW.value: ReportFailureCode.LLM_FAILURE,
    }
    return mapping.get(value, ReportFailureCode.UNKNOWN)


def _first[T](values: Iterable[T | None]) -> T | None:
    """The first non-null value across a run's sections.

    Provenance is per *report* on the row and per *section* in reality, and they
    agree in every deployment that does not change providers mid-run. The first
    is taken rather than the last so that a report records what produced the bulk
    of it — the earlier sections — rather than what happened to produce its
    conclusion.
    """
    return next((value for value in values if value is not None), None)


def _sum_optional(values: Iterable[int | None]) -> int | None:
    """Sum a usage figure across sections, keeping "not reported" distinct from zero.

    ``None`` when no section reported the figure at all — ``0`` would read as
    "this report was free", which is a different and false statement, and is the
    same distinction :class:`~services.llm.LLMCompletion` draws about a single
    call.
    """
    reported = [value for value in values if value is not None]
    return sum(reported) if reported else None


__all__ = [
    "CitationLedger",
    "ReportExport",
    "ReportJob",
    "ReportMetrics",
    "ReportPageResult",
    "ReportService",
]
