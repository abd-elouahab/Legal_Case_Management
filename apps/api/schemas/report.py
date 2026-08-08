"""AI report request and response schemas.

Two responsibilities, the same two the case, document, OCR, indexing, search,
RAG, and assistant schemas carry:

* **Validation** — the report type, the answer language, the optional title, the
  retrieval controls, the list query's bounds, and the export format are all
  enforced here, so routes stay thin and every rejection comes back in the
  standard envelope with a per-field message.
* **Serialization** — a report is returned as an ordered list of *sections*
  rather than as one blob of prose, with the citations the pipeline produced and
  the provenance an evaluation needs.

**The citation schema is :class:`~schemas.rag.RagCitationRead`, reused
verbatim**, exactly as the assistant reuses it and for exactly the same reason:
``14-ai-report-agent.md`` says reports must never invent citations, and a
parallel citation model here would be a second vocabulary to keep in step with
the pipeline's. The one thing this feature *does* to a citation is renumber its
marker — see :func:`~core.reports.remap_markers` — and renumbering is not
modifying: the marker is a position in a list, and a report is a different list
from a single answer.

**The retrieval filters are :class:`~schemas.search.SearchFilterInput`, reused
for the same reason** — they are what the pipeline passes to the search service,
and a copy here would be a copy of the *authorization surface*.

**No schema here carries a prompt, a passage, or a vector.** A report is
assembled from grounded answers; how those were retrieved and prompted is the
pipeline's business, and this feature never sees either.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from core.config import settings
from core.rag import SUPPORTED_ANSWER_LANGUAGES
from core.reports import MAX_REPORT_TITLE_LENGTH, ReportFormat, normalize_report_title
from models.report import ReportStatus, ReportType
from schemas.case import SortOrder
from schemas.rag import RagCitationRead
from schemas.search import SearchFilterInput

#: Default and maximum page sizes, taken from configuration rather than restated,
#: so a deployment that changes one does not have to remember this file.
DEFAULT_PAGE_SIZE = settings.REPORT_PAGE_SIZE
MAX_PAGE_SIZE = settings.REPORT_MAX_PAGE_SIZE

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "ReportCreate",
    "ReportDetailRead",
    "ReportListQuery",
    "ReportMetricsQuery",
    "ReportMetricsRead",
    "ReportPage",
    "ReportRead",
    "ReportSectionRead",
    "ReportSortField",
    "ReportTemplateRead",
    "ReportTemplateSectionRead",
    "SortOrder",
]


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class ReportCreate(BaseModel):
    """Ask the platform to generate one report for one case.

    Deliberately small. Everything that decides what the report *contains* comes
    from the template — ``14-ai-report-agent.md`` requires section ordering to be
    template-driven — so a request chooses the case, the type, and the language,
    and nothing else about the structure.

    The three retrieval controls are the pipeline's own, passed through rather
    than reinterpreted: this feature does not decide what may be retrieved, it
    forwards a request to the service that does.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: uuid.UUID = Field(
        description=(
            "Case the report is about. A case the caller is not party to is refused with 403 — "
            "unlike a report belonging to someone else, which is simply not found."
        )
    )
    report_type: ReportType = Field(
        description=(
            "Which report to produce. `GET /reports/templates` lists them with their sections, so "
            "a client never has to hard-code the catalogue."
        )
    )
    language: str | None = Field(
        default=None,
        description=(
            "ISO 639-1 code the whole report is written in (`ar`, `fr`, or `en`). Settled once, "
            "here, so two sections of one report cannot come back in different languages. Omitted "
            "falls back to French, which `project-overview.md` names alongside Arabic as the "
            "platform's AI-interaction languages."
        ),
    )
    title: str | None = Field(
        default=None,
        max_length=MAX_REPORT_TITLE_LENGTH,
        description=(
            "Heading for the report. Omitted uses the template's name and the case *number* — "
            "never the case title, which is client-confidential and would then travel in an "
            "export filename."
        ),
    )
    conversation_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "The assistant conversation this report was generated from, when it was. Provenance "
            "only: the report is built from the case's documents through the pipeline, never from "
            "a transcript."
        ),
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        le=settings.SEARCH_MAX_LIMIT,
        description=(
            f"Passages to ground each *section* in (max {settings.SEARCH_MAX_LIMIT}). Omitted uses "
            f"the deployment's default."
        ),
    )
    min_score: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="Similarity floor for retrieved passages. Omitted uses the deployment default.",
    )
    filters: SearchFilterInput | None = Field(
        default=None,
        description=(
            "Which documents this report may be built from. The same filters semantic search "
            "accepts, with the same guarantee: they narrow the caller's authorized scope and can "
            "never widen it. The report's case is applied regardless — a filter naming a "
            "*different* case is refused rather than silently overruled."
        ),
    )

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str | None) -> str | None:
        """Accept only a language the platform actually writes reports in.

        Rejected rather than silently ignored, exactly as
        :class:`~schemas.rag.RagRequest` rejects it: a caller who asked for
        German and received French would have no way to tell that the request
        was understood and overruled rather than honoured.
        """
        if value is None:
            return None

        wanted = value.strip().lower()
        if not wanted:
            return None
        if wanted not in SUPPORTED_ANSWER_LANGUAGES:
            supported = ", ".join(sorted(SUPPORTED_ANSWER_LANGUAGES))
            raise ValueError(f"Reports are available in: {supported}.")
        return wanted

    @field_validator("title")
    @classmethod
    def _normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_report_title(value)
        return normalized or None


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


