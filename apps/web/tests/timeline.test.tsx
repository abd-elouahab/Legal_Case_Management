/**
 * Tests for the Timeline & Audit Trail client.
 *
 * Cover what the spec asks for: rendering, event ordering, search, filters,
 * pagination, and what an unauthorized role is shown.
 *
 * These verify what the user is *shown* and what the client *sends*. The API is
 * the real boundary — its 401/403, the per-case assignment check, and the
 * automatic generation of the events themselves are covered by
 * `tests/integration/test_timeline.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CaseTimeline } from "@/components/timeline/case-timeline";
import { TimelineEntry } from "@/components/timeline/timeline-entry";
import { TimelinePagination } from "@/components/timeline/timeline-pagination";
import { TimelineSkeleton } from "@/components/timeline/timeline-skeleton";
import { buildTimelineParams } from "@/lib/api/timeline";
import { formatEventTime } from "@/lib/format";
import { ROUTES } from "@/lib/routes";
import { timelineEventSchema, timelineQuerySchema } from "@/lib/validation/timeline";
import { useSessionStore } from "@/stores/session-store";
import {
  DEFAULT_TIMELINE_QUERY,
  type TimelineQuery,
} from "@/types/timeline-management";
import {
  TIMELINE_EVENT_TYPES,
  TIMELINE_EVENT_TYPE_LABELS,
  timelineEventLabel,
  type TimelineEvent,
} from "@/types/timeline";
import type { UserRole } from "@/types/user";
import {
  errorEnvelope,
  legalCasePayload,
  managedUserPayload,
  mockFetch,
  sessionUserWithRole,
  timelineEventPayload,
  timelinePagePayload,
  userPagePayload,
} from "./helpers";

const CASE_ID = legalCasePayload().id;

vi.mock("next/navigation", () => ({
  usePathname: () => ROUTES.cases,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
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
 * The endpoints the timeline screen touches: the case timeline itself, and the
 * user directory the "Performed by" filter reads.
 */
function mockTimelineApi(
  timeline: Parameters<typeof mockFetch>[0][string] = { body: timelinePagePayload() },
) {
  return mockFetch({
    "/timeline": timeline,
    "/users": { body: userPagePayload([managedUserPayload()]) },
  });
}

/** The last timeline request the client issued. */
function lastTimelineRequest(requests: Array<{ url: string }>) {
  return [...requests].reverse().find((request) => request.url.includes("/timeline?"));
}

