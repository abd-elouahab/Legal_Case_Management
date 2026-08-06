/**
 * Tests for the Document Indexing client.
 *
 * Cover what the user is *shown* and what the client *sends*: the index panel,
 * polling while a run is in flight, the metadata a reader needs, the rebuild
 * action and when it is offered, the monitoring panel, and what an unauthorized
 * role gets instead.
 *
 * The API is the real boundary — its 401/403, the per-case assignment check, the
 * concurrency guarantee, and the indexing itself are covered by
 * `tests/integration/test_indexing.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DocumentIndexPanel } from "@/components/indexing/document-index-panel";
import { IndexMetricsPanel } from "@/components/indexing/index-metrics-panel";
import { IndexStatusBadge } from "@/components/indexing/index-status-badge";
import {
  buildIndexListParams,
  fetchDocumentIndex,
  fetchDocumentIndexHistory,
  fetchIndexMetrics,
} from "@/lib/api/indexing";
import { INDEXING_ENDPOINTS } from "@/lib/api/config";
import { ROUTES } from "@/lib/routes";
import { documentIndexSchema, indexMetricsSchema } from "@/lib/validation/indexing";
import { useSessionStore } from "@/stores/session-store";
import { DEFAULT_INDEX_LIST_QUERY } from "@/types/indexing-management";
import {
  INDEX_STATUSES,
  indexFailureLabel,
  indexLanguageLabel,
} from "@/types/indexing";
import type { LegalDocument } from "@/types/document";
import type { UserRole } from "@/types/user";
import {
  documentIndexPayload,
  errorEnvelope,
  indexMetricsPayload,
  legalDocumentPayload,
  mockFetch,
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

describe("indexing wire contract", () => {
  it("maps an index from snake_case to the app's shape", async () => {
    mockFetch({ "/index": { body: documentIndexPayload() } });

    const index = await fetchDocumentIndex(legalDocumentPayload().id);

    expect(index.documentVersion).toBe(1);
    expect(index.chunkCount).toBe(14);
    expect(index.embeddingModel).toBe("BAAI/bge-m3");
    expect(index.embeddingDimensions).toBe(1024);
    expect(index.durationSeconds).toBe(14.2);
    expect(index.canReindex).toBe(true);
  });

  it("addresses a specific document version", async () => {
    const { requests } = mockFetch({ "/index": { body: documentIndexPayload() } });

    await fetchDocumentIndex(legalDocumentPayload().id, 3);

    expect(requests[0]?.url).toContain("version=3");
  });

  it("keeps the version out of the URL when it is not given", () => {
    // Omitted means "the current version", which is what the API does when the
    // parameter is absent — sending `version=undefined` would be a 422.
    expect(INDEXING_ENDPOINTS.status("abc")).not.toContain("version");
  });

  it("maps the history oldest version first", async () => {
    mockFetch({
      "/index/history": {
        body: [
          documentIndexPayload({ document_version: 1 }),
          documentIndexPayload({ id: "other", document_version: 2 }),
        ],
      },
    });

    const history = await fetchDocumentIndexHistory(legalDocumentPayload().id);

    expect(history.map((entry) => entry.documentVersion)).toEqual([1, 2]);
  });

  it("maps the metrics payload", async () => {
    mockFetch({ "/indexing/metrics": { body: indexMetricsPayload() } });

    const metrics = await fetchIndexMetrics();

    expect(metrics.totalChunks).toBe(96);
    expect(metrics.averageChunksPerDocument).toBe(12);
    expect(metrics.embeddingAvailable).toBe(true);
    expect(metrics.vectorCollection).toBe("document_chunks");
  });

  it("rejects a response with an unknown status", () => {
    // The lifecycle is closed and enforced by a database enum on the server, so
    // an unrecognised value is a genuine contract break rather than a newer
    // backend being ahead of this build.
    expect(() =>
      documentIndexSchema.parse(documentIndexPayload({ status: "halfway" })),
    ).toThrow();
  });

  it("accepts a failure code it has never heard of", () => {
    // The opposite reasoning: a future embedding backend may report a new cause,
    // and the API always sends a human-readable message beside it.
    const parsed = documentIndexSchema.parse(
      documentIndexPayload({ status: "failed", error_code: "quota_exhausted" }),
    );

    expect(parsed.error_code).toBe("quota_exhausted");
  });

  it("accepts an embedding model it has never heard of", () => {
    const parsed = documentIndexSchema.parse(
      documentIndexPayload({ embedding_model: "acme/future-model" }),
    );

    expect(parsed.embedding_model).toBe("acme/future-model");
  });

  it("parses the metrics payload", () => {
    expect(() => indexMetricsSchema.parse(indexMetricsPayload())).not.toThrow();
  });

  it("tolerates an unreachable vector database reporting no count", () => {
    // Null and zero are different facts: "the collection holds nothing" and "the
    // database cannot be reached" need different responses.
    const parsed = indexMetricsSchema.parse(
      indexMetricsPayload({ stored_vectors: null, vector_store_available: false }),
    );

    expect(parsed.stored_vectors).toBeNull();
  });

  it("omits blank filters from the list query", () => {
    const params = buildIndexListParams({
      ...DEFAULT_INDEX_LIST_QUERY,
      status: "failed",
    });

    expect(params).toContain("status=failed");
    expect(params).not.toContain("case_id");
  });

  it("sends the embedding-model filter when one is chosen", () => {
    // The filter that answers "which documents still need re-indexing after a
    // model change?".
    const params = buildIndexListParams({
      ...DEFAULT_INDEX_LIST_QUERY,
      embeddingModel: "BAAI/bge-m3",
    });

    expect(params).toContain("embedding_model=BAAI");
  });
});

describe("labels", () => {
  it("labels the known failure causes", () => {
    expect(indexFailureLabel("vector_store_unavailable")).toBe("Search index unavailable");
  });

  it("falls back to a readable form of an unknown one", () => {
    expect(indexFailureLabel("quota_exhausted")).toBe("Quota exhausted");
  });

  it("labels a failure with no code at all", () => {
    expect(indexFailureLabel(null)).toBe("Failed");
  });

  it("names the platform's languages", () => {
    expect(indexLanguageLabel("ar")).toBe("Arabic");
    expect(indexLanguageLabel("fr")).toBe("French");
    expect(indexLanguageLabel("und")).toBe("Undetermined");
  });

  it("passes an unknown language code through rather than hiding it", () => {
    expect(indexLanguageLabel("es")).toBe("es");
  });
});

describe("status badge", () => {
  it("names every status as text, never by colour alone", () => {
    for (const status of INDEX_STATUSES) {
      const { unmount } = render(<IndexStatusBadge status={status} />);
      expect(screen.getByText(/queued|indexing|searchable|failed/i)).toBeInTheDocument();
      unmount();
    }
  });
});

describe("document index panel", () => {
  it("says indexing does not apply to a Word file", () => {
    // Indexing consumes extracted text, so it applies exactly where extraction
    // does.
    signInAs("administrator");
    mockFetch({ "/index": { body: documentIndexPayload() } });

    renderWithQuery(
      <DocumentIndexPanel document={documentFor({ file_extension: "docx" })} />,
    );

    expect(screen.getByText(/text has been extracted/i)).toBeInTheDocument();
  });

  it("shows the run's status and what it produced", async () => {
    signInAs("administrator");
    mockFetch({ "/index": { body: documentIndexPayload() } });

    renderWithQuery(<DocumentIndexPanel document={documentFor()} />);

    expect(await screen.findByText("Searchable")).toBeInTheDocument();
    expect(await screen.findByText("14")).toBeInTheDocument();
    expect(screen.getByText("BAAI/bge-m3")).toBeInTheDocument();
    expect(screen.getByText("French")).toBeInTheDocument();
  });

  it("shows no passage of the document", async () => {
    // The scope boundary, asserted on what the user actually sees: reading the
    // index back is Semantic Search's feature.
    signInAs("administrator");
    mockFetch({ "/index": { body: documentIndexPayload() } });

    const { container } = renderWithQuery(<DocumentIndexPanel document={documentFor()} />);
    await screen.findByText("Searchable");

    expect(container.textContent).not.toMatch(/bailleur|passage text/i);
  });

  it("treats a missing record as 'not indexed yet', not an error", async () => {
    // A document whose text has not been extracted, or one uploaded before this
    // feature existed.
    signInAs("administrator");
    mockFetch({
      "/index": {
        status: 404,
        body: errorEnvelope("document_index_not_found", "No record."),
      },
    });

    renderWithQuery(<DocumentIndexPanel document={documentFor()} />);

    expect(await screen.findByText(/has not been indexed for search yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("explains a failure and says the document and its text are unaffected", async () => {
    signInAs("administrator");
    mockFetch({
      "/index": {
        body: documentIndexPayload({
          status: "failed",
          error_code: "vector_store_unavailable",
          error_message: "The search index is unavailable.",
        }),
      },
    });

    renderWithQuery(<DocumentIndexPanel document={documentFor()} />);

    expect(await screen.findByText("Search index unavailable")).toBeInTheDocument();
    expect(screen.getByText(/search index is unavailable/i)).toBeInTheDocument();
    expect(
      screen.getByText(/document and its extracted text are unaffected/i),
    ).toBeInTheDocument();
  });

  it("tells the reader a queued run will update itself", async () => {
    signInAs("administrator");
    mockFetch({ "/index": { body: documentIndexPayload({ status: "pending" }) } });

    renderWithQuery(<DocumentIndexPanel document={documentFor()} />);

    expect(await screen.findByText(/queued for indexing/i)).toBeInTheDocument();
  });

  it("keeps polling while the run is in flight and stops when it settles", async () => {
    vi.useFakeTimers();
    try {
      signInAs("administrator");
      const { requests } = mockFetch({
        "/index": [
          { body: documentIndexPayload({ status: "indexing" }) },
          { body: documentIndexPayload({ status: "indexed" }) },
        ],
      });

      renderWithQuery(<DocumentIndexPanel document={documentFor()} />);

      await vi.waitFor(() => expect(requests.length).toBe(1));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6_000);
      });
      await vi.waitFor(() => expect(requests.length).toBe(2));

      // Settled: the server's own `isActive` said so, and the poll stops.
      const settled = requests.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
      expect(requests.length).toBe(settled);
    } finally {
      vi.useRealTimers();
    }
  });

  it("offers a rebuild to a role that holds the permission", async () => {
    signInAs("administrator");
    mockFetch({ "/index": { body: documentIndexPayload() } });

    renderWithQuery(<DocumentIndexPanel document={documentFor()} />);

    expect(
      await screen.findByRole("button", { name: /rebuild index/i }),
    ).toBeInTheDocument();
  });

  it("hides the rebuild from a court representative", async () => {
    // Every UI gate names a permission, never a role — and no action the API
    // would refuse is offered.
    signInAs("court");
    mockFetch({ "/index": { body: documentIndexPayload() } });

    renderWithQuery(<DocumentIndexPanel document={documentFor()} />);

    await screen.findByText("Searchable");
    expect(screen.queryByRole("button", { name: /rebuild index/i })).not.toBeInTheDocument();
  });

  it("disables the rebuild while a run is already in flight", async () => {
    // A run already queued or indexing answers 409; a button that produces an
    // error the user could not have predicted is worse than no button.
    signInAs("administrator");
    mockFetch({ "/index": { body: documentIndexPayload({ status: "indexing" }) } });

    renderWithQuery(<DocumentIndexPanel document={documentFor()} />);

    const button = await screen.findByRole("button", { name: /rebuild index/i });
    expect(button).toBeDisabled();
  });

  it("sends the rebuild as a POST to the document's own route", async () => {
    signInAs("administrator");
    const { requests } = mockFetch({
      "/index/reindex": { body: documentIndexPayload({ status: "pending" }) },
      "/index": { body: documentIndexPayload() },
    });

    renderWithQuery(<DocumentIndexPanel document={documentFor()} />);
    await userEvent.click(await screen.findByRole("button", { name: /rebuild index/i }));

    await waitFor(() => {
      const rebuild = requests.find((request) => request.url.includes("/reindex"));
      expect(rebuild?.method).toBe("POST");
    });
  });

  it("labels the action 'index for search' when there is no record yet", async () => {
    signInAs("administrator");
    mockFetch({
      "/index": {
        status: 404,
        body: errorEnvelope("document_index_not_found", "No record."),
      },
    });

    renderWithQuery(<DocumentIndexPanel document={documentFor()} />);

    expect(
      await screen.findByRole("button", { name: /index for search/i }),
    ).toBeInTheDocument();
  });
});

describe("indexing metrics panel", () => {
  it("shows the four figures the spec names", async () => {
    signInAs("administrator");
    mockFetch({ "/indexing/metrics": { body: indexMetricsPayload() } });

    renderWithQuery(<IndexMetricsPanel />);

    // Each stat is read through its own label rather than by its bare value:
    // several of these figures collide (two failures, two in flight), and a
    // test that matched on "2" alone would pass for the wrong reason.
    for (const [label, value] of [
      ["Documents indexed", "8"],
      ["Passages indexed", "96"],
      ["Average time", "14.2s"],
      ["Failures", "2"],
    ] as const) {
      const heading = await screen.findByText(label);
      expect(heading.nextElementSibling).toHaveTextContent(value);
    }
  });

  it("warns when the embedding model cannot load", async () => {
    // A platform indexing nothing because no model is installed and one with
    // nothing to index show the same zeros.
    signInAs("administrator");
    mockFetch({
      "/indexing/metrics": {
        body: indexMetricsPayload({ embedding_available: false }),
      },
    });

    renderWithQuery(<IndexMetricsPanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/cannot be loaded/i);
  });

  it("warns when the vector database is unreachable", async () => {
    signInAs("administrator");
    mockFetch({
      "/indexing/metrics": {
        body: indexMetricsPayload({
          vector_store_available: false,
          stored_vectors: null,
        }),
      },
    });

    renderWithQuery(<IndexMetricsPanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/not reachable/i);
  });

  it("says plainly when indexing is switched off", async () => {
    signInAs("administrator");
    mockFetch({
      "/indexing/metrics": { body: indexMetricsPayload({ enabled: false }) },
    });

    renderWithQuery(<IndexMetricsPanel />);

    expect(await screen.findByText(/indexing is disabled/i)).toBeInTheDocument();
  });

  it("breaks failures down by cause", async () => {
    signInAs("administrator");
    mockFetch({ "/indexing/metrics": { body: indexMetricsPayload() } });

    renderWithQuery(<IndexMetricsPanel />);

    expect(await screen.findByText("Embedding service failed")).toBeInTheDocument();
    expect(screen.getByText("Search index unavailable")).toBeInTheDocument();
  });

  it("renders nothing when the caller is refused", async () => {
    // The panel has no useful "you cannot see this" state to show.
    signInAs("lawyer");
    mockFetch({
      "/indexing/metrics": {
        status: 403,
        body: errorEnvelope("forbidden", "Access denied."),
      },
    });

    const { container } = renderWithQuery(<IndexMetricsPanel />);

    await waitFor(() => expect(container.textContent).not.toMatch(/documents indexed/i));
  });
});
