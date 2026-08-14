/**
 * Tests for the AI report generation client.
 *
 * Cover what the user is *shown* and what the client *sends*: the wire mapping,
 * the generate form and the catalogue it fetches, progress polling while a run is
 * in flight, the report a reader gets, export, deletion, the monitoring panel,
 * and what an unauthorized role gets instead.
 *
 * The API is the real boundary — its 401/403, the per-case assignment check, the
 * per-user history scope, and the generation itself are covered by
 * `tests/integration/test_reports.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { GenerateReportDialog } from "@/components/reports/generate-report-dialog";
import { ReportDetailDialog } from "@/components/reports/report-detail-dialog";
import { ReportList } from "@/components/reports/report-list";
import { ReportMetricsPanel } from "@/components/reports/report-metrics-panel";
import { ReportProgress } from "@/components/reports/report-progress";
import { ReportStatusBadge } from "@/components/reports/report-status-badge";
import { REPORT_ENDPOINTS } from "@/lib/api/config";
import {
  buildReportListParams,
  fetchReport,
  fetchReportMetrics,
  fetchReports,
  fetchReportTemplates,
} from "@/lib/api/reports";
import { ROUTES } from "@/lib/routes";
import { reportDetailSchema, reportMetricsSchema } from "@/lib/validation/report";
import { useSessionStore } from "@/stores/session-store";
import type { Report } from "@/types/report";
import { REPORT_STATUSES } from "@/types/report";
import en from "@/messages/en.json";
import { DEFAULT_REPORT_LIST_QUERY } from "@/types/report-management";
import type { UserRole } from "@/types/user";
import {
  errorEnvelope,
  mockFetch,
  reportDetailPayload,
  reportMetricsPayload,
  reportPagePayload,
  reportPayload,
  reportTemplatesPayload,
  sessionUserWithRole,
} from "./helpers";

vi.mock("next/navigation", () => ({
  usePathname: () => ROUTES.reports,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));

function signInAs(role: UserRole) {
  act(() => {
    useSessionStore.setState({ user: sessionUserWithRole(role), status: "authenticated" });
  });
}

function renderWithQuery(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  return {
    queryClient,
    ...render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>),
  };
}

/** The app's `Report`, built from the wire fixture the API would send. */
function reportFor(overrides: Record<string, unknown> = {}): Report {
  const payload = reportPayload(overrides);

  return {
    id: payload.id,
    caseId: payload.case_id,
    conversationId: payload.conversation_id,
    reportType: payload.report_type as Report["reportType"],
    title: payload.title,
    language: payload.language,
    status: payload.status as Report["status"],
    sectionsTotal: payload.sections_total,
    sectionsCompleted: payload.sections_completed,
    startedAt: payload.started_at,
    finishedAt: payload.finished_at,
    durationMs: payload.duration_ms,
    durationSeconds: payload.duration_seconds,
    attemptCount: payload.attempt_count,
    retrievedCount: payload.retrieved_count,
    contextCount: payload.context_count,
    groundedSections: payload.grounded_sections,
    characterCount: payload.character_count,
    provider: payload.provider,
    model: payload.model,
    promptName: payload.prompt_name,
    promptVersion: payload.prompt_version,
    templateVersion: payload.template_version,
    promptTokens: payload.prompt_tokens,
    completionTokens: payload.completion_tokens,
    totalTokens: payload.total_tokens,
    errorCode: payload.error_code,
    errorMessage: payload.error_message,
    exportCount: payload.export_count,
    lastExportedAt: payload.last_exported_at,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
    isTerminal: payload.is_terminal,
    isActive: payload.is_active,
    progressPercent: payload.progress_percent,
  };
}

// --------------------------------------------------------------------------- //
// The wire contract
// --------------------------------------------------------------------------- //

