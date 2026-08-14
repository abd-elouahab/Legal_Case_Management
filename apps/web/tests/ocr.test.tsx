/**
 * Tests for the OCR Processing client.
 *
 * Cover what the user is *shown* and what the client *sends*: the status panel,
 * polling while a run is in flight, the extracted text with its page boundaries
 * intact, the retry action and when it is offered, the monitoring panel, and what
 * an unauthorized role gets instead.
 *
 * The API is the real boundary — its 401/403, the per-case assignment check, the
 * concurrency guarantee, and the extraction itself are covered by
 * `tests/integration/test_ocr.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DocumentOcrPanel } from "@/components/ocr/document-ocr-panel";
import { OcrMetricsPanel } from "@/components/ocr/ocr-metrics-panel";
import { OcrStatusBadge } from "@/components/ocr/ocr-status-badge";
import { OcrTextView } from "@/components/ocr/ocr-text-view";
import { buildOcrListParams, fetchOcrResult, fetchOcrText } from "@/lib/api/ocr";
import { OCR_ENDPOINTS } from "@/lib/api/config";
import { ROUTES } from "@/lib/routes";
import { ocrMetricsSchema, ocrResultSchema, ocrTextSchema } from "@/lib/validation/ocr";
import { useSessionStore } from "@/stores/session-store";
import { PERMISSIONS } from "@/types/authorization";
import { DEFAULT_OCR_LIST_QUERY } from "@/types/ocr-management";
import { OCR_FAILURE_CODES, OCR_STATUSES, isOcrSupported } from "@/types/ocr";
import { TIMELINE_EVENT_TYPES } from "@/types/timeline";
import en from "@/messages/en.json";
import type { LegalDocument } from "@/types/document";
import type { UserRole } from "@/types/user";
import {
  errorEnvelope,
  legalDocumentPayload,
  mockFetch,
  ocrMetricsPayload,
  ocrResultPayload,
  ocrTextPayload,
  sessionUserWithRole,
} from "./helpers";

vi.mock("next/navigation", () => ({
  usePathname: () => ROUTES.documents,
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

/** The app's `LegalDocument`, built from the wire fixture the API would send. */
function documentFor(overrides: Record<string, unknown> = {}): LegalDocument {
  const payload = legalDocumentPayload(overrides);

  return {
    id: payload.id,
    caseId: payload.case_id,
    case: null,
    originalFilename: payload.original_filename,
    storedFilename: payload.stored_filename,
    fileExtension: payload.file_extension,
    mimeType: payload.mime_type,
    fileSize: payload.file_size,
    fileSizeLabel: payload.file_size_label,
    storageBucket: payload.storage_bucket,
    storageKey: payload.storage_key,
    category: payload.category as LegalDocument["category"],
    description: payload.description,
    version: payload.version,
    versionCount: payload.version_count,
    uploadedBy: payload.uploaded_by,
    uploader: null,
    uploadedAt: payload.uploaded_at,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
    deletedAt: payload.deleted_at,
    isDeleted: payload.is_deleted,
    isPreviewable: payload.is_previewable,
    versions: [],
  };
}

