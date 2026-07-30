/**
 * Tests for Document Management.
 *
 * Cover the whole client surface the spec asks for: uploading, previewing,
 * downloading, replacing, deleting, searching, filtering, sorting, paginating,
 * and what an unauthorized role is allowed to see.
 *
 * These verify what the user is *shown* and what the client *sends*. The API is
 * the real boundary — its 401/403, per-case assignment check, file validation,
 * and versioning behaviour are covered by `tests/integration/test_documents.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RouteGuard } from "@/components/auth/route-guard";
import { DeleteDocumentDialog } from "@/components/documents/delete-document-dialog";
import { DocumentList } from "@/components/documents/document-list";
import { DocumentPreviewDialog } from "@/components/documents/document-preview-dialog";
import { DocumentVersionHistory } from "@/components/documents/document-version-history";
import { ReplaceDocumentDialog } from "@/components/documents/replace-document-dialog";
import { UploadDocumentDialog } from "@/components/documents/upload-document-dialog";
import { UploadProgress } from "@/components/documents/upload-progress";
import { accessRuleForPath } from "@/lib/authorization/routes";
import { buildDocumentListParams } from "@/lib/api/documents";
import { filenameFromDisposition } from "@/lib/api/upload";
import {
  fileExtensionOf,
  uploadDocumentFormSchema,
  legalDocumentSchema,
} from "@/lib/validation/document";
import { ROUTES } from "@/lib/routes";
import { useSessionStore } from "@/stores/session-store";
import { PERMISSION } from "@/types/authorization";
import { DEFAULT_DOCUMENT_LIST_QUERY } from "@/types/document-management";
import type { LegalDocument } from "@/types/document";
import type { UserRole } from "@/types/user";
import {
  documentPagePayload,
  documentVersionPayload,
  errorEnvelope,
  legalCasePayload,
  legalDocumentPayload,
  managedUserPayload,
  mockFetch,
  mockUpload,
  sessionUserWithRole,
  userPagePayload,
} from "./helpers";

let pathname = ROUTES.documents;

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));

// `sonner` renders through a portal and a global store; the tests assert on the
// requests and the dialog state, not on toast text.
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  }),
}));

function signInAs(role: UserRole) {
  act(() => {
    useSessionStore.setState({ user: sessionUserWithRole(role), status: "authenticated" });
  });
}

/** Render inside a fresh QueryClient so no cache leaks between tests. */
function renderWithQuery(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  return {
    queryClient,
    ...render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>),
  };
}

/**
 * The endpoints the document screens touch: documents, the cases the upload
 * picker reads, and the user directory the uploader filter reads.
 */
function mockDocumentApi(
  documents: Parameters<typeof mockFetch>[0][string] = { body: documentPagePayload() },
  extra: Parameters<typeof mockFetch>[0] = {},
) {
  return mockFetch({
    "/documents": documents,
    "/cases": { body: { items: [legalCasePayload()], total_records: 1, page: 1, page_size: 100, total_pages: 1 } },
    "/users": { body: userPagePayload([managedUserPayload()]) },
    ...extra,
  });
}

function pdfFile(name = "contrat.pdf"): File {
  return new File(["%PDF-1.4 fake"], name, { type: "application/pdf" });
}

/** The last document-list request the client issued. */
function lastListRequest(requests: Array<{ url: string; method: string }>) {
  return [...requests].reverse().find((request) => request.url.includes("/documents?"));
}

// --------------------------------------------------------------------------- //
// Query building
// --------------------------------------------------------------------------- //

describe("buildDocumentListParams", () => {
  it("always sends the page, size, and sort", () => {
    const params = new URLSearchParams(buildDocumentListParams(DEFAULT_DOCUMENT_LIST_QUERY));

    expect(params.get("page")).toBe("1");
    expect(params.get("page_size")).toBe("20");
    expect(params.get("sort_by")).toBe("created_at");
    expect(params.get("sort_order")).toBe("desc");
  });

  it("omits empty filters rather than sending blanks", () => {
    // So the request URL reflects what is actually being asked — which is also
    // what makes the query a stable cache key.
    const params = new URLSearchParams(buildDocumentListParams(DEFAULT_DOCUMENT_LIST_QUERY));

    for (const key of ["search", "case_id", "category", "uploaded_by", "file_extension"]) {
      expect(params.has(key)).toBe(false);
    }
  });

  it("maps every filter onto its wire name", () => {
    const params = new URLSearchParams(
      buildDocumentListParams({
        ...DEFAULT_DOCUMENT_LIST_QUERY,
        search: "  bail  ",
        caseId: "case-1",
        category: "evidence",
        uploadedBy: "user-1",
        fileExtension: "pdf",
        uploadedFrom: "2026-07-01",
        uploadedTo: "2026-07-31",
      }),
    );

    expect(params.get("search")).toBe("bail");
    expect(params.get("case_id")).toBe("case-1");
    expect(params.get("category")).toBe("evidence");
    expect(params.get("uploaded_by")).toBe("user-1");
    expect(params.get("file_extension")).toBe("pdf");
    expect(params.get("uploaded_from")).toBe("2026-07-01");
    expect(params.get("uploaded_to")).toBe("2026-07-31");
  });
});

