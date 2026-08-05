/**
 * Tests for Case Management.
 *
 * Cover the whole client surface the spec asks for: creating, editing, and
 * archiving a case, assignment, searching, filtering, sorting, paginating, and
 * what an unauthorized role is allowed to see.
 *
 * These verify what the user is *shown* and what the client *sends*. The API is
 * the real boundary — its 401/403, per-case assignment check, and CRUD behaviour
 * are covered by `tests/integration/test_cases.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RouteGuard } from "@/components/auth/route-guard";
import { ArchiveCaseDialog } from "@/components/cases/archive-case-dialog";
import { AssignCaseDialog } from "@/components/cases/assign-case-dialog";
import { CaseList } from "@/components/cases/case-list";
import { CasePlaceholderSections } from "@/components/cases/case-placeholder-sections";
import { CreateCaseDialog } from "@/components/cases/create-case-dialog";
import { EditCaseDialog, changedCaseFields } from "@/components/cases/edit-case-dialog";
import { accessRuleForPath } from "@/lib/authorization/routes";
import { buildCaseListParams } from "@/lib/api/cases";
import { createCaseFormSchema, editCaseFormSchema } from "@/lib/validation/case";
import { ROUTES, caseRoute } from "@/lib/routes";
import { useSessionStore } from "@/stores/session-store";
import { PERMISSION } from "@/types/authorization";
import { DEFAULT_CASE_LIST_QUERY } from "@/types/case-management";
import type { EditCaseFormValues } from "@/lib/validation/case";
import type { LegalCase } from "@/types/case";
import type { UserRole } from "@/types/user";
import {
  casePagePayload,
  caseUserPayload,
  errorEnvelope,
  legalCasePayload,
  managedUserPayload,
  mockFetch,
  sessionUserWithRole,
  userPagePayload,
} from "./helpers";

let pathname = ROUTES.cases;

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
 * The two endpoints the case screens touch: cases, and the user directory the
 * assignment pickers read.
 */
function mockCaseApi(
  cases: Parameters<typeof mockFetch>[0][string],
  lawyers = [managedUserPayload({ id: caseUserPayload().id })],
) {
  return mockFetch({
    "/cases": cases,
    "/users": { body: userPagePayload(lawyers) },
  });
}

/** Requests the client actually made to `/cases`, in order. */
function caseRequests(requests: Array<{ url: string; method: string; body: unknown }>) {
  return requests.filter((request) => request.url.includes("/cases"));
}