class ReportSortField(StrEnum):
    """Columns the report history may be ordered by.

    An allow-list rather than a free-text column name: the value reaches an
    ``ORDER BY``, and an enum is what makes that safe by construction rather than
    by escaping.
    """

    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    FINISHED_AT = "finished_at"
    DURATION_MS = "duration_ms"
    TITLE = "title"
    STATUS = "status"
    REPORT_TYPE = "report_type"


class ReportListQuery(BaseModel):
    """Validated query parameters for ``GET /reports``.

    Note what is **not** here: there is no ``requested_by`` filter. The history
    is the caller's own by construction — every read in
    :mod:`repositories.report` is keyed by them — so a filter naming a user would
    either be redundant or be a request the API must refuse, and offering it
    would suggest the second is possible.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="Page number, 1-based.")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Reports per page (max {MAX_PAGE_SIZE}).",
    )
    status: ReportStatus | None = Field(
        default=None, description="Only reports in this lifecycle state."
    )
    report_type: ReportType | None = Field(
        default=None, description="Only reports of this type."
    )
    case_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Only reports about this case. What the case workspace's report list sends, so it "
            "shows this matter's reports rather than the platform's."
        ),
    )
    search: str | None = Field(
        default=None,
        max_length=MAX_REPORT_TITLE_LENGTH,
        description=(
            "Match against the report title, case-insensitively. Titles only — searching the "
            "generated prose would be an unindexed scan over a client's matter, and the platform "
            "already has semantic search for content."
        ),
    )
    sort_by: ReportSortField = Field(
        default=ReportSortField.CREATED_AT, description="Column to order by."
    )
    sort_order: SortOrder = Field(
        default=SortOrder.DESC,
        description="Ordering direction. Newest first by default — a history is read that way.",
    )

    @field_validator("search")
    @classmethod
    def _normalize_search(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None

    @property
    def offset(self) -> int:
        """Rows to skip for the requested page."""
        return (self.page - 1) * self.page_size


class ReportMetricsQuery(BaseModel):
    """Validated query parameters for ``GET /reports/metrics``."""

    model_config = ConfigDict(extra="forbid")

    window_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description=(
            "Only count reports created in the last N days. Omitted covers the platform's whole "
            "history, which is the right default for a figure like 'reports generated'."
        ),
    )


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class ReportSectionRead(BaseModel):
    """One section of a finished report.

    Structured rather than free-form, which is the spec's own requirement: a
    client renders headings, an export renders headings, and neither has to parse
    the platform's own prose back into parts.
    """

    key: str = Field(
        description=(
            "Stable identifier of the section within its template (`parties`, `evidence`). Never "
            "shown to a user; it is what a client keys a rendering choice on."
        )
    )
    title: str = Field(description="The section's heading, in the report's language.")
    content: str = Field(
        description=(
            "The section's prose, with citation markers renumbered against the report's own "
            "reference list. When the documents do not cover the section this is the platform's "
            "own sentence saying so — never anything a model improvised."
        )
    )
    grounded: bool = Field(
        description=(
            "Whether this section was produced from retrieved passages. `false` means the case "
            "file does not cover it — a recorded outcome of a successful report, not an error."
        )
    )
    truncated: bool = Field(
        default=False,
        description=(
            "Whether this section hit the model's output ceiling and stops mid-thought. Reported "
            "rather than hidden: a legal section that ends early must not be read as a complete "
            "one."
        ),
    )
    citation_markers: list[int] = Field(
        default_factory=list,
        description=(
            "The report-level markers this section's prose cites, in the order they first appear. "
            "Lets a client show a section's own sources without re-parsing its text."
        ),
    )
    retrieved_count: int = Field(
        default=0, description="Passages retrieved while writing this section."
    )
    context_count: int = Field(
        default=0, description="Of those, how many fitted the context budget."
    )
    duration_ms: int | None = Field(
        default=None, description="Wall-clock time this section took."
    )


class ReportRead(BaseModel):
    """One report as a history row: what it is, and where its run got to.

    Deliberately without its sections. A history page of twenty reports carrying
    twenty full reports would be several megabytes of generated legal prose sent
    to render a list of titles — and a client polling a run's progress would
    re-download the finished report on every tick.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique report identifier.")
    case_id: uuid.UUID = Field(description="Case the report is about.")
    conversation_id: uuid.UUID | None = Field(
        default=None, description="Assistant conversation it was generated from, if any."
    )
    report_type: ReportType = Field(description="Which report this is.")
    title: str = Field(description="The report's heading.")
    language: str = Field(description="ISO 639-1 code it is written in.")
    status: ReportStatus = Field(description="Lifecycle state of the generation run.")

    sections_total: int | None = Field(
        default=None,
        description=(
            "Sections the template asks for. `null` until the run has been planned — before that "
            "the platform genuinely does not know, and `0` would read as 'this report is empty'."
        ),
    )
    sections_completed: int = Field(
        default=0, description="Of those, how many have been written."
    )

    started_at: datetime | None = Field(default=None, description="When generation began.")
    finished_at: datetime | None = Field(
        default=None, description="When it reached a terminal state."
    )
    duration_ms: int | None = Field(default=None, description="Wall-clock time the run took.")
    attempt_count: int = Field(
        default=0, description="Generation attempts so far, including the first."
    )

    retrieved_count: int | None = Field(
        default=None, description="Passages retrieved across every section."
    )
    context_count: int | None = Field(
        default=None, description="Of those, how many reached a prompt."
    )
    grounded_sections: int | None = Field(
        default=None,
        description=(
            "Sections the pipeline could ground in evidence. The number that says whether the "
            "report is worth reading."
        ),
    )
    character_count: int | None = Field(
        default=None, description="Characters of generated prose — the report's size."
    )

    provider: str | None = Field(default=None, description="Provider that generated it.")
    model: str | None = Field(default=None, description="Model that generated it.")
    prompt_name: str | None = Field(
        default=None, description="Pipeline prompt template each section was produced with."
    )
    prompt_version: int | None = Field(default=None, description="Version of that template.")
    template_version: int = Field(
        default=1, description="Revision of the report template set that shaped it."
    )

    prompt_tokens: int | None = Field(
        default=None, description="Prompt tokens summed across the run's sections."
    )
    completion_tokens: int | None = Field(
        default=None, description="Completion tokens summed across the run's sections."
    )
    total_tokens: int | None = Field(default=None, description="Total tokens billed.")

    error_code: str | None = Field(
        default=None,
        description=(
            "Machine-readable cause when the run failed, so a client can branch without parsing "
            "a sentence."
        ),
    )
    error_message: str | None = Field(
        default=None, description="Human-readable explanation, safe to show the report's owner."
    )

    export_count: int = Field(default=0, description="Times this report has been exported.")
    last_exported_at: datetime | None = Field(
        default=None, description="When it was last exported."
    )

    created_at: datetime = Field(description="When the report was requested.")
    updated_at: datetime = Field(description="When it last changed.")

    @computed_field(  # type: ignore[prop-decorator]
        description="Whether the run has finished, successfully or not.",
    )
    @property
    def is_terminal(self) -> bool:
        """Derived rather than stored, so it cannot disagree with `status`."""
        return self.status in {ReportStatus.COMPLETED, ReportStatus.FAILED}

    @computed_field(  # type: ignore[prop-decorator]
        description="Whether the run is queued or generating right now — poll while true.",
    )
    @property
    def is_active(self) -> bool:
        """What a client's polling loop actually tests."""
        return self.status in {ReportStatus.PENDING, ReportStatus.PROCESSING}

    @computed_field(  # type: ignore[prop-decorator]
        description="How far along the run is, 0-100.",
    )
    @property
    def progress_percent(self) -> int:
        """Derived from the two counters, so a progress bar cannot disagree with them."""
        if self.status is ReportStatus.COMPLETED:
            return 100
        if not self.sections_total:
            return 0
        return min(100, int(self.sections_completed / self.sections_total * 100))

    @computed_field(  # type: ignore[prop-decorator]
        description="Wall-clock generation time in seconds, for display.",
    )
    @property
    def duration_seconds(self) -> float | None:
        """Formatted once, server-side, so every surface expresses it identically."""
        if self.duration_ms is None:
            return None
        return round(self.duration_ms / 1000, 2)