// --------------------------------------------------------------------------- //
// Validation
// --------------------------------------------------------------------------- //

describe("upload validation", () => {
  function files(...entries: File[]): FileList {
    return entries as unknown as FileList;
  }

  it("requires a file", () => {
    const result = uploadDocumentFormSchema.safeParse({
      caseId: "case-1",
      category: "other",
      description: "",
      file: files(),
    });

    expect(result.success).toBe(false);
  });

  it("rejects an empty file", () => {
    const empty = new File([], "empty.pdf", { type: "application/pdf" });

    const result = uploadDocumentFormSchema.safeParse({
      caseId: "case-1",
      category: "other",
      description: "",
      file: files(empty),
    });

    expect(result.success).toBe(false);
  });

  it("rejects an unsupported file type", () => {
    const executable = new File(["MZ"], "payload.exe", { type: "application/octet-stream" });

    const result = uploadDocumentFormSchema.safeParse({
      caseId: "case-1",
      category: "other",
      description: "",
      file: files(executable),
    });

    expect(result.success).toBe(false);
    expect(result.error?.issues[0]?.message).toContain("pdf");
  });

  it("accepts a supported file", () => {
    const result = uploadDocumentFormSchema.safeParse({
      caseId: "case-1",
      category: "contract",
      description: "  Bail  ",
      file: files(pdfFile()),
    });

    expect(result.success).toBe(true);
    expect(result.data?.description).toBe("Bail");
  });

  it("requires a case", () => {
    const result = uploadDocumentFormSchema.safeParse({
      caseId: "",
      category: "other",
      description: "",
      file: files(pdfFile()),
    });

    expect(result.success).toBe(false);
  });

  it.each([
    ["Report.PDF", "pdf"],
    ["scan.tar.gz", "gz"],
    ["README", ""],
    ["ends-with-a-dot.", ""],
  ])("reads the extension of %s as %s", (name, expected) => {
    expect(fileExtensionOf(name)).toBe(expected);
  });
});

describe("legalDocumentSchema", () => {
  it("parses a document payload", () => {
    const parsed = legalDocumentSchema.parse(legalDocumentPayload());

    expect(parsed.original_filename).toBe("contrat-de-bail.pdf");
    expect(parsed.versions).toHaveLength(1);
  });

  it("rejects an unknown category rather than silently dropping it", () => {
    // Unlike a case's `allowed_transitions`, the category *is* the value the row
    // is filed under — rendering it as blank would misrepresent the record.
    expect(() =>
      legalDocumentSchema.parse(legalDocumentPayload({ category: "top_secret" })),
    ).toThrow();
  });
});

describe("filenameFromDisposition", () => {
  it("prefers the RFC 5987 form so a non-ASCII name survives", () => {
    const header = `attachment; filename="?????.pdf"; filename*=UTF-8''%D8%B9%D9%82%D8%AF.pdf`;

    expect(filenameFromDisposition(header, "fallback.pdf")).toBe("عقد.pdf");
  });

  it("falls back to the plain parameter", () => {
    expect(filenameFromDisposition('attachment; filename="contract.pdf"', "x.pdf")).toBe(
      "contract.pdf",
    );
  });

  it("falls back to the supplied name when there is no header", () => {
    expect(filenameFromDisposition(null, "x.pdf")).toBe("x.pdf");
  });
});

// --------------------------------------------------------------------------- //
// Authorization
// --------------------------------------------------------------------------- //