/** A `LegalCase` in the app's domain shape, for the dialogs that take one. */
function legalCase(overrides: Partial<LegalCase> = {}): LegalCase {
  return {
    id: "33333333-3333-4333-8333-333333333333",
    caseNumber: "CASE-2026-0001",
    title: "Benali v. Societe Atlas",
    description: "Breach of a supply contract.",
    category: "Commercial",
    status: "open",
    priority: "high",
    courtName: "Tribunal de Commerce de Casablanca",
    filingDate: "2026-05-10",
    nextHearingDate: "2026-06-10",
    assignedLawyerId: null,
    assignedCourtRepresentativeId: null,
    assignedLawyer: null,
    assignedCourtRepresentative: null,
    createdBy: null,
    updatedBy: null,
    creator: null,
    updater: null,
    createdAt: "2026-05-10T09:00:00Z",
    updatedAt: "2026-05-20T08:30:00Z",
    isArchived: false,
    allowedTransitions: ["in_progress", "waiting_for_hearing", "closed", "archived"],
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// Query building
// --------------------------------------------------------------------------- //

describe("buildCaseListParams", () => {
  it("always sends page, size, and sort", () => {
    const params = new URLSearchParams(buildCaseListParams(DEFAULT_CASE_LIST_QUERY));

    expect(params.get("page")).toBe("1");
    expect(params.get("page_size")).toBe("20");
    expect(params.get("sort_by")).toBe("created_at");
    expect(params.get("sort_order")).toBe("desc");
  });

  it("omits empty search terms, blank dates, and unset filters", () => {
    // Sending blanks would make the request URL — and therefore the cache key —
    // claim a filter that is not applied.
    const params = new URLSearchParams(buildCaseListParams(DEFAULT_CASE_LIST_QUERY));

    for (const key of [
      "search",
      "status",
      "priority",
      "assigned_lawyer_id",
      "assigned_court_representative_id",
      "court_name",
      "filing_date_from",
      "hearing_date_to",
    ]) {
      expect(params.has(key), `${key} should be omitted`).toBe(false);
    }
  });

  it("includes a trimmed search and every active filter", () => {
    const params = new URLSearchParams(
      buildCaseListParams({
        ...DEFAULT_CASE_LIST_QUERY,
        search: "  atlas  ",
        status: "open",
        priority: "urgent",
        assignedLawyerId: "lawyer-1",
        assignedCourtRepresentativeId: "court-1",
        courtName: " casablanca ",
        filingDateFrom: "2026-05-01",
        filingDateTo: "2026-05-31",
        hearingDateFrom: "2026-06-01",
        hearingDateTo: "2026-06-30",
      }),
    );

    expect(params.get("search")).toBe("atlas");
    expect(params.get("status")).toBe("open");
    expect(params.get("priority")).toBe("urgent");
    expect(params.get("assigned_lawyer_id")).toBe("lawyer-1");
    expect(params.get("assigned_court_representative_id")).toBe("court-1");
    expect(params.get("court_name")).toBe("casablanca");
    expect(params.get("filing_date_from")).toBe("2026-05-01");
    expect(params.get("hearing_date_to")).toBe("2026-06-30");
  });
});

// --------------------------------------------------------------------------- //
// Form validation
// --------------------------------------------------------------------------- //

describe("case form validation", () => {
  const valid = {
    caseNumber: "",
    title: "Benali v. Societe Atlas",
    description: "Breach of a supply contract.",
    category: "Commercial",
    status: "draft" as const,
    priority: "medium" as const,
    courtName: "Tribunal de Commerce",
    filingDate: "2026-05-10",
    nextHearingDate: "2026-06-10",
    assignedLawyerId: "",
    assignedCourtRepresentativeId: "",
  };

  it("normalizes the title and collapses its whitespace", () => {
    const result = createCaseFormSchema.parse({ ...valid, title: "  Benali   v.  Atlas " });

    expect(result.title).toBe("Benali v. Atlas");
  });

  it("rejects a blank title", () => {
    expect(createCaseFormSchema.safeParse({ ...valid, title: "   " }).success).toBe(false);
  });

  it("accepts an empty case number as 'generate one'", () => {
    expect(createCaseFormSchema.parse({ ...valid, caseNumber: "" }).caseNumber).toBe("");
  });

  it("uppercases a supplied case number", () => {
    // So the same reference cannot be filed twice in different casings.
    expect(createCaseFormSchema.parse({ ...valid, caseNumber: "tc/2026/44" }).caseNumber).toBe(
      "TC/2026/44",
    );
  });

  it("rejects a case number containing unusable characters", () => {
    expect(createCaseFormSchema.safeParse({ ...valid, caseNumber: "TC 2026!" }).success).toBe(
      false,
    );
  });

  it("rejects a hearing scheduled before the filing", () => {
    const result = createCaseFormSchema.safeParse({
      ...valid,
      filingDate: "2026-05-10",
      nextHearingDate: "2026-05-09",
    });

    expect(result.success).toBe(false);
    // Reported against the field the user would fix, not as a form-wide banner.
    expect(result.success === false && result.error.issues[0]?.path).toEqual([
      "nextHearingDate",
    ]);
  });

  it("accepts a hearing on the filing date", () => {
    expect(
      createCaseFormSchema.safeParse({
        ...valid,
        filingDate: "2026-05-10",
        nextHearingDate: "2026-05-10",
      }).success,
    ).toBe(true);
  });

  it("accepts a hearing with no filing date", () => {
    expect(
      createCaseFormSchema.safeParse({ ...valid, filingDate: "", nextHearingDate: "2020-01-01" })
        .success,
    ).toBe(true);
  });

  it("the edit form has no case-number field", () => {
    // The number identifies the case and is immutable once filed.
    const { caseNumber, ...withoutNumber } = valid;
    void caseNumber;

    expect(editCaseFormSchema.safeParse(withoutNumber).success).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// Partial updates
// --------------------------------------------------------------------------- //

describe("changedCaseFields", () => {
  const stored = legalCase({ assignedLawyerId: "lawyer-1" });

  const values: EditCaseFormValues = {
    title: stored.title,
    description: stored.description ?? "",
    category: stored.category ?? "",
    status: stored.status,
    priority: stored.priority,
    courtName: stored.courtName ?? "",
    filingDate: stored.filingDate ?? "",
    nextHearingDate: stored.nextHearingDate ?? "",
    assignedLawyerId: stored.assignedLawyerId ?? "",
    assignedCourtRepresentativeId: "",
  };

  it("sends nothing when nothing changed", () => {
    // A PATCH that echoed every field would overwrite a colleague's concurrent
    // edit with values this dialog loaded before it happened.
    expect(changedCaseFields(stored, values, { includeAssignments: true })).toEqual({});
  });

  it("sends only the fields that differ", () => {
    const payload = changedCaseFields(
      stored,
      { ...values, priority: "urgent" },
      { includeAssignments: true },
    );

    expect(payload).toEqual({ priority: "urgent" });
  });

  it("expresses an emptied field as an explicit null", () => {
    const payload = changedCaseFields(
      stored,
      { ...values, courtName: "" },
      { includeAssignments: true },
    );

    expect(payload).toEqual({ courtName: null });
  });

  it("omits the assignment fields when the caller cannot assign", () => {
    // Sending them would be refused outright: assignment is `cases:assign`, and
    // a partly-permitted PATCH is rejected in full by the API.
    const payload = changedCaseFields(
      stored,
      { ...values, assignedLawyerId: "" },
      { includeAssignments: false },
    );

    expect(payload).toEqual({});
  });

  it("expresses an unassignment as an explicit null when permitted", () => {
    const payload = changedCaseFields(
      stored,
      { ...values, assignedLawyerId: "" },
      { includeAssignments: true },
    );

    expect(payload).toEqual({ assignedLawyerId: null });
  });
});

// --------------------------------------------------------------------------- //
// Authorization
// --------------------------------------------------------------------------- //

describe("case management authorization", () => {
  it("requires a session for /cases and everything under it", async () => {
    const { proxy } = await import("@/proxy");
    const { NextRequest } = await import("next/server");

    for (const path of [ROUTES.cases, caseRoute("abc")]) {
      const response = proxy(new NextRequest(new URL(path, "http://localhost:3000")));
      expect(response?.status).toBe(307);
      expect(response?.headers.get("location")).toContain("/login");
    }
  });

  it("gates /cases on cases:view", () => {
    expect(accessRuleForPath(ROUTES.cases)).toEqual({ permission: PERMISSION.casesView });
  });

  it("gates a case's details page through the same rule", () => {
    // Longest-prefix matching, so a new page under /cases needs no declaration.
    expect(accessRuleForPath(caseRoute("abc"))).toEqual({ permission: PERMISSION.casesView });
  });

  it.each(["administrator", "lawyer", "court"] as const)("lets %s reach the page", (role) => {
    // Every role holds `cases:view`; *which* cases they see is decided per
    // resource by the API, not by the route guard.
    pathname = ROUTES.cases;
    signInAs(role);

    renderWithQuery(
      <RouteGuard>
        <p>Case list</p>
      </RouteGuard>,
    );

    expect(screen.getByText("Case list")).toBeInTheDocument();
  });

  it("hides New case from a lawyer", async () => {
    signInAs("lawyer");
    mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    expect(screen.queryByRole("button", { name: /new case/i })).not.toBeInTheDocument();
  });

  it("offers New case to an administrator", async () => {
    signInAs("administrator");
    mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    expect(screen.getAllByRole("button", { name: /new case/i }).length).toBeGreaterThan(0);
  });

  it("hides the assignee filters from a lawyer", async () => {
    // Reading them needs `users:view`; a lawyer sees only their own cases, so
    // the filter would have one possible answer anyway.
    signInAs("lawyer");
    mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    expect(screen.queryByLabelText("Assigned lawyer")).not.toBeInTheDocument();
  });

  it("hides Archive from a lawyer's row menu", async () => {
    const user = userEvent.setup();
    signInAs("lawyer");
    mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    await user.click(screen.getByRole("button", { name: "Actions for CASE-2026-0001" }));

    expect(await screen.findByRole("menuitem", { name: /view details/i })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /^archive$/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("menuitem", { name: /manage assignments/i }),
    ).not.toBeInTheDocument();
  });

  it("offers Archive and assignments to an administrator", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    await user.click(screen.getByRole("button", { name: "Actions for CASE-2026-0001" }));

    expect(await screen.findByRole("menuitem", { name: /^archive$/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /manage assignments/i })).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Listing: search, filters, sorting, pagination
// --------------------------------------------------------------------------- //

describe("CaseList", () => {
  it("renders a row per case with the documented columns", async () => {
    signInAs("administrator");
    mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);

    expect(await screen.findByText("Benali v. Societe Atlas")).toBeInTheDocument();
    expect(screen.getByText("CASE-2026-0001")).toBeInTheDocument();
    expect(screen.getByText("Open")).toBeInTheDocument();
    expect(screen.getByText("High")).toBeInTheDocument();
    expect(screen.getByText("Tribunal de Commerce de Casablanca")).toBeInTheDocument();
  });

  it("links each case to its details page", async () => {
    signInAs("administrator");
    mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);

    const link = await screen.findByRole("link", { name: "Benali v. Societe Atlas" });
    expect(link).toHaveAttribute("href", caseRoute(legalCasePayload().id as string));
  });

  it("shows a skeleton while the first page loads", () => {
    signInAs("administrator");
    mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);

    expect(screen.getByLabelText("Loading cases")).toBeInTheDocument();
  });

  it("shows an empty state with a call to action when there are no cases", async () => {
    signInAs("administrator");
    mockCaseApi({ body: casePagePayload([], { total_records: 0 }) });

    renderWithQuery(<CaseList />);

    expect(await screen.findByText("No cases yet")).toBeInTheDocument();
  });

  it("shows an error state with a retry when the request fails", async () => {
    signInAs("administrator");
    mockCaseApi({ status: 500, body: errorEnvelope("internal_error") });

    renderWithQuery(<CaseList />);

    expect(await screen.findByText("Could not load cases")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("searches, and resets to the first page when it does", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    await user.type(screen.getByLabelText("Search"), "atlas");

    await waitFor(() => {
      const last = caseRequests(requests).at(-1);
      expect(last?.url).toContain("search=atlas");
      expect(last?.url).toContain("page=1");
    });
  });

  it("debounces typing into a single request", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");
    const before = caseRequests(requests).length;

    await user.type(screen.getByLabelText("Search"), "atlas");
    await waitFor(() => {
      expect(caseRequests(requests).at(-1)?.url).toContain("search=atlas");
    });

    // One request for the settled term, not one per keystroke.
    expect(caseRequests(requests).length - before).toBeLessThanOrEqual(2);
  });

  it("shows a distinct empty state when the filters match nothing", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockCaseApi([
      { body: casePagePayload() },
      { body: casePagePayload([], { total_records: 0 }) },
    ]);

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    await user.type(screen.getByLabelText("Search"), "nothing");

    expect(await screen.findByText("No cases match your filters")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("filters by status", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    await user.click(screen.getByLabelText("Status"));
    await user.click(await screen.findByRole("option", { name: "Waiting for Hearing" }));

    await waitFor(() => {
      expect(caseRequests(requests).at(-1)?.url).toContain("status=waiting_for_hearing");
    });
  });

  it("combines the status and priority filters", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    await user.click(screen.getByLabelText("Status"));
    await user.click(await screen.findByRole("option", { name: "Open" }));
    await waitFor(() => expect(caseRequests(requests).at(-1)?.url).toContain("status=open"));

    await user.click(screen.getByLabelText("Priority"));
    await user.click(await screen.findByRole("option", { name: "Urgent" }));

    await waitFor(() => {
      const url = caseRequests(requests).at(-1)?.url ?? "";
      expect(url).toContain("status=open");
      expect(url).toContain("priority=urgent");
    });
  });

  it("filters by court name", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    await user.type(screen.getByLabelText("Court"), "casablanca");

    await waitFor(() => {
      expect(caseRequests(requests).at(-1)?.url).toContain("court_name=casablanca");
    });
  });

  it("filters by a filing-date range", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    await user.type(screen.getByLabelText("Filed from"), "2026-05-01");

    await waitFor(() => {
      expect(caseRequests(requests).at(-1)?.url).toContain("filing_date_from=2026-05-01");
    });
  });

  it("clears every filter at once", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    await user.click(screen.getByLabelText("Priority"));
    await user.click(await screen.findByRole("option", { name: "Urgent" }));
    await waitFor(() => expect(caseRequests(requests).at(-1)?.url).toContain("priority=urgent"));

    await user.click(screen.getByRole("button", { name: /clear/i }));

    await waitFor(() => {
      expect(caseRequests(requests).at(-1)?.url).not.toContain("priority=");
    });
  });

  it("sorts by a column, and reverses on a second click", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    const header = screen.getByRole("button", { name: /sort by case number/i });
    await user.click(header);
    await waitFor(() => {
      const url = caseRequests(requests).at(-1)?.url ?? "";
      expect(url).toContain("sort_by=case_number");
      expect(url).toContain("sort_order=asc");
    });

    await user.click(screen.getByRole("button", { name: /sorted ascending/i }));
    await waitFor(() => {
      expect(caseRequests(requests).at(-1)?.url).toContain("sort_order=desc");
    });
  });

  it("announces the sorted column to assistive technology", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockCaseApi({ body: casePagePayload() });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    await user.click(screen.getByRole("button", { name: /sort by priority/i }));

    await waitFor(() => {
      expect(
        screen.getByRole("columnheader", { name: /priority/i }),
      ).toHaveAttribute("aria-sort", "ascending");
    });
  });

  it("pages forward and keeps the page it was asked for", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({
      body: casePagePayload([legalCasePayload()], { total_records: 45, total_pages: 3 }),
    });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    // Anchored: the table also has a "Next hearing" sort button.
    await user.click(screen.getByRole("button", { name: /^next$/i }));

    await waitFor(() => {
      expect(caseRequests(requests).at(-1)?.url).toContain("page=2");
    });
  });

  it("reports the record range rather than only the page number", async () => {
    signInAs("administrator");
    mockCaseApi({
      body: casePagePayload([legalCasePayload()], {
        total_records: 45,
        page_size: 20,
        total_pages: 3,
      }),
    });

    renderWithQuery(<CaseList />);

    // The range answers what a user actually asks — how much of the caseload
    // they are looking at — which "page 1 of 3" does not.
    expect(await screen.findByText(/showing 1–20 of 45 cases/i)).toBeInTheDocument();
  });

  it("disables Previous on the first page", async () => {
    signInAs("administrator");
    mockCaseApi({
      body: casePagePayload([legalCasePayload()], { total_records: 45, total_pages: 3 }),
    });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    expect(screen.getByRole("button", { name: /previous/i })).toBeDisabled();
  });

  it("states plainly when a case has nobody assigned", async () => {
    // An empty cell reads as missing data; the absence of a lawyer is a fact.
    signInAs("administrator");
    mockCaseApi({
      body: casePagePayload([
        legalCasePayload({ assigned_lawyer: null, assigned_lawyer_id: null }),
      ]),
    });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    expect(screen.getAllByText("Unassigned").length).toBeGreaterThan(0);
  });
});

// --------------------------------------------------------------------------- //
// Creating
// --------------------------------------------------------------------------- //

describe("CreateCaseDialog", () => {
  it("sends the case without a case number when the field is empty", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: legalCasePayload() });

    renderWithQuery(<CreateCaseDialog open onOpenChange={vi.fn()} />);

    await user.type(screen.getByLabelText("Title"), "Benali v. Atlas");
    await user.click(screen.getByRole("button", { name: /create case/i }));

    await waitFor(() => {
      const post = caseRequests(requests).find((request) => request.method === "POST");
      expect(post).toBeDefined();
      // Omitted, not blank — that is what asks the API to generate one.
      expect(post?.body).not.toHaveProperty("case_number");
      expect(post?.body).toMatchObject({ title: "Benali v. Atlas", status: "draft" });
    });
  });

  it("sends a supplied case number, uppercased", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: legalCasePayload() });

    renderWithQuery(<CreateCaseDialog open onOpenChange={vi.fn()} />);

    await user.type(screen.getByLabelText("Title"), "Benali v. Atlas");
    await user.type(screen.getByLabelText("Case number"), "tc/2026/44");
    await user.click(screen.getByRole("button", { name: /create case/i }));

    await waitFor(() => {
      const post = caseRequests(requests).find((request) => request.method === "POST");
      expect(post?.body).toMatchObject({ case_number: "TC/2026/44" });
    });
  });

  it("shows a validation error and sends nothing when the title is blank", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: legalCasePayload() });

    renderWithQuery(<CreateCaseDialog open onOpenChange={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: /create case/i }));

    expect(await screen.findByText("Title is required.")).toBeInTheDocument();
    expect(caseRequests(requests).some((request) => request.method === "POST")).toBe(false);
  });

  it("maps a server field error back onto its input", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockCaseApi({
      status: 409,
      body: errorEnvelope(
        "case_number_already_exists",
        "A case with this case number already exists.",
      ),
    });

    renderWithQuery(<CreateCaseDialog open onOpenChange={vi.fn()} />);

    await user.type(screen.getByLabelText("Title"), "Benali v. Atlas");
    await user.type(screen.getByLabelText("Case number"), "TC/2026/44");
    await user.click(screen.getByRole("button", { name: /create case/i }));

    expect(
      await screen.findByText("A case with this case number already exists."),
    ).toBeInTheDocument();
  });

  it("hides the assignment selects from a caller who cannot assign", async () => {
    // Sending them would be refused in full: assignment needs `cases:assign`.
    signInAs("lawyer");
    mockCaseApi({ body: legalCasePayload() });

    renderWithQuery(<CreateCaseDialog open onOpenChange={vi.fn()} />);

    expect(screen.queryByLabelText("Assigned lawyer")).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Editing
// --------------------------------------------------------------------------- //

describe("EditCaseDialog", () => {
  it("prefills from the case", async () => {
    signInAs("administrator");
    mockCaseApi({ body: legalCasePayload() });

    renderWithQuery(<EditCaseDialog legalCase={legalCase()} open onOpenChange={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByLabelText("Title")).toHaveValue("Benali v. Societe Atlas");
    });
    expect(screen.getByLabelText("Court")).toHaveValue("Tribunal de Commerce de Casablanca");
  });

  it("sends only what changed", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: legalCasePayload() });

    renderWithQuery(<EditCaseDialog legalCase={legalCase()} open onOpenChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText("Title")).toHaveValue("Benali v. Societe Atlas"));

    await user.clear(screen.getByLabelText("Title"));
    await user.type(screen.getByLabelText("Title"), "Renamed matter");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => {
      const patch = caseRequests(requests).find((request) => request.method === "PATCH");
      expect(patch?.body).toEqual({ title: "Renamed matter" });
    });
  });

  it("offers only the transitions the case can legally make", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockCaseApi({ body: legalCasePayload() });

    // A draft can only be opened or archived; the rules live on the server, and
    // the dialog renders what it was told.
    renderWithQuery(
      <EditCaseDialog
        legalCase={legalCase({ status: "draft", allowedTransitions: ["open", "archived"] })}
        open
        onOpenChange={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByLabelText("Title")).toHaveValue("Benali v. Societe Atlas"));

    await user.click(screen.getByLabelText("Status"));
    const listbox = await screen.findByRole("listbox");

    expect(within(listbox).getByRole("option", { name: "Draft" })).toBeInTheDocument();
    expect(within(listbox).getByRole("option", { name: "Open" })).toBeInTheDocument();
    expect(within(listbox).queryByRole("option", { name: "Closed" })).not.toBeInTheDocument();
  });

  it("surfaces a refused transition from the server", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockCaseApi({
      status: 409,
      body: errorEnvelope(
        "invalid_case_transition",
        "A case that is 'draft' cannot move to 'closed'.",
      ),
    });

    renderWithQuery(<EditCaseDialog legalCase={legalCase()} open onOpenChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText("Title")).toHaveValue("Benali v. Societe Atlas"));

    await user.clear(screen.getByLabelText("Title"));
    await user.type(screen.getByLabelText("Title"), "Renamed");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    // The server's message is passed through: only it knows which move failed.
    expect(
      await screen.findByText("A case that is 'draft' cannot move to 'closed'."),
    ).toBeInTheDocument();
  });

  it("does not send an empty PATCH", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: legalCasePayload() });

    renderWithQuery(<EditCaseDialog legalCase={legalCase()} open onOpenChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText("Title")).toHaveValue("Benali v. Societe Atlas"));

    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => {
      expect(caseRequests(requests).some((request) => request.method === "PATCH")).toBe(false);
    });
  });
});