class ReportDetailRead(ReportRead):
    """A report together with its sections, its citations, and its front matter.

    One request rather than two, because opening a report always needs all of it
    and a client that had to fetch the sections separately would render an empty
    document for one round trip.
    """

    sections: list[ReportSectionRead] = Field(
        default_factory=list,
        description=(
            "The report itself, in template order. Empty while the run is still in flight, and "
            "empty when it failed."
        ),
    )
    citations: list[RagCitationRead] = Field(
        default_factory=list,
        description=(
            "Every source the report rests on, de-duplicated across sections and numbered "
            "globally — document, version, page, case, and the excerpt the model read. The "
            "`[n]` markers in the sections resolve into this list."
        ),
    )
    references_title: str = Field(
        description="Heading of the reference list, in the report's language."
    )
    disclaimer: str = Field(
        description=(
            "The standing note every generated report carries: it is not legal advice and must be "
            "checked by the responsible lawyer. Sent with the report rather than added by each "
            "client, so it survives every surface and every export."
        )
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="Sources attached to this report.",
    )
    @property
    def citation_count(self) -> int:
        """Derived rather than stored, so it cannot disagree with `citations`."""
        return len(self.citations)

    @computed_field(  # type: ignore[prop-decorator]
        description="Distinct documents this report cites.",
    )
    @property
    def document_count(self) -> int:
        """What a "sources: 6 documents" line reads.

        Distinct documents rather than citations, because three pages of one
        contract are one source to a lawyer.
        """
        return len({citation.document_id for citation in self.citations})