function eventFor(overrides: Partial<TimelineEvent> = {}): TimelineEvent {
  return {
    id: "event-1",
    caseId: CASE_ID,
    eventType: "document_uploaded",
    category: "document",
    title: "Document Uploaded",
    description: 'Amina Benali uploaded "Contract.pdf".',
    actorId: "user-1",
    actorName: "Amina Benali",
    actorRole: "administrator",
    metadata: { filename: "Contract.pdf" },
    createdAt: "2026-07-20T14:32:00Z",
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// Query building
// --------------------------------------------------------------------------- //

describe("buildTimelineParams", () => {
  it("always sends the page, size, and sort", () => {
    const params = new URLSearchParams(buildTimelineParams(DEFAULT_TIMELINE_QUERY));

    expect(params.get("page")).toBe("1");
    expect(params.get("page_size")).toBe("20");
    expect(params.get("sort_by")).toBe("created_at");
    // Reverse chronological is how a timeline is read.
    expect(params.get("sort_order")).toBe("desc");
  });

  it("omits empty filters rather than sending blanks", () => {
    // So the request URL reflects what is actually being asked — which is also
    // what makes the query a stable cache key.
    const params = new URLSearchParams(buildTimelineParams(DEFAULT_TIMELINE_QUERY));

    for (const key of ["search", "event_type", "actor_id", "date_from", "date_to"]) {
      expect(params.has(key)).toBe(false);
    }
  });

  it("maps every filter onto its wire name", () => {
    const query: TimelineQuery = {
      ...DEFAULT_TIMELINE_QUERY,
      search: "  contract  ",
      eventType: "status_changed",
      actorId: "user-9",
      dateFrom: "2026-07-01",
      dateTo: "2026-07-31",
      sortOrder: "asc",
    };
    const params = new URLSearchParams(buildTimelineParams(query));

    expect(params.get("search")).toBe("contract");
    expect(params.get("event_type")).toBe("status_changed");
    expect(params.get("actor_id")).toBe("user-9");
    expect(params.get("date_from")).toBe("2026-07-01");
    expect(params.get("date_to")).toBe("2026-07-31");
    expect(params.get("sort_order")).toBe("asc");
  });
});

describe("timelineQuerySchema", () => {
  it("corrects an out-of-range page rather than sending it", () => {
    expect(timelineQuerySchema.parse({ ...DEFAULT_TIMELINE_QUERY, page: 0 }).page).toBe(1);
  });

  it("caps the page size", () => {
    expect(
      timelineQuerySchema.parse({ ...DEFAULT_TIMELINE_QUERY, pageSize: 5_000 }).pageSize,
    ).toBe(20);
  });

  it("discards a malformed date", () => {
    expect(
      timelineQuerySchema.parse({ ...DEFAULT_TIMELINE_QUERY, dateFrom: "yesterday" }).dateFrom,
    ).toBe("");
  });

  it("accepts an event type the client has never heard of", () => {
    // The API's registry is an open set; a filter for a later module's type must
    // survive validation rather than being silently dropped.
    expect(
      timelineQuerySchema.parse({ ...DEFAULT_TIMELINE_QUERY, eventType: "hearing_scheduled" })
        .eventType,
    ).toBe("hearing_scheduled");
  });
});

// --------------------------------------------------------------------------- //
// Response validation
// --------------------------------------------------------------------------- //

describe("timelineEventSchema", () => {
  it("accepts the API's payload", () => {
    expect(() => timelineEventSchema.parse(timelineEventPayload())).not.toThrow();
  });

  it("accepts an event type this build does not know", () => {
    const parsed = timelineEventSchema.parse(
      timelineEventPayload({ event_type: "hearing_scheduled", category: "case" }),
    );

    expect(parsed.event_type).toBe("hearing_scheduled");
  });

  it("defaults an absent metadata object rather than failing", () => {
    const { metadata: _omitted, ...withoutMetadata } = timelineEventPayload();

    expect(timelineEventSchema.parse(withoutMetadata).metadata).toEqual({});
  });

  it("falls back on an unrecognised category", () => {
    // `category` is computed server-side, so an unknown value means the contract
    // changed — the entry should still render with a neutral icon.
    expect(timelineEventSchema.parse(timelineEventPayload({ category: "weather" })).category).toBe(
      "case",
    );
  });
});

describe("timelineEventLabel", () => {
  it("labels every type the platform records", () => {
    for (const type of TIMELINE_EVENT_TYPES) {
      expect(timelineEventLabel(type)).toBe(TIMELINE_EVENT_TYPE_LABELS[type]);
    }
  });

  it("renders an unknown type as English rather than as a database value", () => {
    expect(timelineEventLabel("hearing_scheduled")).toBe("Hearing scheduled");
  });
});

// --------------------------------------------------------------------------- //
// Timestamp formatting
// --------------------------------------------------------------------------- //

describe("formatEventTime", () => {
  const now = new Date("2026-07-31T18:00:00Z");

  it("says Today for the current day", () => {
    expect(formatEventTime("2026-07-31T14:32:00Z", now)).toMatch(/^Today • /);
  });

  it("says Yesterday for the day before", () => {
    expect(formatEventTime("2026-07-30T09:15:00Z", now)).toMatch(/^Yesterday • /);
  });

  it("omits the year within the current year", () => {
    const formatted = formatEventTime("2026-07-24T14:32:00Z", now);

    expect(formatted).toContain("July");
    expect(formatted).not.toContain("2026");
  });

  it("includes the year once it stops being obvious", () => {
    expect(formatEventTime("2025-07-24T14:32:00Z", now)).toContain("2025");
  });

  it("renders a missing timestamp as a dash rather than as Invalid Date", () => {
    expect(formatEventTime(null, now)).toBe("—");
  });
});

// --------------------------------------------------------------------------- //
// Entry rendering
// --------------------------------------------------------------------------- //

describe("TimelineEntry", () => {
  it("shows the title, description, user, role, and timestamp", () => {
    // The six things `08-timeline.md` requires an entry to display, minus the
    // icon, which is decorative and asserted separately.
    render(
      <ul>
        <TimelineEntry event={eventFor()} />
      </ul>,
    );

    expect(screen.getByText("Document Uploaded")).toBeInTheDocument();
    expect(screen.getByText('Amina Benali uploaded "Contract.pdf".')).toBeInTheDocument();
    expect(screen.getByText("Amina Benali")).toBeInTheDocument();
    expect(screen.getByText("Administrator")).toBeInTheDocument();
    expect(screen.getByRole("time")).toHaveAttribute("datetime", "2026-07-20T14:32:00Z");
  });

  it("carries the exact instant on the timestamp, not only the relative form", () => {
    // The precise time is what matters in a legal audit trail.
    render(
      <ul>
        <TimelineEntry event={eventFor()} />
      </ul>,
    );

    expect(screen.getByRole("time")).toHaveAttribute("title");
  });

  it("labels an event with no actor as System", () => {
    render(
      <ul>
        <TimelineEntry event={eventFor({ actorName: null, actorRole: null, actorId: null })} />
      </ul>,
    );

    expect(screen.getByText("System")).toBeInTheDocument();
  });

  it("shows an unrecognised role verbatim rather than blank", () => {
    // `actorRole` is a snapshot: a role retired from the platform must still read
    // back from an old entry.
    render(
      <ul>
        <TimelineEntry event={eventFor({ actorRole: "paralegal" })} />
      </ul>,
    );

    expect(screen.getByText("paralegal")).toBeInTheDocument();
  });

  it("renders an entry with no description", () => {
    render(
      <ul>
        <TimelineEntry event={eventFor({ description: null })} />
      </ul>,
    );

    expect(screen.getByText("Document Uploaded")).toBeInTheDocument();
  });

  it("does not expose raw metadata to the reader", () => {
    // The structured object exists for future modules and the single-event
    // endpoint, not so a user reads JSON.
    render(
      <ul>
        <TimelineEntry event={eventFor({ metadata: { secret_key: "should-not-render" } })} />
      </ul>,
    );

    expect(screen.queryByText(/should-not-render/)).not.toBeInTheDocument();
  });
});

describe("TimelineSkeleton", () => {
  it("announces itself as busy while loading", () => {
    render(<TimelineSkeleton />);

    expect(screen.getByTestId("timeline-skeleton")).toHaveAttribute("aria-busy", "true");
  });
});

// --------------------------------------------------------------------------- //
// The case timeline
// --------------------------------------------------------------------------- //

describe("CaseTimeline", () => {
  it("requests the timeline of the case it was given", async () => {
    signInAs("administrator");
    const { requests } = mockTimelineApi();

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);

    await waitFor(() => expect(lastTimelineRequest(requests)).toBeDefined());
    expect(lastTimelineRequest(requests)!.url).toContain(`/cases/${CASE_ID}/timeline`);
  });

  it("shows a skeleton before the first page arrives", () => {
    signInAs("administrator");
    mockTimelineApi();

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);

    expect(screen.getByTestId("timeline-skeleton")).toBeInTheDocument();
  });

  it("renders the events it received", async () => {
    signInAs("administrator");
    mockTimelineApi();

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);

    expect(await screen.findByText("Document Uploaded")).toBeInTheDocument();
    expect(
      screen.getByText('Amina Benali uploaded "contrat-de-bail.pdf".'),
    ).toBeInTheDocument();
  });

  it("preserves the order the API returned", async () => {
    // The server sorts; the client must not re-order, or the two would disagree
    // about what "newest first" means across a page boundary.
    signInAs("administrator");
    mockTimelineApi({
      body: timelinePagePayload([
        timelineEventPayload({ id: "e3", title: "Case Archived", event_type: "case_archived" }),
        timelineEventPayload({ id: "e2", title: "Status Changed", event_type: "status_changed" }),
        timelineEventPayload({ id: "e1", title: "Case Created", event_type: "case_created" }),
      ]),
    });

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);

    await screen.findByText("Case Archived");
    const entries = screen.getAllByRole("listitem");
    expect(within(entries[0]!).getByText("Case Archived")).toBeInTheDocument();
    expect(within(entries[1]!).getByText("Status Changed")).toBeInTheDocument();
    expect(within(entries[2]!).getByText("Case Created")).toBeInTheDocument();
  });

  it("shows a distinct empty state when nothing has happened yet", async () => {
    signInAs("administrator");
    mockTimelineApi({ body: timelinePagePayload([], { total_records: 0 }) });

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);

    expect(await screen.findByText("No activity yet")).toBeInTheDocument();
    // Nothing to clear, so no "Clear filters" affordance.
    expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
  });

  it("shows a different empty state when a search returns nothing", async () => {
    signInAs("administrator");
    mockTimelineApi({ body: timelinePagePayload([], { total_records: 0 }) });

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);
    await screen.findByText("No activity yet");

    await userEvent.type(screen.getByLabelText("Search activity"), "nothing matches");

    expect(await screen.findByText("No activity matches your filters")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("shows an error state with a retry when the request fails", async () => {
    signInAs("administrator");
    mockTimelineApi({ status: 500, body: errorEnvelope("internal_error") });

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("explains a refusal in terms the user can act on", async () => {
    // A caller not assigned to the case is refused outright rather than handed an
    // empty page, so this is a real state and not defensive padding.
    signInAs("lawyer");
    mockTimelineApi({ status: 403, body: errorEnvelope("forbidden") });

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);

    expect(
      await screen.findByText("You do not have permission to view this case's activity."),
    ).toBeInTheDocument();
  });

  // ------------------------------------------------------------- search #

  it("sends a debounced search term", async () => {
    signInAs("administrator");
    const { requests } = mockTimelineApi();

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);
    await screen.findByText("Document Uploaded");

    await userEvent.type(screen.getByLabelText("Search activity"), "contract");

    await waitFor(() =>
      expect(lastTimelineRequest(requests)!.url).toContain("search=contract"),
    );
  });

  it("returns to the first page when the search changes", async () => {
    // Searching while on page 4 would otherwise ask for the fourth page of a
    // two-page result and show an empty list.
    signInAs("administrator");
    const { requests } = mockTimelineApi({
      body: timelinePagePayload([timelineEventPayload()], {
        total_records: 60,
        total_pages: 3,
      }),
    });

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);
    await screen.findByText("Document Uploaded");

    await userEvent.click(screen.getByRole("button", { name: /Next/ }));
    await waitFor(() => expect(lastTimelineRequest(requests)!.url).toContain("page=2"));

    await userEvent.type(screen.getByLabelText("Search activity"), "x");

    await waitFor(() => expect(lastTimelineRequest(requests)!.url).toContain("page=1"));
  });

  // ------------------------------------------------------------ filters #

  it("filters by activity type", async () => {
    signInAs("administrator");
    const { requests } = mockTimelineApi();

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);
    await screen.findByText("Document Uploaded");

    await userEvent.click(screen.getByLabelText("Activity type"));
    await userEvent.click(await screen.findByRole("option", { name: "Status changed" }));

    await waitFor(() =>
      expect(lastTimelineRequest(requests)!.url).toContain("event_type=status_changed"),
    );
  });

  it("filters by date range", async () => {
    signInAs("administrator");
    const { requests } = mockTimelineApi();

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);
    await screen.findByText("Document Uploaded");

    await userEvent.type(screen.getByLabelText("From"), "2026-07-01");

    await waitFor(() =>
      expect(lastTimelineRequest(requests)!.url).toContain("date_from=2026-07-01"),
    );
  });

  it("offers the actor filter only to a caller who can read the directory", async () => {
    // A lawyer cannot resolve a name from an identifier, so the control would be
    // one they could not populate.
    signInAs("administrator");
    mockTimelineApi();

    const { unmount } = renderWithQuery(<CaseTimeline caseId={CASE_ID} />);
    expect(await screen.findByLabelText("Performed by")).toBeInTheDocument();
    unmount();

    signInAs("lawyer");
    mockTimelineApi();
    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);

    await screen.findByText("Document Uploaded");
    expect(screen.queryByLabelText("Performed by")).not.toBeInTheDocument();
  });

  it("clears every filter at once", async () => {
    signInAs("administrator");
    const { requests } = mockTimelineApi();

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);
    await screen.findByText("Document Uploaded");

    await userEvent.type(screen.getByLabelText("Search activity"), "contract");
    await waitFor(() =>
      expect(lastTimelineRequest(requests)!.url).toContain("search=contract"),
    );

    await userEvent.click(screen.getByRole("button", { name: /Clear/ }));

    await waitFor(() =>
      expect(lastTimelineRequest(requests)!.url).not.toContain("search="),
    );
  });

  // --------------------------------------------------------------- sort #

  it("flips between newest-first and oldest-first", async () => {
    signInAs("administrator");
    const { requests } = mockTimelineApi();

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);
    await screen.findByText("Document Uploaded");

    await userEvent.click(screen.getByRole("button", { name: "Sort oldest first" }));

    await waitFor(() => expect(lastTimelineRequest(requests)!.url).toContain("sort_order=asc"));
    expect(screen.getByRole("button", { name: "Sort newest first" })).toBeInTheDocument();
  });

  // --------------------------------------------------------- pagination #

  it("pages through a long history", async () => {
    signInAs("administrator");
    const { requests } = mockTimelineApi({
      body: timelinePagePayload([timelineEventPayload()], {
        total_records: 45,
        total_pages: 3,
      }),
    });

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);
    await screen.findByText("Document Uploaded");

    expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Next/ }));

    await waitFor(() => expect(lastTimelineRequest(requests)!.url).toContain("page=2"));
  });

  it("does not offer Previous on the first page", async () => {
    signInAs("administrator");
    mockTimelineApi({
      body: timelinePagePayload([timelineEventPayload()], {
        total_records: 45,
        total_pages: 3,
      }),
    });

    renderWithQuery(<CaseTimeline caseId={CASE_ID} />);
    await screen.findByText("Document Uploaded");

    expect(screen.getByRole("button", { name: /Previous/ })).toBeDisabled();
  });
});