// --------------------------------------------------------------------------- //
// Assignment
// --------------------------------------------------------------------------- //

describe("AssignCaseDialog", () => {
  it("assigns a lawyer through the assignments endpoint", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const lawyer = managedUserPayload({ id: "lawyer-1", role: "lawyer" });
    const { requests } = mockCaseApi({ body: legalCasePayload() }, [lawyer]);

    renderWithQuery(<AssignCaseDialog legalCase={legalCase()} open onOpenChange={vi.fn()} />);

    await user.click(screen.getByLabelText("Assigned lawyer"));
    await user.click(await screen.findByRole("option", { name: "Karim Zahra" }));
    await user.click(screen.getByRole("button", { name: /save assignments/i }));

    await waitFor(() => {
      const patch = caseRequests(requests).find((request) =>
        request.url.includes("/assignments"),
      );
      // Only the position that changed, in the API's wire vocabulary.
      expect(patch?.body).toEqual({ assigned_lawyer_id: "lawyer-1" });
    });
  });

  it("removes an assignment with an explicit null", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: legalCasePayload() }, [
      managedUserPayload({ id: "lawyer-1", role: "lawyer" }),
    ]);

    renderWithQuery(
      <AssignCaseDialog
        legalCase={legalCase({ assignedLawyerId: "lawyer-1" })}
        open
        onOpenChange={vi.fn()}
      />,
    );

    await user.click(screen.getByLabelText("Assigned lawyer"));
    await user.click(await screen.findByRole("option", { name: "Unassigned" }));
    await user.click(screen.getByRole("button", { name: /save assignments/i }));

    await waitFor(() => {
      const patch = caseRequests(requests).find((request) =>
        request.url.includes("/assignments"),
      );
      expect(patch?.body).toMatchObject({ assigned_lawyer_id: null });
    });
  });

  it("sends nothing when no assignment changed", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: legalCasePayload() });

    renderWithQuery(<AssignCaseDialog legalCase={legalCase()} open onOpenChange={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: /save assignments/i }));

    await waitFor(() => {
      expect(
        caseRequests(requests).some((request) => request.url.includes("/assignments")),
      ).toBe(false);
    });
  });

  it("surfaces a rejected assignment", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockCaseApi(
      {
        status: 422,
        body: errorEnvelope("invalid_assignment", "Invalid assignment.", [
          {
            field: "assigned_lawyer_id",
            message: "This user cannot be assigned to that position.",
          },
        ]),
      },
      [managedUserPayload({ id: "lawyer-1", role: "lawyer" })],
    );

    renderWithQuery(<AssignCaseDialog legalCase={legalCase()} open onOpenChange={vi.fn()} />);

    await user.click(screen.getByLabelText("Assigned lawyer"));
    await user.click(await screen.findByRole("option", { name: "Karim Zahra" }));
    await user.click(screen.getByRole("button", { name: /save assignments/i }));

    expect(
      await screen.findByText("This user cannot be assigned to that position."),
    ).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Archiving
// --------------------------------------------------------------------------- //

describe("ArchiveCaseDialog", () => {
  it("says the case is kept, not deleted", () => {
    // "Delete" is a misleading word for a soft delete, and someone expecting a
    // permanent one should learn otherwise here rather than afterwards.
    signInAs("administrator");
    mockCaseApi({ body: legalCasePayload() });

    renderWithQuery(<ArchiveCaseDialog legalCase={legalCase()} open onOpenChange={vi.fn()} />);

    expect(screen.getByText(/kept — not deleted/i)).toBeInTheDocument();
    expect(screen.getByText(/remains searchable/i)).toBeInTheDocument();
  });

  it("archives with a DELETE on confirmation", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const { requests } = mockCaseApi({ body: legalCasePayload({ status: "archived" }) });

    renderWithQuery(<ArchiveCaseDialog legalCase={legalCase()} open onOpenChange={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Archive" }));

    await waitFor(() => {
      expect(caseRequests(requests).some((request) => request.method === "DELETE")).toBe(true);
    });
  });

  it("shows the failure and stays open when archiving is refused", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    const onOpenChange = vi.fn();
    mockCaseApi({ status: 403, body: errorEnvelope("forbidden") });

    renderWithQuery(
      <ArchiveCaseDialog legalCase={legalCase()} open onOpenChange={onOpenChange} />,
    );

    await user.click(screen.getByRole("button", { name: "Archive" }));

    expect(
      await screen.findByText("You do not have permission to perform this action."),
    ).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("offers Restore instead of Archive on an archived case", async () => {
    const user = userEvent.setup();
    signInAs("administrator");
    mockCaseApi({ body: casePagePayload([legalCasePayload({ status: "archived", is_archived: true })]) });

    renderWithQuery(<CaseList />);
    await screen.findByText("Benali v. Societe Atlas");

    await user.click(screen.getByRole("button", { name: "Actions for CASE-2026-0001" }));

    expect(await screen.findByRole("menuitem", { name: /restore/i })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /^archive$/i })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Placeholder sections
// --------------------------------------------------------------------------- //

describe("CasePlaceholderSections", () => {
  it.each(["Notes", "AI Assistant", "Reports"])("reserves a card for %s", (title) => {
    render(<CasePlaceholderSections />);

    expect(screen.getByText(title)).toBeInTheDocument();
  });

  it.each(["Documents", "Timeline"])("no longer reserves a card for %s", (title) => {
    // Both modules shipped, so the case details page renders the real list and
    // the real history in their place — a placeholder beside a working feature
    // reads as a bug.
    render(<CasePlaceholderSections />);

    expect(screen.queryByText(title)).not.toBeInTheDocument();
  });

  it("says plainly that the modules are not built yet", () => {
    // So an empty card is never mistaken for a loading failure.
    render(<CasePlaceholderSections />);

    expect(screen.getAllByText("Available in an upcoming release.")).toHaveLength(3);
  });
});