class ReportPage(BaseModel):
    """One page of the report history."""

    items: list[ReportRead] = Field(description="Reports on this page.")
    total_records: int = Field(
        description="Total reports matching the filters, across all pages. The caller's own only."
    )
    page: int = Field(description="Current page number (1-based).")
    page_size: int = Field(description="Maximum number of reports per page.")
    total_pages: int = Field(description="Number of pages available for the current filters.")

    @classmethod
    def build(
        cls, items: list[ReportRead], *, total: int, page: int, page_size: int
    ) -> ReportPage:
        """Assemble a page, deriving ``total_pages`` from the total and size.

        An empty result still reports one page, so a client never renders
        "page 1 of 0".
        """
        return cls(
            items=items,
            total_records=total,
            page=page,
            page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)) if page_size else 1,
        )


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


class ReportTemplateSectionRead(BaseModel):
    """One section a template will produce, as advertised before generation."""

    key: str = Field(description="Stable identifier of the section within its template.")
    title: str = Field(description="Heading it will carry, in the requested language.")


class ReportTemplateRead(BaseModel):
    """One report type the platform can produce.

    Served so a client never hard-codes the catalogue: adding a sixth template is
    an entry in :data:`~core.reports.REPORT_TEMPLATES`, and the picker, the
    filters, and the section preview all follow without a frontend change. That
    is the *"allow future report templates without redesign"* the spec asks for,
    made true of the client as well as the server.
    """

    report_type: ReportType = Field(description="Identifier to send when requesting this report.")
    title: str = Field(description="The report's name, in the requested language.")
    description: str = Field(
        description="One line on what it is for, so nobody generates one to find out."
    )
    sections: list[ReportTemplateSectionRead] = Field(
        description="The sections it will produce, in the order they will appear."
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="How many sections this report contains.",
    )
    @property
    def section_count(self) -> int:
        """Derived rather than stored, so it cannot disagree with `sections`."""
        return len(self.sections)


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