describe("document route protection", () => {
  it("declares the documents:view requirement once, in the navigation config", () => {
    expect(accessRuleForPath(ROUTES.documents)).toEqual({
      permission: PERMISSION.documentsView,
    });
  });

  it.each<UserRole>(["administrator", "lawyer", "court"])(
    "admits %s, who holds documents:view",
    (role) => {
      pathname = ROUTES.documents;
      signInAs(role);

      render(
        <RouteGuard>
          <p>Documents</p>
        </RouteGuard>,
      );

      expect(screen.getByText("Documents")).toBeInTheDocument();
    },
  );
});

describe("DocumentList row actions", () => {
  it("offers Replace and Delete to an administrator", async () => {
    pathname = ROUTES.documents;
    mockDocumentApi();
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(<DocumentList />);
    await screen.findByText("contrat-de-bail.pdf");

    await user.click(screen.getByRole("button", { name: /Actions for contrat-de-bail.pdf/ }));

    expect(await screen.findByRole("menuitem", { name: /Replace/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Delete/ })).toBeInTheDocument();
  });

  it.each<UserRole>(["lawyer", "court"])(
    "hides Replace and Delete from %s, who holds neither permission",
    async (role) => {
      pathname = ROUTES.documents;
      mockDocumentApi();
      signInAs(role);
      const user = userEvent.setup();

      renderWithQuery(<DocumentList />);
      await screen.findByText("contrat-de-bail.pdf");

      await user.click(screen.getByRole("button", { name: /Actions for contrat-de-bail.pdf/ }));
      await screen.findByRole("menuitem", { name: /Download/ });

      expect(screen.queryByRole("menuitem", { name: /Replace/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("menuitem", { name: /^Delete/ })).not.toBeInTheDocument();
    },
  );

  it("hides Upload from a caller without documents:upload", async () => {
    pathname = ROUTES.documents;
    mockDocumentApi();
    // No role lacks `documents:upload` today, so the check is made directly on
    // the permission set rather than by inventing a role that cannot exist.
    act(() => {
      useSessionStore.setState({
        user: { ...sessionUserWithRole("lawyer"), permissions: ["documents:view"] },
        status: "authenticated",
      });
    });

    renderWithQuery(<DocumentList />);
    await screen.findByText("contrat-de-bail.pdf");

    expect(
      screen.queryByRole("button", { name: /Upload document/ }),
    ).not.toBeInTheDocument();
  });

  it("does not offer Preview for a file type the server cannot render", async () => {
    pathname = ROUTES.documents;
    mockDocumentApi({
      body: documentPagePayload([
        legalDocumentPayload({
          original_filename: "memoire.docx",
          file_extension: "docx",
          is_previewable: false,
        }),
      ]),
    });
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(<DocumentList />);
    await screen.findByText("memoire.docx");

    await user.click(screen.getByRole("button", { name: /Actions for memoire.docx/ }));
    await screen.findByRole("menuitem", { name: /Download/ });

    // Offering it would be offering an action the API answers with 415.
    expect(screen.queryByRole("menuitem", { name: /Preview/ })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Listing
// --------------------------------------------------------------------------- //

describe("DocumentList", () => {
  it("renders the columns the spec lists", async () => {
    pathname = ROUTES.documents;
    mockDocumentApi();
    signInAs("administrator");

    renderWithQuery(<DocumentList />);
    await screen.findByText("contrat-de-bail.pdf");

    const table = screen.getByRole("table");
    for (const column of ["File name", "Category", "Size", "Version", "Uploaded by", "Upload date"]) {
      expect(within(table).getByRole("columnheader", { name: new RegExp(column) })).toBeInTheDocument();
    }
    expect(within(table).getByText("Contract")).toBeInTheDocument();
    expect(within(table).getByText("2.0 KB")).toBeInTheDocument();
    expect(within(table).getByText("v1")).toBeInTheDocument();
  });

  it("shows a skeleton while the first page loads", () => {
    pathname = ROUTES.documents;
    mockDocumentApi();
    signInAs("administrator");

    renderWithQuery(<DocumentList />);

    expect(screen.getByLabelText("Loading documents")).toBeInTheDocument();
  });

  it("shows a distinct empty state when nothing has been uploaded", async () => {
    pathname = ROUTES.documents;
    mockDocumentApi({ body: documentPagePayload([]) });
    signInAs("administrator");

    renderWithQuery(<DocumentList />);

    expect(await screen.findByText("No documents yet")).toBeInTheDocument();
  });

  it("shows a different empty state when a search returns nothing", async () => {
    pathname = ROUTES.documents;
    mockDocumentApi({ body: documentPagePayload([]) });
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(<DocumentList />);
    await screen.findByText("No documents yet");

    await user.type(screen.getByLabelText("Search"), "nothing");

    // A fruitless search needs a way back to the full list, not an invitation to
    // upload — two genuinely different situations.
    expect(await screen.findByText("No documents match your filters")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("shows an error state with a retry", async () => {
    pathname = ROUTES.documents;
    mockDocumentApi({ status: 503, body: errorEnvelope("document_storage_unavailable") });
    signInAs("administrator");

    renderWithQuery(<DocumentList />);

    expect(await screen.findByText("Could not load documents")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Retry|Try again/i })).toBeInTheDocument();
  });

  it("reports the pagination totals", async () => {
    pathname = ROUTES.documents;
    mockDocumentApi({
      body: documentPagePayload([legalDocumentPayload()], {
        total_records: 45,
        page: 1,
        page_size: 20,
        total_pages: 3,
      }),
    });
    signInAs("administrator");

    renderWithQuery(<DocumentList />);

    expect(await screen.findByText("Showing 1–20 of 45 documents")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
  });

  it("searches, debounced, and resets to the first page", async () => {
    pathname = ROUTES.documents;
    const { requests } = mockDocumentApi();
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(<DocumentList />);
    await screen.findByText("contrat-de-bail.pdf");

    await user.type(screen.getByLabelText("Search"), "bail");

    await waitFor(() => {
      expect(lastListRequest(requests)?.url).toContain("search=bail");
    });
    expect(lastListRequest(requests)?.url).toContain("page=1");
  });

  it("filters by file type", async () => {
    pathname = ROUTES.documents;
    const { requests } = mockDocumentApi();
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(<DocumentList />);
    await screen.findByText("contrat-de-bail.pdf");

    await user.click(screen.getByLabelText("File type"));
    await user.click(await screen.findByRole("option", { name: "PDF" }));

    await waitFor(() => {
      expect(lastListRequest(requests)?.url).toContain("file_extension=pdf");
    });
  });

  it("filters by category", async () => {
    pathname = ROUTES.documents;
    const { requests } = mockDocumentApi();
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(<DocumentList />);
    await screen.findByText("contrat-de-bail.pdf");

    await user.click(screen.getByLabelText("Category"));
    await user.click(await screen.findByRole("option", { name: "Evidence" }));

    await waitFor(() => {
      expect(lastListRequest(requests)?.url).toContain("category=evidence");
    });
  });

  it("sorts a column in both directions", async () => {
    pathname = ROUTES.documents;
    const { requests } = mockDocumentApi();
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(<DocumentList />);
    await screen.findByText("contrat-de-bail.pdf");

    await user.click(screen.getByRole("button", { name: /Sort by File name/ }));
    await waitFor(() => {
      expect(lastListRequest(requests)?.url).toContain("sort_by=original_filename");
    });
    expect(lastListRequest(requests)?.url).toContain("sort_order=asc");

    await user.click(screen.getByRole("button", { name: /Sorted ascending/ }));
    await waitFor(() => {
      expect(lastListRequest(requests)?.url).toContain("sort_order=desc");
    });
  });

  it("pins the case scope when embedded in a case workspace", async () => {
    pathname = ROUTES.documents;
    const { requests } = mockDocumentApi();
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(<DocumentList caseId="case-42" />);
    await screen.findByText("contrat-de-bail.pdf");

    expect(lastListRequest(requests)?.url).toContain("case_id=case-42");

    // "Clear filters" must not silently widen the view to every case.
    await user.type(screen.getByLabelText("Search"), "bail");
    await waitFor(() => expect(lastListRequest(requests)?.url).toContain("search=bail"));
    await user.click(screen.getByRole("button", { name: /Clear/ }));

    await waitFor(() => {
      expect(lastListRequest(requests)?.url).not.toContain("search=");
    });
    expect(lastListRequest(requests)?.url).toContain("case_id=case-42");
  });

  it("hides the case column when scoped to one case", async () => {
    pathname = ROUTES.documents;
    mockDocumentApi();
    signInAs("administrator");

    renderWithQuery(<DocumentList caseId="case-42" />);
    await screen.findByText("contrat-de-bail.pdf");

    // Repeating one case number down every row is noise.
    expect(screen.queryByRole("columnheader", { name: "Case" })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Upload
// --------------------------------------------------------------------------- //

describe("UploadDocumentDialog", () => {
  it("sends the file, case, category, and description as multipart", async () => {
    mockDocumentApi();
    const { uploads } = mockUpload({ status: 201, body: legalDocumentPayload() });
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(
      <UploadDocumentDialog open onOpenChange={vi.fn()} caseId={legalCasePayload().id} />,
    );

    await user.upload(screen.getByLabelText("File"), pdfFile());
    await user.click(screen.getByLabelText("Category"));
    await user.click(await screen.findByRole("option", { name: "Contract" }));
    await user.type(screen.getByLabelText("Description (optional)"), "Bail commercial");
    await user.click(screen.getByRole("button", { name: /^Upload$/ }));

    await waitFor(() => expect(uploads).toHaveLength(1));
    expect(uploads[0]?.url).toContain("/documents/upload");
    expect(uploads[0]?.fields).toMatchObject({
      case_id: legalCasePayload().id,
      category: "contract",
      description: "Bail commercial",
      file: { name: "contrat.pdf" },
    });
  });

  it("sends the credential and the cookie", async () => {
    mockDocumentApi();
    const { uploads } = mockUpload({ status: 201, body: legalDocumentPayload() });
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(
      <UploadDocumentDialog open onOpenChange={vi.fn()} caseId={legalCasePayload().id} />,
    );

    await user.upload(screen.getByLabelText("File"), pdfFile());
    await user.click(screen.getByRole("button", { name: /^Upload$/ }));

    await waitFor(() => expect(uploads).toHaveLength(1));
    // The refresh cookie must travel with a multipart request exactly as it does
    // with a JSON one.
    expect(uploads[0]?.withCredentials).toBe(true);
    // Content-Type is deliberately left to the browser, which supplies the
    // multipart boundary; setting it by hand omits it.
    expect(uploads[0]?.headers["Content-Type"]).toBeUndefined();
  });

  it("shows real transmitted-byte progress while the upload is in flight", async () => {
    mockDocumentApi();
    const { release } = mockUpload({
      status: 201,
      body: legalDocumentPayload(),
      progress: [[42, 100]],
      hold: true,
    });
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(
      <UploadDocumentDialog open onOpenChange={vi.fn()} caseId={legalCasePayload().id} />,
    );

    await user.upload(screen.getByLabelText("File"), pdfFile());
    await user.click(screen.getByRole("button", { name: /^Upload$/ }));

    const bar = await screen.findByRole("progressbar", { name: "Upload progress" });
    // Driven by the XHR upload events, not a fabricated animation — which is why
    // `fetch` is not used for this request.
    await waitFor(() => expect(bar).toHaveAttribute("aria-valuenow", "42"));

    act(() => release());
    await waitFor(() =>
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument(),
    );
  });

  it("refuses an unsupported type before it reaches the network", async () => {
    mockDocumentApi();
    const { uploads } = mockUpload();
    signInAs("administrator");
    // The `accept` attribute already stops the picker from offering this file;
    // bypassing it is what exercises the schema rule *behind* that attribute,
    // which is the one that still applies to a drag-and-drop or a stale form.
    const user = userEvent.setup({ applyAccept: false });

    renderWithQuery(
      <UploadDocumentDialog open onOpenChange={vi.fn()} caseId={legalCasePayload().id} />,
    );

    await user.upload(
      screen.getByLabelText("File"),
      new File(["MZ"], "payload.exe", { type: "application/octet-stream" }),
    );
    await user.click(screen.getByRole("button", { name: /^Upload$/ }));

    expect(await screen.findByText(/Supported file types/)).toBeInTheDocument();
    expect(uploads).toHaveLength(0);
  });

  it("declares the accepted types on the file input", () => {
    mockDocumentApi();
    mockUpload();
    signInAs("administrator");

    renderWithQuery(
      <UploadDocumentDialog open onOpenChange={vi.fn()} caseId={legalCasePayload().id} />,
    );

    // So the picker filters before the user chooses, mirroring the server policy.
    expect(screen.getByLabelText("File")).toHaveAttribute(
      "accept",
      ".pdf,.docx,.doc,.txt,.jpg,.jpeg,.png",
    );
  });

  it("maps a server file rejection back onto the file input", async () => {
    // The client cannot inspect the bytes; only the server can tell a truncated
    // PDF from a real one. Its complaint has to land on the field it is about.
    mockDocumentApi();
    mockUpload({
      status: 422,
      body: errorEnvelope("invalid_document_file", "The file could not be accepted.", [
        { field: "file", message: "The file does not appear to be a valid PDF file." },
      ]),
    });
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(
      <UploadDocumentDialog open onOpenChange={vi.fn()} caseId={legalCasePayload().id} />,
    );

    await user.upload(screen.getByLabelText("File"), pdfFile());
    await user.click(screen.getByRole("button", { name: /^Upload$/ }));

    expect(
      await screen.findByText("The file does not appear to be a valid PDF file."),
    ).toBeInTheDocument();
  });

  it("handles a storage outage gracefully", async () => {
    mockDocumentApi();
    mockUpload({
      status: 503,
      body: errorEnvelope("document_storage_unavailable", "Storage is unavailable."),
    });
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(
      <UploadDocumentDialog open onOpenChange={vi.fn()} caseId={legalCasePayload().id} />,
    );

    await user.upload(screen.getByLabelText("File"), pdfFile());
    await user.click(screen.getByRole("button", { name: /^Upload$/ }));

    expect(
      await screen.findByText(/Document storage is temporarily unavailable/),
    ).toBeInTheDocument();
  });

  it("hides the case picker when the case is already known", async () => {
    mockDocumentApi();
    mockUpload();
    signInAs("administrator");

    renderWithQuery(
      <UploadDocumentDialog open onOpenChange={vi.fn()} caseId={legalCasePayload().id} />,
    );

    expect(screen.queryByLabelText("Case")).not.toBeInTheDocument();
  });

  it("offers a case picker on the global documents page", async () => {
    mockDocumentApi();
    mockUpload();
    signInAs("administrator");

    renderWithQuery(<UploadDocumentDialog open onOpenChange={vi.fn()} />);

    expect(await screen.findByLabelText("Case")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Replace and versioning
// --------------------------------------------------------------------------- //

describe("ReplaceDocumentDialog", () => {
  function existingDocument(): LegalDocument {
    return {
      id: legalDocumentPayload().id,
      caseId: legalCasePayload().id,
      case: null,
      originalFilename: "contrat-de-bail.pdf",
      storedFilename: "abc.pdf",
      fileExtension: "pdf",
      mimeType: "application/pdf",
      fileSize: 2048,
      fileSizeLabel: "2.0 KB",
      storageBucket: "legal-documents",
      storageKey: "cases/x/documents/y/v1/abc.pdf",
      category: "contract",
      description: null,
      version: 1,
      versionCount: 1,
      uploadedBy: null,
      uploader: null,
      uploadedAt: "2026-07-20T09:00:00Z",
      createdAt: "2026-07-20T09:00:00Z",
      updatedAt: "2026-07-20T09:00:00Z",
      deletedAt: null,
      isDeleted: false,
      isPreviewable: true,
      versions: [],
    };
  }

  it("states that the previous version is kept", () => {
    mockDocumentApi();
    mockUpload();
    signInAs("administrator");

    renderWithQuery(
      <ReplaceDocumentDialog document={existingDocument()} open onOpenChange={vi.fn()} />,
    );

    // "Replace" usually implies the old file is gone; here it is not.
    expect(screen.getByText(/Version 1 is kept and stays downloadable/)).toBeInTheDocument();
  });

  it("posts the new file to the replace endpoint", async () => {
    mockDocumentApi();
    const { uploads } = mockUpload({
      status: 200,
      body: legalDocumentPayload({ version: 2, original_filename: "revision.pdf" }),
    });
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(
      <ReplaceDocumentDialog document={existingDocument()} open onOpenChange={vi.fn()} />,
    );

    await user.upload(screen.getByLabelText("New file"), pdfFile("revision.pdf"));
    await user.click(screen.getByRole("button", { name: /Upload new version/ }));

    await waitFor(() => expect(uploads).toHaveLength(1));
    expect(uploads[0]?.url).toContain(`/documents/${legalDocumentPayload().id}/replace`);
    expect(uploads[0]?.fields).toMatchObject({ file: { name: "revision.pdf" } });
  });
});

describe("DocumentVersionHistory", () => {
  function versionedDocument(): LegalDocument {
    const parsed = legalDocumentSchema.parse(
      legalDocumentPayload({
        version: 2,
        versions: [
          documentVersionPayload({ version: 1, original_filename: "v1.pdf" }),
          documentVersionPayload({
            version: 2,
            original_filename: "v2.pdf",
            created_at: "2026-07-25T10:00:00Z",
          }),
        ],
      }),
    );

    return {
      id: parsed.id,
      caseId: parsed.case_id,
      case: null,
      originalFilename: parsed.original_filename,
      storedFilename: parsed.stored_filename,
      fileExtension: parsed.file_extension,
      mimeType: parsed.mime_type,
      fileSize: parsed.file_size,
      fileSizeLabel: parsed.file_size_label,
      storageBucket: parsed.storage_bucket,
      storageKey: parsed.storage_key,
      category: parsed.category,
      description: parsed.description,
      version: parsed.version,
      versionCount: parsed.version_count,
      uploadedBy: parsed.uploaded_by,
      uploader: null,
      uploadedAt: parsed.uploaded_at,
      createdAt: parsed.created_at,
      updatedAt: parsed.updated_at,
      deletedAt: parsed.deleted_at,
      isDeleted: parsed.is_deleted,
      isPreviewable: parsed.is_previewable,
      versions: parsed.versions.map((version) => ({
        version: version.version,
        originalFilename: version.original_filename,
        fileExtension: version.file_extension,
        mimeType: version.mime_type,
        fileSize: version.file_size,
        fileSizeLabel: version.file_size_label,
        uploadedBy: version.uploaded_by,
        uploader: null,
        createdAt: version.created_at,
      })),
    };
  }

  it("lists every version, newest first, and marks the current one", () => {
    render(
      <DocumentVersionHistory document={versionedDocument()} onDownloadVersion={vi.fn()} />,
    );

    const entries = screen.getAllByRole("listitem");
    expect(entries).toHaveLength(2);
    expect(within(entries[0]!).getByText("Version 2")).toBeInTheDocument();
    expect(within(entries[0]!).getByText("Current")).toBeInTheDocument();
    expect(within(entries[1]!).getByText("Version 1")).toBeInTheDocument();
  });

  it("lets a previous version be downloaded", async () => {
    const onDownloadVersion = vi.fn();
    const user = userEvent.setup();

    render(
      <DocumentVersionHistory
        document={versionedDocument()}
        onDownloadVersion={onDownloadVersion}
      />,
    );

    const entries = screen.getAllByRole("listitem");
    await user.click(within(entries[1]!).getByRole("button", { name: /Download/ }));

    expect(onDownloadVersion).toHaveBeenCalledWith(expect.objectContaining({ version: 1 }));
  });
});

// --------------------------------------------------------------------------- //
// Download and preview
// --------------------------------------------------------------------------- //

describe("download", () => {
  it("fetches the file through an authenticated request and saves it", async () => {
    pathname = ROUTES.documents;
    const click = vi.fn();
    const createObjectURL = vi.fn(() => "blob:document");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL, revokeObjectURL }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(click);

    const { requests } = mockFetch({
      "/documents/44444444-4444-4444-8444-444444444444/download": {
        binary: {
          content: "%PDF-1.4",
          contentType: "application/pdf",
          disposition: 'attachment; filename="contrat-de-bail.pdf"',
        },
      },
      "/documents": { body: documentPagePayload() },
      "/cases": { body: { items: [], total_records: 0, page: 1, page_size: 100, total_pages: 1 } },
      "/users": { body: userPagePayload([]) },
    });
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(<DocumentList />);
    await screen.findByText("contrat-de-bail.pdf");

    await user.click(screen.getByRole("button", { name: /Actions for contrat-de-bail.pdf/ }));
    await user.click(await screen.findByRole("menuitem", { name: /Download/ }));

    await waitFor(() => expect(click).toHaveBeenCalled());
    const download = requests.find((request) => request.url.includes("/download"));
    // A plain <a href> would arrive anonymous: the token is a header, not a cookie.
    expect(download?.credentials).toBe("include");
    // The object URL is revoked immediately; it pins the whole blob otherwise.
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:document");
  });
});

describe("DocumentPreviewDialog", () => {
  function previewableDocument(previewable = true): LegalDocument {
    return {
      id: legalDocumentPayload().id,
      caseId: legalCasePayload().id,
      case: null,
      originalFilename: "contrat-de-bail.pdf",
      storedFilename: "abc.pdf",
      fileExtension: previewable ? "pdf" : "docx",
      mimeType: previewable ? "application/pdf" : "application/msword",
      fileSize: 2048,
      fileSizeLabel: "2.0 KB",
      storageBucket: "legal-documents",
      storageKey: "cases/x/documents/y/v1/abc.pdf",
      category: "contract",
      description: null,
      version: 1,
      versionCount: 1,
      uploadedBy: null,
      uploader: null,
      uploadedAt: "2026-07-20T09:00:00Z",
      createdAt: "2026-07-20T09:00:00Z",
      updatedAt: "2026-07-20T09:00:00Z",
      deletedAt: null,
      isDeleted: false,
      isPreviewable: previewable,
      versions: [],
    };
  }

  it("renders a previewable file inline from a blob URL", async () => {
    vi.stubGlobal(
      "URL",
      Object.assign(URL, {
        createObjectURL: vi.fn(() => "blob:preview"),
        revokeObjectURL: vi.fn(),
      }),
    );
    mockFetch({
      "/preview": { binary: { content: "%PDF-1.4", contentType: "application/pdf" } },
    });
    signInAs("administrator");

    renderWithQuery(
      <DocumentPreviewDialog document={previewableDocument()} open onOpenChange={vi.fn()} />,
    );

    const frame = await screen.findByTitle("Preview of contrat-de-bail.pdf");
    expect(frame).toHaveAttribute("src", "blob:preview");
    // The document is user-supplied: no script, no forms, opaque origin.
    expect(frame).toHaveAttribute("sandbox", "");
  });

  it("offers a download when the server says the type cannot be previewed", async () => {
    vi.stubGlobal(
      "URL",
      Object.assign(URL, {
        createObjectURL: vi.fn(() => "blob:preview"),
        revokeObjectURL: vi.fn(),
      }),
    );
    mockFetch({
      "/preview": {
        status: 415,
        body: errorEnvelope("preview_unavailable", "This file type cannot be previewed."),
      },
    });
    signInAs("administrator");

    renderWithQuery(
      <DocumentPreviewDialog document={previewableDocument(false)} open onOpenChange={vi.fn()} />,
    );

    expect(await screen.findByText("Cannot preview this document")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Download/ })).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Delete
// --------------------------------------------------------------------------- //

describe("DeleteDocumentDialog", () => {
  function target(): LegalDocument {
    return {
      id: legalDocumentPayload().id,
      caseId: legalCasePayload().id,
      case: null,
      originalFilename: "contrat-de-bail.pdf",
      storedFilename: "abc.pdf",
      fileExtension: "pdf",
      mimeType: "application/pdf",
      fileSize: 2048,
      fileSizeLabel: "2.0 KB",
      storageBucket: "legal-documents",
      storageKey: "k",
      category: "contract",
      description: null,
      version: 1,
      versionCount: 1,
      uploadedBy: null,
      uploader: null,
      uploadedAt: "2026-07-20T09:00:00Z",
      createdAt: "2026-07-20T09:00:00Z",
      updatedAt: "2026-07-20T09:00:00Z",
      deletedAt: null,
      isDeleted: false,
      isPreviewable: true,
      versions: [],
    };
  }

  it("states that the document is kept, not destroyed", () => {
    mockDocumentApi();
    signInAs("administrator");

    renderWithQuery(<DeleteDocumentDialog document={target()} open onOpenChange={vi.fn()} />);

    expect(screen.getByText(/kept — not destroyed/)).toBeInTheDocument();
  });

  it("sends a DELETE and closes on success", async () => {
    const onOpenChange = vi.fn();
    const { requests } = mockFetch({
      "/documents": { body: legalDocumentPayload({ deleted_at: "2026-07-30T10:00:00Z", is_deleted: true }) },
    });
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(
      <DeleteDocumentDialog document={target()} open onOpenChange={onOpenChange} />,
    );

    await user.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(requests.at(-1)?.method).toBe("DELETE");
  });

  it("shows the server's message when the delete is refused", async () => {
    mockFetch({
      "/documents": { status: 403, body: errorEnvelope("forbidden", "Forbidden.") },
    });
    signInAs("administrator");
    const user = userEvent.setup();

    renderWithQuery(<DeleteDocumentDialog document={target()} open onOpenChange={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(
      await screen.findByText("You do not have permission to perform this action."),
    ).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Progress indicator
// --------------------------------------------------------------------------- //

describe("UploadProgress", () => {
  it("exposes the percentage to assistive technology", () => {
    render(<UploadProgress percent={42} />);

    const bar = screen.getByRole("progressbar", { name: "Upload progress" });
    expect(bar).toHaveAttribute("aria-valuenow", "42");
    expect(screen.getByText("Uploading… 42%")).toBeInTheDocument();
  });

  it("omits aria-valuenow when the total is unknown", () => {
    // Which is precisely how an indeterminate progress bar is expressed.
    render(<UploadProgress percent={null} />);

    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  });
});