describe("report wire contract", () => {
  it("maps a report from snake_case to the app's shape", async () => {
    mockFetch({ [REPORT_ENDPOINTS.list]: { body: reportPagePayload() } });

    const page = await fetchReports(DEFAULT_REPORT_LIST_QUERY);

    expect(page.items[0]!.reportType).toBe("case_summary");
    expect(page.items[0]!.sectionsTotal).toBe(4);
    expect(page.items[0]!.isTerminal).toBe(true);
    expect(page.items[0]!.progressPercent).toBe(100);
  });

  it("maps sections and citations onto the detail shape", async () => {
    mockFetch({ "/reports/": { body: reportDetailPayload() } });

    const report = await fetchReport(reportPayload().id);

    expect(report.sections).toHaveLength(2);
    expect(report.sections[0]!.citationMarkers).toEqual([1]);
    expect(report.citations[0]!.documentName).toBe("bail-commercial.pdf");
    expect(report.disclaimer).toContain("conseil juridique");
  });

  it("keeps the citation shape the pipeline produced", () => {
    /* The API returns the pipeline's own citation objects with only the marker
       renumbered; a schema that dropped a field here would silently strip the
       provenance a legal citation is for. */
    const parsed = reportDetailSchema.parse(reportDetailPayload());

    expect(parsed.citations[0]).toMatchObject({
      document_name: expect.any(String),
      document_version: expect.any(Number),
      page_number: expect.any(Number),
      case_id: expect.any(String),
    });
  });

  it("maps the metrics without a `since` window", () => {
    /* Unlike search, RAG, and the assistant: every figure is a SQL aggregate
       over persisted rows, so there is no process-lifetime caveat to report. */
    const parsed = reportMetricsSchema.parse(reportMetricsPayload());

    expect(parsed).not.toHaveProperty("since");
    expect(parsed.total_exports).toBe(4);
  });

  it("maps the template catalogue", async () => {
    mockFetch({ [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() } });

    const templates = await fetchReportTemplates();

    expect(templates[0]!.reportType).toBe("case_summary");
    expect(templates[0]!.sections.map((section) => section.key)).toEqual([
      "overview",
      "parties",
    ]);
  });

  it("omits 'any' filters from the query string", () => {
    /* So the request URL reflects what is actually being asked — which also makes
       the query a stable cache key. */
    const params = buildReportListParams(DEFAULT_REPORT_LIST_QUERY);

    expect(params).toContain("page=1");
    expect(params).toContain("sort_by=created_at");
    expect(params).not.toContain("status=");
    expect(params).not.toContain("report_type=");
  });

  it("sends the filters that are set", () => {
    const params = buildReportListParams({
      ...DEFAULT_REPORT_LIST_QUERY,
      status: "failed",
      reportType: "evidence_summary",
      caseId: "case-1",
      search: "audience",
    });

    expect(params).toContain("status=failed");
    expect(params).toContain("report_type=evidence_summary");
    expect(params).toContain("case_id=case-1");
    expect(params).toContain("search=audience");
  });

  it("sends only what a generate request carries", async () => {
    /* No retrieval tuning: the API accepts top-K, a similarity floor, and
       document filters, and none of them belongs in a form a lawyer fills in. */
    const { requests } = mockFetch({ [REPORT_ENDPOINTS.create]: { body: reportPayload() } });
    const { generateReport } = await import("@/lib/api/reports");

    await generateReport({ caseId: "case-1", reportType: "case_summary", language: "fr" });

    expect(requests.at(-1)!.method).toBe("POST");
    expect(requests.at(-1)!.body).toEqual({
      case_id: "case-1",
      report_type: "case_summary",
      language: "fr",
    });
  });

  it("requests metrics for a window when one is given", async () => {
    const { requests } = mockFetch({
      [REPORT_ENDPOINTS.metrics]: { body: reportMetricsPayload() },
    });

    await fetchReportMetrics(30);

    expect(requests.at(-1)!.url).toContain("window_days=30");
  });
});

// --------------------------------------------------------------------------- //
// Progress
// --------------------------------------------------------------------------- //