class ReportMetricsRead(BaseModel):
    """Platform-wide report health, as the spec's "Monitoring" section describes it.

    The six figures it names — **generated reports, average generation time,
    export count, failed generations, average report size, and token usage (when
    available)** — plus the rates, the breakdowns, and the configuration behind
    them.

    Note what is *not* here: a ``since`` caveat. Unlike the search, RAG, and
    assistant metrics, every figure on this page is a **SQL aggregate over
    persisted rows**, because a report *is* a persisted run. The numbers are
    exact, they survive a restart, and every API instance reports the same ones —
    which is what a monitoring page ought to be, and is only possible for a
    feature whose work leaves a row behind.
    """

    total_reports: int = Field(description="Reports requested in the window.")
    pending: int = Field(description="Queued, not yet picked up by a worker.")
    processing: int = Field(description="Being generated right now.")
    completed: int = Field(description="Generated successfully — the spec's generated reports.")
    failed: int = Field(description="Runs that could not produce a report.")

    success_rate: float = Field(
        description="Percentage of *finished* runs that produced a report, 0-100."
    )
    failure_rate: float = Field(
        description="Percentage that failed. Complements `success_rate`."
    )

    average_duration_ms: float | None = Field(
        default=None,
        description=(
            "Mean wall-clock generation time of successful runs. Failures are excluded: a run that "
            "timed out against an unresponsive provider would make the platform look slow when "
            "what it is, is blocked."
        ),
    )
    average_characters: float | None = Field(
        default=None,
        description="Mean characters of generated prose per report — the spec's average size.",
    )

    total_sections: int = Field(description="Sections written across every completed report.")
    grounded_sections: int = Field(
        description="Of those, how many the pipeline could ground in retrieved evidence."
    )
    grounding_rate: float = Field(
        description=(
            "Percentage of written sections that were grounded. The number to watch, and not an "
            "AI metric: a falling rate means the corpus no longer covers what reports ask of it."
        )
    )

    total_exports: int = Field(description="Exports served across every report.")
    exported_reports: int = Field(
        description=(
            "Reports exported at least once. The denominator for the figure above: 40 exports "
            "over two reports and over forty are very different platforms."
        )
    )

    total_prompt_tokens: int | None = Field(
        default=None,
        description=(
            "Prompt tokens across every metered report. Absent when no provider has reported "
            "usage — zero would read as 'this platform's reports are free'."
        ),
    )
    total_completion_tokens: int | None = Field(
        default=None, description="Completion tokens across every metered report."
    )
    metered_reports: int = Field(
        description="Reports whose provider reported token usage — the denominator for the totals."
    )
    average_total_tokens: float | None = Field(
        default=None, description="Mean tokens per metered report, prompt plus completion."
    )

    reports_by_type: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Reports grouped by type. What decides whether a sixth template is worth writing, and "
            "which of the five is not earning its place."
        ),
    )
    failures_by_code: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Failed runs grouped by cause. A failure rate says something is wrong; this says what "
            "— an unreachable vector database, a missing credential, and a case with no indexed "
            "documents read identically otherwise."
        ),
    )

    window_days: int | None = Field(
        default=None, description="Window the figures cover, or absent for all time."
    )

    available_formats: list[ReportFormat] = Field(
        default_factory=list,
        description=(
            "Export formats this deployment can actually produce. A format whose rendering "
            "library is not installed is absent rather than offered and then refused."
        ),
    )
    template_version: int = Field(
        description="Revision of the report template set new reports are produced with."
    )
    llm_available: bool = Field(
        description=(
            "Whether a provider client can be built here. False means every generation will fail."
        )
    )
    prompt_available: bool = Field(
        description=(
            "Whether the pipeline's answer template can be loaded. False means every section will "
            "fail, because every section is a pipeline run."
        )
    )
    enabled: bool = Field(
        description="Whether report generation is permitted at all on this deployment."
    )

    @computed_field(  # type: ignore[prop-decorator]
        description="Mean generation time in seconds, for display.",
    )
    @property
    def average_duration_seconds(self) -> float | None:
        """Formatted once, server-side, so every surface expresses it identically."""
        if self.average_duration_ms is None:
            return None
        return round(self.average_duration_ms / 1000, 2)