describe("OCR wire contract", () => {
  it("maps a run from snake_case to the app's shape", async () => {
    mockFetch({ "/ocr": { body: ocrResultPayload() } });

    const result = await fetchOcrResult(legalDocumentPayload().id);

    expect(result.documentVersion).toBe(1);
    expect(result.detectedLanguage).toBe("eng+fra+ara");
    expect(result.durationSeconds).toBe(3.12);
    expect(result.canRetry).toBe(true);
  });

  it("addresses a specific document version", async () => {
    const { requests } = mockFetch({ "/ocr": { body: ocrResultPayload() } });

    await fetchOcrResult(legalDocumentPayload().id, 3);

    expect(requests[0]?.url).toContain("version=3");
  });

  it("keeps the version out of the URL when it is not given", () => {
    // Omitted means "the current version", which is what the API does when the
    // parameter is absent — sending `version=undefined` would be a 422.
    expect(OCR_ENDPOINTS.status("abc")).not.toContain("version");
  });

  it("preserves page boundaries through the mapping", async () => {
    mockFetch({ "/ocr/text": { body: ocrTextPayload() } });

    const text = await fetchOcrText(legalDocumentPayload().id);

    expect(text.pages.map((page) => page.pageNumber)).toEqual([1, 2]);
    expect(text.fullText.split(text.pageSeparator)).toEqual(
      text.pages.map((page) => page.text),
    );
  });

  it("preserves multilingual text", async () => {
    mockFetch({ "/ocr/text": { body: ocrTextPayload() } });

    const text = await fetchOcrText(legalDocumentPayload().id);

    expect(text.pages[1]?.text).toBe("محضر الجلسة");
  });

  it("rejects a response with an unknown status", () => {
    // The lifecycle is closed and enforced by a database enum on the server, so
    // an unrecognised value is a genuine contract break rather than a newer
    // backend being ahead of this build.
    expect(() => ocrResultSchema.parse(ocrResultPayload({ status: "halfway" }))).toThrow();
  });

  it("accepts a failure code it has never heard of", () => {
    // The opposite reasoning: a future engine may report a new cause, and the
    // API always sends a human-readable message beside it.
    const parsed = ocrResultSchema.parse(
      ocrResultPayload({ status: "failed", error_code: "handwriting_unsupported" }),
    );

    expect(parsed.error_code).toBe("handwriting_unsupported");
  });

  it("parses the text and metrics payloads", () => {
    expect(() => ocrTextSchema.parse(ocrTextPayload())).not.toThrow();
    expect(() => ocrMetricsSchema.parse(ocrMetricsPayload())).not.toThrow();
  });

  it("omits blank filters from the list query", () => {
    const params = buildOcrListParams({ ...DEFAULT_OCR_LIST_QUERY, status: "failed" });

    expect(params).toContain("status=failed");
    expect(params).not.toContain("case_id");
  });
});

describe("format policy", () => {
  it("applies to PDFs and images", () => {
    for (const extension of ["pdf", "png", "jpg", "jpeg", "PDF"]) {
      expect(isOcrSupported(extension)).toBe(true);
    }
  });

  it("does not apply to files that already carry text", () => {
    for (const extension of ["docx", "doc", "txt"]) {
      expect(isOcrSupported(extension)).toBe(false);
    }
  });
});

// Catalogue entries since `21-localization.md` — see the equivalent note in
// `tests/indexing.test.tsx`. The fallback for an unrecognised code is the
// provider's and is covered in `tests/localization.test.tsx`.
describe("failure labels", () => {
  it("names every failure cause the platform defines", () => {
    for (const code of OCR_FAILURE_CODES) {
      expect(en.ocr.failures).toHaveProperty(code);
    }
  });

  it("says what went wrong rather than naming an error class", () => {
    expect(en.ocr.failures.timeout).toBe("Took too long");
  });
});

describe("status badge", () => {
  it("names every status as text, never by colour alone", () => {
    for (const status of OCR_STATUSES) {
      const { unmount } = render(<OcrStatusBadge status={status} />);
      expect(screen.getByText(/queued|extracting|completed|failed/i)).toBeInTheDocument();
      unmount();
    }
  });
});