describe("report progress", () => {
  it("shows nothing for a finished run", () => {
    /* A progress bar beside a finished report is a control that can only
       mislead. */
    const { container } = render(<ReportProgress report={reportFor()} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("says a queued run is waiting rather than showing zero percent", () => {
    /* Zero on a bar reads as "started and got nowhere", which is a different and
       more alarming thing than "waiting for a worker". */
    render(<ReportProgress report={reportFor({ status: "pending" })} />);

    expect(screen.getByText(/queued/i)).toBeInTheDocument();
  });

  it("counts sections rather than estimating a time", () => {
    /* A time estimate would be a guess about a language model's latency, and a
       wrong one is worse than none. */
    render(
      <ReportProgress
        report={reportFor({ status: "processing", sections_total: 7, sections_completed: 2 })}
      />,
    );

    expect(screen.getByText(/section 3 of 7/i)).toBeInTheDocument();
  });

  it("exposes the real values to assistive technology", () => {
    render(
      <ReportProgress
        report={reportFor({ status: "processing", sections_total: 4, sections_completed: 2 })}
      />,
    );

    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "50");
    expect(bar).toHaveAttribute("aria-valuemax", "100");
  });
});

describe("report status badge", () => {
  it.each(REPORT_STATUSES)("labels %s as text, not colour alone", (status) => {
    /* WCAG: the state is never conveyed by colour or shape alone — every badge
       carries its label and its icon is aria-hidden. */
    const { container } = render(<ReportStatusBadge status={status} />);

    expect(container.textContent?.trim()).not.toBe("");
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
});

// --------------------------------------------------------------------------- //
// Generating
// --------------------------------------------------------------------------- //

describe("generate report dialog", () => {
  it("offers the report types the server advertises", async () => {
    signInAs("lawyer");
    mockFetch({ [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() } });

    renderWithQuery(
      <GenerateReportDialog open onOpenChange={() => {}} caseId="case-1" />,
    );

    await waitFor(() =>
      expect(screen.getByText("Vue complète du dossier, de bout en bout.")).toBeInTheDocument(),
    );
  });

  it("shows the sections a report will contain before it is generated", async () => {
    /* A report costs a model call per section and takes minutes, so "what am I
       about to get" must be answerable before pressing the button. */
    signInAs("lawyer");
    mockFetch({ [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() } });

    renderWithQuery(
      <GenerateReportDialog open onOpenChange={() => {}} caseId="case-1" />,
    );

    await waitFor(() => expect(screen.getByText(/1\. Aperçu/)).toBeInTheDocument());
    expect(screen.getByText(/2\. Parties/)).toBeInTheDocument();
  });

  it("hides the case picker when generating from inside a case", async () => {
    signInAs("lawyer");
    mockFetch({ [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() } });

    renderWithQuery(
      <GenerateReportDialog open onOpenChange={() => {}} caseId="case-1" />,
    );

    await waitFor(() => expect(screen.getByLabelText("Report type")).toBeInTheDocument());
    expect(screen.queryByLabelText("Case")).not.toBeInTheDocument();
  });

  it("sends the case, the type, and the language", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() },
      [REPORT_ENDPOINTS.create]: { body: reportPayload({ status: "pending" }) },
    });

    renderWithQuery(
      <GenerateReportDialog open onOpenChange={() => {}} caseId="case-1" />,
    );
    await waitFor(() => expect(screen.getByLabelText("Report type")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /generate/i }));

    await waitFor(() => {
      const created = requests.find((request) => request.method === "POST");
      expect(created?.body).toMatchObject({
        case_id: "case-1",
        report_type: "case_summary",
      });
    });
  });

  it("reports a refusal against the form rather than losing it", async () => {
    signInAs("lawyer");
    mockFetch({
      [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() },
      [REPORT_ENDPOINTS.create]: {
        status: 429,
        body: errorEnvelope(
          "too_many_active_reports",
          "You already have 3 reports being generated. Wait for one to finish, then try again.",
        ),
      },
    });

    renderWithQuery(
      <GenerateReportDialog open onOpenChange={() => {}} caseId="case-1" />,
    );
    await waitFor(() => expect(screen.getByLabelText("Report type")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /generate/i }));

    /* The platform's own sentence rather than the server's verbatim one. The
       server knows the exact count and writes it in English; since
       `21-localization.md` an English sentence is not something to put on an
       Arabic screen, so the code selects a translated message and the count
       stays in the log. */
    expect(await screen.findByRole("alert")).toHaveTextContent(
      en.reports.errors.tooManyActive,
    );
  });
});

// --------------------------------------------------------------------------- //
// Reading a report
// --------------------------------------------------------------------------- //

