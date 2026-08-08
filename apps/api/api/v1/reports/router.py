"""AI report generation endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.report.ReportService`, and shape the HTTP response. No
business logic lives here, and **no route calls the RAG pipeline directly** — the
report service owns the lifecycle, the pipeline owns every section, and one path
from the first to the second keeps them that way.

**Requesting a report answers 201 and returns immediately.** Generation is
background work (``14-ai-report-agent.md``: *"Users should receive progress
updates instead of waiting synchronously"*), so the response is the ``pending``
run, and the client polls ``GET /reports/{id}`` while ``is_active`` is true. It
is deliberately **not** a 202: a report *resource* is created by this call, it
has a URL, and 201 with that resource in the body is what lets a client render
the new row without a second request.

Authorization is layered, and the three layers answer different questions:

* the ``reports:*`` and ``ai:*`` **capabilities** are required by the reusable
  dependencies in :mod:`api.authorization`, declared next to the route so they
  appear in the OpenAPI schema. Because ``CurrentUser`` resolves first, an
  anonymous caller gets **401** and only an authenticated-but-unentitled one gets
  **403**;
* the **case** — may this caller generate a report about it — is applied by
  :class:`~services.report_access.ReportAccessPolicy`, which delegates to the
  case policy. A case they are not party to is a **403**;
* the **report** — whose it is — is applied by the repository's queries, which
  are all keyed by requester, so a report belonging to somebody else is a **404**
  rather than a 403. The asymmetry is deliberate and is explained in
  :mod:`core.exceptions`;
* the **documents** a report may be built from are scoped by
  :class:`~services.search.SearchService` inside the vector query, because the
  agent retrieves through nothing else.

**Generating requires two permissions, and that is not belt-and-braces.**
``reports:generate`` is the Reports module's capability and ``ai:generate-report``
is the AI capability that has named exactly this surface since Authorization
shipped; a report is both, so a deployment that grants one and withholds the
other must not reach the agent through this door. ``ai:ask`` is deliberately
**not** additionally required: it gates the ad-hoc question endpoint, and a
deployment that wants report generation without ad-hoc questioning is a coherent
policy this API should not overrule. Reading and exporting need only
``reports:view``, because neither asks anything new of the pipeline.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query, Response, status

from api.authorization import require_all_permissions, require_permission
from api.deps import ReportServiceDep
from core.permissions import Permission
from core.reports import ReportFormat, references_title, report_disclaimer
from models.report import Report
from models.user import User
from schemas.rag import RagCitationRead
from schemas.report import (
    ReportCreate,
    ReportDetailRead,
    ReportListQuery,
    ReportMetricsQuery,
    ReportMetricsRead,
    ReportPage,
    ReportRead,
    ReportSectionRead,
    ReportTemplateRead,
    ReportTemplateSectionRead,
)

logger = structlog.get_logger(__name__)

#: Mounted under ``/reports``.
router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
# --------------------------------------------------------------------------- #

Reader = Annotated[User, Depends(require_permission(Permission.REPORTS_VIEW))]
Generator = Annotated[
    User,
    Depends(
        require_all_permissions(Permission.REPORTS_GENERATE, Permission.AI_GENERATE_REPORT)
    ),
]
ReportMonitor = Annotated[User, Depends(require_permission(Permission.REPORTS_MONITOR))]

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "Missing, invalid, or expired access token.",
    }
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "description": (
            "The account is disabled, lacks the required permission, or is not party to the case "
            "the report is about."
        ),
    }
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such report belongs to this caller — it does not exist, it was deleted, or it is "
            "somebody else's. The three are deliberately indistinguishable. (For `POST /reports`, "
            "the case does not exist.)"
        ),
    }
}
_CONFLICT: dict[int | str, dict[str, object]] = {
    status.HTTP_409_CONFLICT: {
        "description": "The report is already being generated, or has not finished generating.",
    }
}
_THROTTLED: dict[int | str, dict[str, object]] = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "description": (
            "The caller already has as many reports in flight as the platform allows. A report "
            "costs a model call per section, so the queue is bounded per user."
        ),
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Request validation failed."}
}
_UNAVAILABLE: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": (
            "Report generation is disabled on this deployment, or the requested export format "
            "cannot be produced here. The `error` field names which."
        ),
    }
}


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #


def _to_report(report: Report) -> ReportRead:
    """Project one report onto its history-row shape.

    Deliberately **without** its sections: a page of twenty reports carrying
    twenty full reports would be megabytes of generated legal prose sent to
    render a list of titles, and a client polling a run's progress would
    re-download the finished report on every tick.
    """
    return ReportRead.model_validate(report)


def _to_detail(report: Report) -> ReportDetailRead:
    """Project one report onto its full shape, sections and citations included.

    The citations round-trip through :class:`~schemas.rag.RagCitationRead` — the
    pipeline's own type, reused verbatim — so what a client receives is
    structurally identical to what ``POST /rag/answer`` returns, with only the
    marker renumbered against the report's own reference list.
    """
    return ReportDetailRead(
        **_to_report(report).model_dump(),
        sections=[ReportSectionRead(**_section(section)) for section in report.sections],
        citations=[RagCitationRead.model_validate(citation) for citation in report.citations],
        references_title=references_title(report.language),
        disclaimer=report_disclaimer(report.language),
    )


def _section(section: dict[str, Any]) -> dict[str, Any]:
    """Read one stored section defensively.

    The column is JSON, so a report written by an earlier version of the platform
    may lack a key this version reads. Defaults here rather than optional fields
    on the schema, because the *contract* should not become more permissive to
    accommodate history — a section without a title is a rendering bug, and a
    schema that allowed one would hide it.
    """
    return {
        "key": str(section.get("key", "")),
        "title": str(section.get("title", "")),
        "content": str(section.get("content", "")),
        "grounded": bool(section.get("grounded", False)),
        "truncated": bool(section.get("truncated", False)),
        "citation_markers": list(section.get("citation_markers", [])),
        "retrieved_count": int(section.get("retrieved_count", 0) or 0),
        "context_count": int(section.get("context_count", 0) or 0),
        "duration_ms": section.get("duration_ms"),
    }


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


@router.get(
    "/templates",
    response_model=list[ReportTemplateRead],
    status_code=status.HTTP_200_OK,
    summary="List the report types this platform can produce",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def list_report_templates(
    actor: Reader,
    reports: ReportServiceDep,
    language: Annotated[
        str | None,
        Query(description="ISO 639-1 code to label the templates in (`ar`, `fr`, or `en`)."),
    ] = None,
) -> list[ReportTemplateRead]:
    """Return every report type, with the sections each one will produce.

    Served so a client never hard-codes the catalogue: adding a sixth template is
    an entry in the platform's template table, and the picker, the type filter,
    and the section preview all follow with no frontend change. That is the
    spec's *"allow future report templates without redesign"*, made true of the
    client as well as the server.

    **Registered before `/{report_id}`** so `templates` is matched as a literal
    path rather than parsed as a report identifier — FastAPI resolves routes in
    declaration order, and a UUID parser would answer 422 for this URL.

    Requires only `reports:view`: reading what the platform *could* produce asks
    nothing of the pipeline and reveals nothing about any case.
    """
    resolved = _resolve_language(language)
    return [
        ReportTemplateRead(
            report_type=template.report_type,
            title=template.title(resolved),
            description=template.description(resolved),
            sections=[
                ReportTemplateSectionRead(key=section.key, title=section.title(resolved))
                for section in template.sections
            ],
        )
        for template in reports.templates()
    ]


def _resolve_language(value: str | None) -> str:
    """Settle the language a catalogue is labelled in.

    Reuses the same resolver a report request uses, so the titles a client shows
    in its picker are byte-identical to the headings the generated report will
    carry — a picker offering "Synthèse de l'affaire" that produces a report
    headed "Case Summary" is a small inconsistency that reads as a bug.
    """
    from core.reports import resolve_report_language

    return resolve_report_language(value)


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


@router.get(
    "/metrics",
    response_model=ReportMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="AI report metrics",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_report_metrics(
    actor: ReportMonitor,
    reports: ReportServiceDep,
    query: Annotated[ReportMetricsQuery, Query()],
) -> ReportMetricsRead:
    """Return platform-wide report health.

    The six figures the spec's Monitoring section names — **generated reports,
    average generation time, export count, failed generations, average report
    size, and token usage** — plus the rates, the breakdowns by type and by
    cause, and which export formats this deployment can actually produce.

    **Every figure is a SQL aggregate over persisted rows**, which is why this
    endpoint carries no `since` caveat while `/search/metrics`, `/rag/metrics`,
    and `/assistant/metrics` all do: a report *is* a persisted run, so the
    numbers are exact, they survive a restart, and every API instance reports the
    same ones.

    An operational view, so it is gated on `reports:monitor` and reports **counts,
    durations, sizes, and configuration only** — never a report, a title, a
    section, a citation, or whose it was.

    **Registered before `/{report_id}`** for the same reason `/templates` is.
    """
    metrics = reports.metrics(window_days=query.window_days)
    counts = metrics.counts

    return ReportMetricsRead(
        total_reports=counts.total,
        pending=counts.pending,
        processing=counts.processing,
        completed=counts.completed,
        failed=counts.failed,
        success_rate=metrics.success_rate,
        failure_rate=metrics.failure_rate,
        average_duration_ms=counts.average_duration_ms,
        average_characters=counts.average_characters,
        total_sections=counts.total_sections,
        grounded_sections=counts.grounded_sections,
        grounding_rate=metrics.grounding_rate,
        total_exports=counts.total_exports,
        exported_reports=counts.exported_reports,
        total_prompt_tokens=counts.total_prompt_tokens,
        total_completion_tokens=counts.total_completion_tokens,
        metered_reports=counts.metered_reports,
        average_total_tokens=metrics.average_total_tokens,
        reports_by_type=metrics.reports_by_type,
        failures_by_code=metrics.failures_by_code,
        window_days=metrics.window_days,
        available_formats=metrics.available_formats,
        template_version=metrics.template_version,
        llm_available=metrics.llm_available,
        prompt_available=metrics.prompt_available,
        enabled=metrics.enabled,
    )


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


@router.post(
    "",
    response_model=ReportRead,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a report for a case",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_THROTTLED,
        **_VALIDATION,
        **_UNAVAILABLE,
    },
)
def create_report(
    actor: Generator, reports: ReportServiceDep, payload: ReportCreate
) -> ReportRead:
    """Ask the platform to produce one report, and get the run back immediately.

    **This returns before the report exists.** Generation is background work: the
    response is a `pending` run with a `sections_total` you can draw a progress
    bar against, and the client polls `GET /reports/{id}` while `is_active` is
    true. A seven-section report is seven retrievals and seven model calls, so
    waiting for it on an HTTP connection is not something either end should try.

    **The report is built only from documents the caller can already read.** Every
    section goes through the RAG pipeline, which retrieves through the semantic
    search service, which scopes each passage to the cases the caller is party to
    — inside the vector query. A case they are not party to is refused here, with
    a 403, before anything is queued.

    **Each section is grounded or it says so.** A section the case file does not
    cover carries the platform's own sentence to that effect rather than anything
    a model improvised, and is marked `grounded: false`. A report in which
    *nothing* could be grounded fails with `insufficient_context` instead of
    arriving as a document of empty headings.

    The queue is bounded per user: a report costs a model call per section, so a
    caller who already has several in flight is answered **429** rather than
    being allowed to spend the deployment's whole token budget from one tab.
    """
    return _to_report(reports.request_report(payload, actor=actor))


@router.get(
    "",
    response_model=ReportPage,
    status_code=status.HTTP_200_OK,
    summary="List your reports",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def list_reports(
    actor: Reader,
    reports: ReportServiceDep,
    query: Annotated[ReportListQuery, Query()],
) -> ReportPage:
    """List the caller's own reports, newest first.

    **Only the caller's.** The scope is applied in the database query, so the page
    totals count only what they generated — a total that included other people's
    reports would disclose how many of them exist, and the spec requires history
    to remain user-specific.

    `case_id` narrows the list to one matter, which is what the case workspace
    sends. Sections are deliberately not included: fetch `GET /reports/{id}` to
    read one.
    """
    page = reports.list_reports(query, actor=actor)

    return ReportPage.build(
        [_to_report(report) for report in page.results],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get(
    "/{report_id}",
    response_model=ReportDetailRead,
    status_code=status.HTTP_200_OK,
    summary="Read one report",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def get_report(
    report_id: uuid.UUID, actor: Reader, reports: ReportServiceDep
) -> ReportDetailRead:
    """Return one report with its sections, its citations, and its status.

    **This is also the progress endpoint.** While a run is in flight the sections
    are empty and `progress_percent`, `sections_completed`, and `sections_total`
    move as it writes; poll it while `is_active` is true. One endpoint rather than
    two, because a client that polled a separate status URL would then need a
    third request to fetch the report the moment it finished.

    Sections arrive in template order, each with its heading, its prose, and
    whether it was grounded. The `[n]` markers in the prose resolve into
    `citations`, which is de-duplicated and numbered across the whole report —
    so two sections leaning on the same page of the same contract cite one
    source, not two.

    A report that does not exist, one that was deleted, and one belonging to
    another user all answer **404** — telling them apart is precisely the
    disclosure this feature must not make.
    """
    return _to_detail(reports.get_report(report_id, actor=actor))


@router.post(
    "/{report_id}/regenerate",
    response_model=ReportRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate this report again",
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_CONFLICT,
        **_THROTTLED,
        **_UNAVAILABLE,
    },
)
def regenerate_report(
    report_id: uuid.UUID, actor: Generator, reports: ReportServiceDep
) -> ReportRead:
    """Produce this report again from the case's current documents.

    **The same report, generated again** — not a new one. The identifier, the
    title, and the history entry are unchanged, so a link somebody saved keeps
    working and the list stays a list of reports rather than of attempts at one.
    `attempt_count` records how many times it has been produced.

    What changes is the content: documents uploaded, replaced, or indexed since
    the last run are now in scope, which is the whole point of the operation.

    **202 rather than 201**, unlike `POST /reports`: no resource is created here,
    the existing one has been accepted for reprocessing, and its sections are
    cleared until the new run writes them.

    The caller's access to the **case** is checked again, not merely their
    ownership of the report: a lawyer unassigned from a matter since the first run
    must not be able to produce a fresh interpretation of it from a link they
    still hold.
    """
    return _to_report(reports.regenerate(report_id, actor=actor))


@router.delete(
    "/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a report",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def delete_report(
    report_id: uuid.UUID, actor: Reader, reports: ReportServiceDep
) -> Response:
    """Withdraw a report from your history.

    It stops being readable and exportable through every endpoint immediately. The
    row is kept rather than destroyed, because the report carries the citations of
    an analysis a lawyer may have acted on — a future retention job is what
    reclaims it.

    Answers **204** with no body, and a second delete answers **404**: by then
    there is no report of that identifier belonging to anyone.
    """
    reports.delete_report(report_id, actor=actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{report_id}/export",
    status_code=status.HTTP_200_OK,
    summary="Export a report as a file",
    response_class=Response,
    responses={
        **_UNAUTHORIZED,
        **_FORBIDDEN,
        **_NOT_FOUND,
        **_CONFLICT,
        **_UNAVAILABLE,
        status.HTTP_200_OK: {
            "content": {"application/pdf": {}, "text/markdown": {}},
            "description": "The rendered report, as an attachment.",
        },
    },
)
def export_report(
    report_id: uuid.UUID,
    actor: Reader,
    reports: ReportServiceDep,
    export_format: Annotated[
        ReportFormat,
        Query(
            alias="format",
            description=(
                "`pdf` or `markdown`. `GET /reports/metrics` lists what this deployment can "
                "actually produce, so a client never offers a format the API will refuse."
            ),
        ),
    ] = ReportFormat.PDF,
) -> Response:
    """Download the report as a PDF or a Markdown file.

    **Rendered per request rather than served from storage**, which is what makes
    the spec's *"exported reports inherit the same permissions as their source
    case"* structural: there is no object anybody can be handed a URL to, and
    every byte is produced inside a request that has already resolved the report
    through an owner-scoped query. A shared link is a link to *this endpoint*,
    which will refuse whoever follows it.

    A report that has not finished generating answers **409**, not an empty file.
    A format whose rendering library is not installed on this deployment answers
    **503** naming Markdown, which needs nothing beyond the platform itself.

    **Arabic PDFs need a font.** ReportLab's built-in fonts cover Latin script
    only, so an Arabic report is refused rather than rendered as a page of empty
    boxes unless the deployment configures one. Markdown has no such limit.
    """
    export = reports.export(report_id, export_format, actor=actor)

    return Response(
        content=export.content,
        media_type=export.media_type,
        headers={
            # ASCII only, and deliberately: `export_filename` folds accents and
            # strips everything a header cannot safely carry, because this value
            # is derived from a report title a user may have typed. An RFC 5987
            # `filename*` parameter would preserve "Synthèse" — and would also be
            # a second, unsanitised copy of user input in the same header, which
            # is not a trade worth making for one accent.
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "Content-Length": str(export.size),
            # A generated report is one case's analysis. Nothing between here and
            # the user may keep a copy, and a shared cache in front of the API
            # would be exactly that.
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = ["router"]