describe("TimelinePagination", () => {
  it("reports the record range, not only the page number", () => {
    // "Showing 21–40 of 137" answers the question a user actually has.
    render(
      <TimelinePagination
        page={2}
        pageSize={20}
        totalPages={7}
        totalRecords={137}
        onPageChange={vi.fn()}
      />,
    );

    expect(screen.getByText("Showing 21–40 of 137 entries")).toBeInTheDocument();
  });

  it("uses the singular for one entry", () => {
    render(
      <TimelinePagination
        page={1}
        pageSize={20}
        totalPages={1}
        totalRecords={1}
        onPageChange={vi.fn()}
      />,
    );

    expect(screen.getByText("Showing 1–1 of 1 entry")).toBeInTheDocument();
  });

  it("shows a loading indicator while the next page is in flight", () => {
    // The previous page stays on screen, so without this pressing Next appears
    // to do nothing on a slow connection.
    render(
      <TimelinePagination
        page={1}
        pageSize={20}
        totalPages={3}
        totalRecords={45}
        onPageChange={vi.fn()}
        isLoading
      />,
    );

    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Next/ })).toBeDisabled();
  });

  it("announces the range politely as it changes", () => {
    const { container } = render(
      <TimelinePagination
        page={1}
        pageSize={20}
        totalPages={1}
        totalRecords={3}
        onPageChange={vi.fn()}
      />,
    );

    expect(container.querySelector('[aria-live="polite"]')).toBeInTheDocument();
  });
});