describe("extracted text view", () => {
  const text = {
    ocrResultId: "r1",
    documentId: "d1",
    documentVersion: 1,
    status: "completed" as const,
    detectedLanguage: "eng+fra",
    pageCount: 2,
    characterCount: 10,
    fullText: "one\ftwo",
    pageSeparator: "\f",
    pages: [
      { pageNumber: 1, text: "one", confidence: 90, characterCount: 3, isEmpty: false },
      { pageNumber: 2, text: "two", confidence: null, characterCount: 3, isEmpty: false },
    ],
  };

  it("renders each page with its number", () => {
    render(<OcrTextView text={text} />);

    expect(screen.getByText("Page 1")).toBeInTheDocument();
    expect(screen.getByText("Page 2")).toBeInTheDocument();
  });

  it("shows a page that produced nothing as empty rather than omitting it", () => {
    // Dropping a blank page 2 would renumber every page after it.
    render(
      <OcrTextView
        text={{
          ...text,
          pages: [
            { pageNumber: 1, text: "", confidence: null, characterCount: 0, isEmpty: true },
            text.pages[1]!,
          ],
        }}
      />,
    );

    expect(screen.getByText("Page 1")).toBeInTheDocument();
    expect(screen.getByText(/produced no text/i)).toBeInTheDocument();
  });

  it("explains a run that extracted nothing at all", () => {
    render(<OcrTextView text={{ ...text, pages: [], pageCount: 0, fullText: "" }} />);

    expect(screen.getByText(/no text was extracted/i)).toBeInTheDocument();
  });

  it("offers the whole text for copying", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });

    render(<OcrTextView text={text} />);
    await userEvent.click(screen.getByRole("button", { name: /copy all text/i }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("one\ftwo"));
  });
});

describe("document OCR panel", () => {
  it("says extraction does not apply to a Word file", () => {
    signInAs("administrator");
    mockFetch({ "/ocr": { body: ocrResultPayload() } });

    renderWithQuery(
      <DocumentOcrPanel document={documentFor({ file_extension: "docx" })} />,
    );

    expect(screen.getByText(/applies to PDFs and images/i)).toBeInTheDocument();
  });

  it("shows the run's status and metadata", async () => {
    signInAs("administrator");
    mockFetch({ "/ocr": { body: ocrResultPayload() } });

    renderWithQuery(<DocumentOcrPanel document={documentFor()} />);

    expect(await screen.findByText("Completed")).toBeInTheDocument();
    expect(await screen.findByText("92%")).toBeInTheDocument();
    expect(screen.getByText("eng+fra+ara")).toBeInTheDocument();
  });

  it("treats a missing record as 'not processed yet', not an error", async () => {
    // A document uploaded before this feature existed, or while it was disabled.
    signInAs("administrator");
    mockFetch({
      "/ocr": { status: 404, body: errorEnvelope("ocr_result_not_found", "No record.") },
    });

    renderWithQuery(<DocumentOcrPanel document={documentFor()} />);

    expect(await screen.findByText(/has not been processed yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("explains a failure and says the document is unaffected", async () => {
    signInAs("administrator");
    mockFetch({
      "/ocr": {
        body: ocrResultPayload({
          status: "failed",
          error_code: "timeout",
          error_message: "Text extraction took too long and was stopped.",
        }),
      },
    });

    renderWithQuery(<DocumentOcrPanel document={documentFor()} />);

    expect(await screen.findByText("Took too long")).toBeInTheDocument();
    expect(screen.getByText(/took too long and was stopped/i)).toBeInTheDocument();
    expect(screen.getByText(/document itself is unaffected/i)).toBeInTheDocument();
  });

  it("tells the reader a queued run will update itself", async () => {
    signInAs("administrator");
    mockFetch({ "/ocr": { body: ocrResultPayload({ status: "pending" }) } });

    renderWithQuery(<DocumentOcrPanel document={documentFor()} />);

    expect(await screen.findByText(/queued for extraction/i)).toBeInTheDocument();
  });

  it("loads the text only when the reader asks for it", async () => {
    signInAs("administrator");
    const { requests } = mockFetch({
      "/ocr/text": { body: ocrTextPayload() },
      "/ocr": { body: ocrResultPayload() },
    });

    renderWithQuery(<DocumentOcrPanel document={documentFor()} />);
    await screen.findByText("Completed");

    // A details dialog that fetched a 100-page extraction on open would pay for
    // it every time someone checked a file size.
    expect(requests.some((request) => request.url.includes("/ocr/text"))).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: /view extracted text/i }));

    await waitFor(() =>
      expect(requests.some((request) => request.url.includes("/ocr/text"))).toBe(true),
    );
    expect(await screen.findByText("Page 1")).toBeInTheDocument();
  });

  it("offers Retry to a lawyer and sends it to the right endpoint", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch({
      "/ocr/retry": { status: 202, body: ocrResultPayload({ status: "pending" }) },
      "/ocr": { body: ocrResultPayload() },
    });

    renderWithQuery(<DocumentOcrPanel document={documentFor()} />);
    await userEvent.click(await screen.findByRole("button", { name: /retry extraction/i }));

    await waitFor(() => {
      const retry = requests.find((request) => request.url.includes("/ocr/retry"));
      expect(retry?.method).toBe("POST");
      // No file travels: the API re-reads what is already stored.
      expect(retry?.body).toBeUndefined();
    });
  });

  it("does not offer Retry while a run is already in flight", async () => {
    // The API answers 409, and a button that produces an error the user could
    // not have predicted is worse than no button.
    signInAs("administrator");
    mockFetch({ "/ocr": { body: ocrResultPayload({ status: "processing" }) } });

    renderWithQuery(<DocumentOcrPanel document={documentFor()} />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /retry extraction/i })).toBeDisabled(),
    );
  });

  it("hides Retry from a court representative", async () => {
    signInAs("court");
    mockFetch({ "/ocr": { body: ocrResultPayload() } });

    renderWithQuery(<DocumentOcrPanel document={documentFor()} />);
    await screen.findByText("Completed");

    // They hold `ocr:view` but not `ocr:retry` — the UI must not offer an action
    // the API would refuse.
    expect(screen.queryByRole("button", { name: /retry extraction/i })).toBeNull();
  });
});