describe("report detail", () => {
  it("renders the sections in template order", async () => {
    signInAs("lawyer");
    mockFetch({ "/reports/": { body: reportDetailPayload() } });

    renderWithQuery(
      <ReportDetailDialog reportId={reportPayload().id} open onOpenChange={() => {}} />,
    );

    await waitFor(() => expect(screen.getByText("Aperçu")).toBeInTheDocument());
    expect(screen.getByText("Parties")).toBeInTheDocument();
  });

  it("marks a section the case file does not cover", async () => {
    /* Hiding it would leave a reader to conclude the report forgot to mention
       the parties; the badge says plainly that the documents do not support it. */
    signInAs("lawyer");
    mockFetch({ "/reports/": { body: reportDetailPayload() } });

    renderWithQuery(
      <ReportDetailDialog reportId={reportPayload().id} open onOpenChange={() => {}} />,
    );

    expect(await screen.findByText(/not covered/i)).toBeInTheDocument();
  });

  it("renders the prose as written rather than as markup", async () => {
    /* Interpreting generated text as Markdown would mean deciding what to do
       with a `[1]` citation marker and a `#` from a statute reference. */
    signInAs("lawyer");
    mockFetch({ "/reports/": { body: reportDetailPayload() } });

    renderWithQuery(
      <ReportDetailDialog reportId={reportPayload().id} open onOpenChange={() => {}} />,
    );

    expect(
      await screen.findByText("Le litige porte sur un bail commercial [1]."),
    ).toBeInTheDocument();
  });

  it("shows the reference list with document, page, and version", async () => {
    signInAs("lawyer");
    mockFetch({ "/reports/": { body: reportDetailPayload() } });

    renderWithQuery(
      <ReportDetailDialog reportId={reportPayload().id} open onOpenChange={() => {}} />,
    );

    expect(await screen.findByText(/bail-commercial\.pdf/)).toBeInTheDocument();
  });

  it("always shows the platform's disclaimer", async () => {
    /* A document that looks like a lawyer's work product and is not must say so
       on its face. */
    signInAs("lawyer");
    mockFetch({ "/reports/": { body: reportDetailPayload() } });

    renderWithQuery(
      <ReportDetailDialog reportId={reportPayload().id} open onOpenChange={() => {}} />,
    );

    expect(await screen.findByText(/ne constitue pas un conseil juridique/i)).toBeInTheDocument();
  });

  it("shows progress instead of empty headings while a run is in flight", async () => {
    signInAs("lawyer");
    mockFetch({
      "/reports/": {
        body: { ...reportDetailPayload({ status: "processing" }), sections: [], citations: [] },
      },
    });

    renderWithQuery(
      <ReportDetailDialog reportId={reportPayload().id} open onOpenChange={() => {}} />,
    );

    expect(await screen.findByRole("progressbar")).toBeInTheDocument();
    expect(screen.queryByText("Aperçu")).not.toBeInTheDocument();
  });

  it("explains a failure and keeps Regenerate available", async () => {
    /* Almost every failure here is transient, and the remedy is one button. */
    signInAs("lawyer");
    mockFetch({
      "/reports/": {
        body: {
          ...reportDetailPayload({
            status: "failed",
            error_code: "llm_unavailable",
            error_message: "The AI service is unavailable.",
          }),
          sections: [],
          citations: [],
        },
      },
    });

    renderWithQuery(
      <ReportDetailDialog reportId={reportPayload().id} open onOpenChange={() => {}} />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      en.reports.failures.llm_unavailable,
    );
    expect(screen.getByRole("button", { name: /regenerate/i })).toBeInTheDocument();
  });

  it("offers no export while a run is unfinished", async () => {
    /* The API answers 409, and a Download beside a progress bar reads as
       broken. */
    signInAs("lawyer");
    mockFetch({
      "/reports/": {
        body: { ...reportDetailPayload({ status: "processing" }), sections: [], citations: [] },
      },
      [REPORT_ENDPOINTS.metrics]: { body: reportMetricsPayload() },
    });

    renderWithQuery(
      <ReportDetailDialog reportId={reportPayload().id} open onOpenChange={() => {}} />,
    );

    await waitFor(() => expect(screen.getByRole("progressbar")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /export/i })).not.toBeInTheDocument();
  });

  it("says plainly when a report is not the caller's", async () => {
    /* 404 covers "deleted" and "somebody else's" alike — the API deliberately
       does not say which. */
    signInAs("lawyer");
    mockFetch({
      "/reports/": {
        status: 404,
        body: errorEnvelope("report_not_found", "Report not found."),
      },
    });

    renderWithQuery(
      <ReportDetailDialog reportId={reportPayload().id} open onOpenChange={() => {}} />,
    );

    expect(await screen.findByText(/no longer available/i)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// The history
// --------------------------------------------------------------------------- //

describe("report list", () => {
  it("lists the caller's reports", async () => {
    signInAs("lawyer");
    mockFetch({
      [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() },
      [REPORT_ENDPOINTS.list]: { body: reportPagePayload() },
    });

    renderWithQuery(<ReportList />);

    expect(
      await screen.findByRole("button", { name: reportPayload().title }),
    ).toBeInTheDocument();
  });

  it("labels each row with its report type", async () => {
    signInAs("lawyer");
    mockFetch({
      [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() },
      [REPORT_ENDPOINTS.list]: { body: reportPagePayload() },
    });

    renderWithQuery(<ReportList />);

    expect(await screen.findByText(en.reports.types.case_summary)).toBeInTheDocument();
  });

  it("offers Generate when the history is empty", async () => {
    signInAs("lawyer");
    mockFetch({
      [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() },
      [REPORT_ENDPOINTS.list]: { body: reportPagePayload([], { total_records: 0 }) },
    });

    renderWithQuery(<ReportList />);

    expect(await screen.findByText(/no reports yet/i)).toBeInTheDocument();
  });

  it("distinguishes 'no results' from 'no reports yet'", async () => {
    signInAs("lawyer");
    mockFetch({
      [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() },
      [REPORT_ENDPOINTS.list]: { body: reportPagePayload([], { total_records: 0 }) },
    });

    renderWithQuery(<ReportList />);
    await screen.findByText(/no reports yet/i);

    await userEvent.type(screen.getByLabelText("Search"), "audience");
    await userEvent.click(screen.getByRole("button", { name: /^search$/i }));

    expect(await screen.findByText(/no matching reports/i)).toBeInTheDocument();
  });

  it("scopes the request to one case inside a case workspace", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() },
      [REPORT_ENDPOINTS.list]: { body: reportPagePayload() },
    });

    renderWithQuery(<ReportList caseId="case-42" />);

    await waitFor(() =>
      expect(
        requests.some((request) => request.url.includes("case_id=case-42")),
      ).toBe(true),
    );
  });

  it("hides the Generate control from a role that cannot generate", async () => {
    /* A court representative holds no AI capability at all — the one place this
       platform draws a line between reading the case file and generating an
       interpretation of it. */
    signInAs("court");
    mockFetch({
      [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() },
      [REPORT_ENDPOINTS.list]: { body: reportPagePayload() },
    });

    renderWithQuery(<ReportList />);

    await screen.findByRole("button", { name: reportPayload().title });
    expect(screen.queryByRole("button", { name: /generate report/i })).not.toBeInTheDocument();
  });

  it("shows a retry when the history cannot be loaded", async () => {
    signInAs("lawyer");
    mockFetch({
      [REPORT_ENDPOINTS.templates]: { body: reportTemplatesPayload() },
      [REPORT_ENDPOINTS.list]: {
        status: 503,
        body: errorEnvelope("service_unavailable", "A required service is unavailable."),
      },
    });

    renderWithQuery(<ReportList />);

    expect(await screen.findByText(/reports could not be loaded/i)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Monitoring
// --------------------------------------------------------------------------- //

describe("report metrics panel", () => {
  it("shows the six figures the spec names", async () => {
    signInAs("administrator");
    mockFetch({ [REPORT_ENDPOINTS.metrics]: { body: reportMetricsPayload() } });

    renderWithQuery(<ReportMetricsPanel />);

    await waitFor(() => expect(screen.getByText("Reports generated")).toBeInTheDocument());
    expect(screen.getByText("Average time")).toBeInTheDocument();
    expect(screen.getByText("Exports")).toBeInTheDocument();
    expect(screen.getByText("Failures")).toBeInTheDocument();
    expect(screen.getByText("Average size")).toBeInTheDocument();
    expect(screen.getByText("Tokens used")).toBeInTheDocument();
  });

  it("warns when no AI provider is reachable", async () => {
    /* A platform generating nothing because no credential is configured and one
       nobody has asked yet show the same zeros. */
    signInAs("administrator");
    mockFetch({
      [REPORT_ENDPOINTS.metrics]: { body: reportMetricsPayload({ llm_available: false }) },
    });

    renderWithQuery(<ReportMetricsPanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/no ai provider is reachable/i);
  });

  it("says when generation is switched off rather than showing an alarm", async () => {
    signInAs("administrator");
    mockFetch({
      [REPORT_ENDPOINTS.metrics]: { body: reportMetricsPayload({ enabled: false }) },
    });

    renderWithQuery(<ReportMetricsPanel />);

    expect(await screen.findByText(/generation is disabled/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("names which export formats this deployment can produce", async () => {
    /* So a client never offers an export the API will refuse. */
    signInAs("administrator");
    mockFetch({
      [REPORT_ENDPOINTS.metrics]: {
        body: reportMetricsPayload({ available_formats: ["markdown"] }),
      },
    });

    renderWithQuery(<ReportMetricsPanel />);

    expect(await screen.findByText(/export formats available here: markdown/i)).toBeInTheDocument();
  });

  it("renders nothing when the caller may not read it", async () => {
    /* The panel has no useful "you cannot see this" state, so the 403 is
       swallowed rather than turned into an error card. */
    signInAs("lawyer");
    mockFetch({
      [REPORT_ENDPOINTS.metrics]: {
        status: 403,
        body: errorEnvelope("forbidden", "You do not have permission."),
      },
    });

    const { container } = renderWithQuery(<ReportMetricsPanel />);

    await waitFor(() => expect(container.querySelector("[aria-busy]")).toBeNull());
  });
});