describe("metrics panel", () => {
  it("reports the rates and the average time", async () => {
    signInAs("administrator");
    mockFetch({ "/ocr/metrics": { body: ocrMetricsPayload() } });

    renderWithQuery(<OcrMetricsPanel />);

    expect(await screen.findByText("80%")).toBeInTheDocument();
    expect(screen.getByText("20%")).toBeInTheDocument();
    expect(screen.getByText("3.12s")).toBeInTheDocument();
  });

  it("groups failures by cause", async () => {
    signInAs("administrator");
    mockFetch({ "/ocr/metrics": { body: ocrMetricsPayload() } });

    renderWithQuery(<OcrMetricsPanel />);

    expect(await screen.findByText("Took too long")).toBeInTheDocument();
    expect(screen.getByText("Extraction service failed")).toBeInTheDocument();
  });

  it("warns when the engine itself is unreachable", async () => {
    // A missing install and a stack of unreadable scans produce the same failure
    // rate and need entirely different responses.
    signInAs("administrator");
    mockFetch({ "/ocr/metrics": { body: ocrMetricsPayload({ engine_available: false }) } });

    renderWithQuery(<OcrMetricsPanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/not reachable/i);
  });

  it("says so when extraction is switched off", async () => {
    signInAs("administrator");
    mockFetch({ "/ocr/metrics": { body: ocrMetricsPayload({ enabled: false }) } });

    renderWithQuery(<OcrMetricsPanel />);

    expect(await screen.findByText(/disabled on this deployment/i)).toBeInTheDocument();
  });

  it("stays silent rather than showing an error when it is refused", async () => {
    // Rendered behind an `ocr:monitor` gate; a 403 that slipped through has no
    // useful state to show a user who did not ask for it.
    signInAs("administrator");
    mockFetch({
      "/ocr/metrics": { status: 403, body: errorEnvelope("forbidden", "Refused.") },
    });

    const { container } = renderWithQuery(<OcrMetricsPanel />);

    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});

describe("permissions and timeline registry", () => {
  it("defines the three OCR permissions", () => {
    for (const permission of ["ocr:view", "ocr:retry", "ocr:monitor"]) {
      expect(PERMISSIONS).toContain(permission);
    }
  });

  it("labels the four OCR timeline events without an acronym", () => {
    for (const eventType of ["ocr_started", "ocr_completed", "ocr_failed", "ocr_retried"]) {
      expect(TIMELINE_EVENT_TYPES).toContain(eventType);
      expect(
        en.timeline.events[eventType as keyof typeof en.timeline.events],
      ).toMatch(/text extraction/i);
    }
  });
});
