/**
 * Tests for the Semantic Search client.
 *
 * Cover what the user is *shown* and what the client *sends*: the search form,
 * the ranked passages and their citations, the "no results" and "not searched
 * yet" states, filters, paging, dependency-outage messages, the monitoring
 * panel, and what an unauthorized role gets instead.
 *
 * The API is the real boundary — its 401/403, the per-case scope applied inside
 * the vector query, the ranking, and the retrieval itself are covered by
 * `tests/integration/test_search.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CaseSearch } from "@/components/search/case-search";
import { SearchMetricsPanel } from "@/components/search/search-metrics-panel";
import { SearchResultCard } from "@/components/search/search-result-card";
import { SemanticSearch } from "@/components/search/semantic-search";
import { SEARCH_ENDPOINTS } from "@/lib/api/config";
import { buildSearchFilters, fetchSearchMetrics, searchDocuments } from "@/lib/api/search";
import { ROUTES } from "@/lib/routes";
import { searchFormSchema, searchResponseSchema } from "@/lib/validation/search";
import { useSessionStore } from "@/stores/session-store";
import {
  EMPTY_SEARCH_FILTERS,
  hasActiveFilters,
  relevancePercent,
  searchFailureLabel,
  searchLanguageLabel,
  type SearchResult,
} from "@/types/search";
import type { UserRole } from "@/types/user";
import {
  casePagePayload,
  errorEnvelope,
  mockFetch,
  searchMetricsPayload,
  searchResponsePayload,
  searchResultPayload,
  sessionUserWithRole,
} from "./helpers";

vi.mock("next/navigation", () => ({
  usePathname: () => ROUTES.search,
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

/** The app's `SearchResult`, built from the wire fixture the API would send. */
function resultFor(overrides: Record<string, unknown> = {}): SearchResult {
  const payload = searchResultPayload(overrides);

  return {
    documentId: payload.document_id,
    documentVersion: payload.document_version,
    caseId: payload.case_id,
    pageNumber: payload.page_number,
    chunkNumber: payload.chunk_number,
    score: payload.score,
    text: payload.text,
    language: payload.language,
    rank: payload.rank,
    document: payload.document
      ? {
          id: payload.document.id,
          caseId: payload.document.case_id,
          originalFilename: payload.document.original_filename,
          fileExtension: payload.document.file_extension,
          category: payload.document.category as SearchResult["document"] extends null
            ? never
            : "contract",
        }
      : null,
  };
}

/** Routes every search screen needs: the search itself plus the case picker. */
function searchRoutes(overrides: Record<string, unknown> = {}) {
  return {
    "/api/v1/cases": { body: casePagePayload() },
    "/api/v1/search": { body: searchResponsePayload() },
    ...overrides,
  };
}

async function submitSearch(query = "loyer payable d'avance") {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/search your documents/i), query);
  await user.click(screen.getByRole("button", { name: /^search$/i }));
  return user;
}

// --------------------------------------------------------------------------- //
// Domain helpers
// --------------------------------------------------------------------------- //

describe("search domain helpers", () => {
  it("renders a similarity as a percentage", () => {
    expect(relevancePercent(0.8421)).toBe(84);
  });

  it("clamps a negative similarity to zero rather than showing a negative match", () => {
    // Cosine similarity is [-1, 1]; a negative score means the passage points
    // away from the query. "-32% match" reads as a data error.
    expect(relevancePercent(-0.32)).toBe(0);
  });

  it("labels the languages the indexer can produce", () => {
    expect(searchLanguageLabel("ar")).toBe("Arabic");
    expect(searchLanguageLabel("fr")).toBe("French");
    expect(searchLanguageLabel("und")).toBe("Undetermined");
  });

  it("falls back for a language a later backend may report", () => {
    expect(searchLanguageLabel("es")).toBe("ES");
  });

  it("falls back for a failure code this build has never heard of", () => {
    expect(searchFailureLabel("reranker_unavailable")).toBe("Reranker unavailable");
    expect(searchFailureLabel(null)).toBe("Search failed");
  });

  it("reports whether anything is filtered", () => {
    expect(hasActiveFilters(EMPTY_SEARCH_FILTERS)).toBe(false);
    expect(hasActiveFilters({ ...EMPTY_SEARCH_FILTERS, languages: [] })).toBe(false);
    expect(hasActiveFilters({ ...EMPTY_SEARCH_FILTERS, languages: ["fr"] })).toBe(true);
  });
});

describe("search form validation", () => {
  it("accepts a natural-language question", () => {
    expect(searchFormSchema.safeParse({ query: "quand le loyer est-il dû ?" }).success).toBe(
      true,
    );
  });

  it("rejects a query with nothing to search for", () => {
    for (const query of ["", " ", "a", "???"]) {
      expect(searchFormSchema.safeParse({ query }).success).toBe(false);
    }
  });

  it("accepts an Arabic query", () => {
    // The platform exists to serve Arabic filings; a Latin-only rule would
    // reject every one of them.
    expect(searchFormSchema.safeParse({ query: "الكراء الشهري" }).success).toBe(true);
  });

  it("accepts a bare case number", () => {
    expect(searchFormSchema.safeParse({ query: "2024" }).success).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// The API client
// --------------------------------------------------------------------------- //

describe("search API client", () => {
  it("sends the query in a POST body, never in the URL", async () => {
    // A query string is written to the proxy's access log, the browser's
    // history, and the `Referer` of anything loaded next — three logs the
    // application does not control.
    const { requests } = mockFetch({ "/api/v1/search": { body: searchResponsePayload() } });

    await searchDocuments({
      query: "divorce Benali",
      limit: 10,
      offset: 0,
      minScore: null,
      filters: EMPTY_SEARCH_FILTERS,
    });

    const request = requests.at(-1)!;
    expect(request.method).toBe("POST");
    expect(request.url).not.toContain("divorce");
    expect(request.url).toContain(SEARCH_ENDPOINTS.search);
    expect(request.body).toMatchObject({ query: "divorce Benali" });
  });

  it("omits filters that are not set", () => {
    expect(buildSearchFilters(EMPTY_SEARCH_FILTERS)).toEqual({});
  });

  it("drops an empty filter array rather than sending one that matches nothing", () => {
    expect(buildSearchFilters({ ...EMPTY_SEARCH_FILTERS, languages: [] })).toEqual({});
  });

  it("maps the wire format onto the app's domain types", async () => {
    mockFetch({ "/api/v1/search": { body: searchResponsePayload() } });

    const response = await searchDocuments({
      query: "loyer",
      limit: 10,
      offset: 0,
      minScore: null,
      filters: EMPTY_SEARCH_FILTERS,
    });

    expect(response.results[0]).toMatchObject({
      documentId: "33333333-3333-4333-8333-333333333333",
      documentVersion: 1,
      pageNumber: 4,
      chunkNumber: 2,
      rank: 1,
    });
    expect(response.results[0]?.document?.originalFilename).toBe("bail-commercial.pdf");
  });

  it("accepts a language code this build has never seen", () => {
    // The label is produced per passage by a heuristic on the server; a strict
    // enum would make a newer backend unrenderable.
    const parsed = searchResponseSchema.safeParse(
      searchResponsePayload([searchResultPayload({ language: "es" })]),
    );

    expect(parsed.success).toBe(true);
  });

  it("fetches the monitoring view", async () => {
    mockFetch({ "/api/v1/search/metrics": { body: searchMetricsPayload() } });

    const metrics = await fetchSearchMetrics();

    expect(metrics.totalSearches).toBe(25);
    expect(metrics.averageScore).toBe(0.7412);
    expect(metrics.ranker).toBe("similarity");
  });
});

// --------------------------------------------------------------------------- //
// The result card
// --------------------------------------------------------------------------- //

describe("SearchResultCard", () => {
  it("shows the passage in full, with its citation", () => {
    // The passage is the evidence. Truncating it would send a lawyer to open
    // the document to find out whether the clause they need is inside — which
    // is the work this feature exists to save.
    const result = resultFor();
    render(<SearchResultCard result={result} />);

    expect(screen.getByText(result.text)).toBeInTheDocument();
    expect(screen.getByText("bail-commercial.pdf")).toBeInTheDocument();
    expect(screen.getByText(/page 4/i)).toBeInTheDocument();
    expect(screen.getByText(/passage 3/i)).toBeInTheDocument();
    expect(screen.getByText(/version 1/i)).toBeInTheDocument();
  });

  it("states relevance as a labelled figure, never as colour alone", () => {
    render(<SearchResultCard result={resultFor()} />);

    expect(screen.getByText("84% match")).toBeInTheDocument();
  });

  it("labels the category and the language", () => {
    render(<SearchResultCard result={resultFor()} />);

    expect(screen.getByText("Contract")).toBeInTheDocument();
    expect(screen.getByText("French")).toBeInTheDocument();
  });

  it("lets the browser choose the direction, so Arabic renders right-to-left", () => {
    const result = resultFor({ text: "الكراء الشهري يؤدى مسبقا", language: "ar" });
    render(<SearchResultCard result={result} />);

    expect(screen.getByText(result.text)).toHaveAttribute("dir", "auto");
  });

  it("links to the case, which the reader is certainly party to", () => {
    // The API would not have returned the passage otherwise. A link to a
    // document viewer could take a reader somewhere they may not open.
    render(<SearchResultCard result={resultFor()} />);

    expect(screen.getByRole("link", { name: /open case/i })).toHaveAttribute(
      "href",
      `/cases/${resultFor().caseId}`,
    );
  });

  it("renders a passage whose document summary is missing", () => {
    const result = resultFor({ document: null });
    render(<SearchResultCard result={result} />);

    expect(screen.getByText(result.text)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// The search screen
// --------------------------------------------------------------------------- //

describe("SemanticSearch", () => {
  it("invites a search before anything has been submitted", () => {
    signInAs("lawyer");
    mockFetch(searchRoutes());

    renderWithQuery(<SemanticSearch />);

    expect(screen.getByText(/nothing searched yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not search until the query is submitted", async () => {
    // Every request costs a query embedding on the server; search-as-you-type
    // would embed every prefix of every question.
    signInAs("lawyer");
    const { requests } = mockFetch(searchRoutes());
    renderWithQuery(<SemanticSearch />);

    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/search your documents/i), "loyer");

    expect(requests.filter((request) => request.url.includes("/search"))).toHaveLength(0);
  });

  it("renders the ranked passages once submitted", async () => {
    signInAs("lawyer");
    mockFetch(searchRoutes());
    renderWithQuery(<SemanticSearch />);

    await submitSearch();

    await waitFor(() =>
      expect(screen.getByText("bail-commercial.pdf")).toBeInTheDocument(),
    );
    expect(screen.getByText(/1 passage for/i)).toBeInTheDocument();
  });

  it("refuses a query with nothing to search for, without calling the API", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch(searchRoutes());
    renderWithQuery(<SemanticSearch />);

    const user = userEvent.setup();
    // Long enough to pass the length rule, so it is the "carries nothing to
    // search for" rule that refuses it — a single character would only prove
    // the length check works.
    await user.type(screen.getByLabelText(/search your documents/i), "???");
    await user.click(screen.getByRole("button", { name: /^search$/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/word or a number/i);
    expect(requests.filter((request) => request.url.includes("/search"))).toHaveLength(0);
  });

  it("distinguishes no results from not having searched", async () => {
    signInAs("lawyer");
    mockFetch(searchRoutes({ "/api/v1/search": { body: searchResponsePayload([]) } }));
    renderWithQuery(<SemanticSearch />);

    await submitSearch();

    expect(await screen.findByText(/no matching passages/i)).toBeInTheDocument();
    // And it is not an error: an empty corpus is an answer.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sends the filters the user chose", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch(searchRoutes());
    renderWithQuery(<SemanticSearch />);

    const user = userEvent.setup();
    await user.click(screen.getByLabelText(/^language$/i));
    await user.click(await screen.findByRole("option", { name: "Arabic" }));
    await submitSearch();

    await waitFor(() => {
      const request = requests.filter((entry) => entry.url.includes("/search")).at(-1);
      expect(request?.body).toMatchObject({ filters: { languages: ["ar"] } });
    });
  });

  it("names the dependency when search is unavailable", async () => {
    // "Search is unavailable" sends a user to retry forever; "the search index
    // is unreachable" sends them to an administrator.
    signInAs("lawyer");
    mockFetch(
      searchRoutes({
        "/api/v1/search": {
          status: 503,
          body: errorEnvelope(
            "vector_store_unavailable",
            "The search index is unavailable. Indexed documents are unaffected.",
          ),
        },
      }),
    );
    renderWithQuery(<SemanticSearch />);

    await submitSearch();

    expect(await screen.findByRole("alert")).toHaveTextContent(/search index is unavailable/i);
  });

  it("explains a disabled deployment", async () => {
    signInAs("lawyer");
    mockFetch(
      searchRoutes({
        "/api/v1/search": {
          status: 503,
          body: errorEnvelope("search_disabled", "Disabled."),
        },
      }),
    );
    renderWithQuery(<SemanticSearch />);

    await submitSearch();

    expect(await screen.findByRole("alert")).toHaveTextContent(/currently disabled/i);
  });

  it("does not retry a failed search", async () => {
    // A 503 means a dependency is down; retrying costs another query embedding
    // to fail the same way. The user retries by pressing the button.
    signInAs("lawyer");
    const { requests } = mockFetch(
      searchRoutes({
        "/api/v1/search": {
          status: 503,
          body: errorEnvelope("vector_store_unavailable", "Down."),
        },
      }),
    );
    renderWithQuery(<SemanticSearch />);

    await submitSearch();
    await screen.findByRole("alert");

    expect(requests.filter((request) => request.url.includes("/search"))).toHaveLength(1);
  });

  it("pages with the submitted query rather than what is in the box", async () => {
    // Otherwise "edit the box, press Next" silently searches for something else.
    signInAs("lawyer");
    const { requests } = mockFetch(
      searchRoutes({
        "/api/v1/search": {
          body: searchResponsePayload([searchResultPayload()], { has_more: true }),
        },
      }),
    );
    renderWithQuery(<SemanticSearch />);

    const user = await submitSearch("loyer payable");
    await screen.findByText("bail-commercial.pdf");

    await user.clear(screen.getByLabelText(/search your documents/i));
    await user.type(screen.getByLabelText(/search your documents/i), "something else");
    await user.click(screen.getByRole("button", { name: /next/i }));

    await waitFor(() => {
      const request = requests.filter((entry) => entry.url.includes("/search")).at(-1);
      expect(request?.body).toMatchObject({ query: "loyer payable", offset: 10 });
    });
  });

  it("offers no paging when the page did not fill", async () => {
    signInAs("lawyer");
    mockFetch(searchRoutes());
    renderWithQuery(<SemanticSearch />);

    await submitSearch();
    await screen.findByText("bail-commercial.pdf");

    expect(screen.queryByRole("button", { name: /next/i })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// The case-scoped panel
// --------------------------------------------------------------------------- //

describe("CaseSearch", () => {
  const caseId = "22222222-2222-4222-8222-222222222222";

  it("pins every search to the case", async () => {
    signInAs("lawyer");
    const { requests } = mockFetch(searchRoutes());
    renderWithQuery(<CaseSearch caseId={caseId} />);

    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/search this case's documents/i), "loyer");
    await user.click(screen.getByRole("button", { name: /^search$/i }));

    await waitFor(() => {
      const request = requests.filter((entry) => entry.url.includes("/search")).at(-1);
      expect(request?.body).toMatchObject({ filters: { case_id: caseId } });
    });
  });

  it("hides the case filter, so the view cannot be widened to the platform", () => {
    signInAs("lawyer");
    mockFetch(searchRoutes());
    renderWithQuery(<CaseSearch caseId={caseId} />);

    expect(screen.queryByLabelText(/^case$/i)).not.toBeInTheDocument();
  });

  it("renders nothing for a role without the search capability", () => {
    // A search box that answers 403 is worse than no search box. Presentation
    // only — the API authorizes every request independently.
    act(() => {
      useSessionStore.setState({
        user: { ...sessionUserWithRole("lawyer"), permissions: ["documents:view"] },
        status: "authenticated",
      });
    });
    mockFetch(searchRoutes());

    const { container } = renderWithQuery(<CaseSearch caseId={caseId} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("is offered to a court representative", () => {
    // They already read the full extracted text of these documents; withholding
    // search would leave them able to read every page but not find a clause.
    signInAs("court");
    mockFetch(searchRoutes());

    renderWithQuery(<CaseSearch caseId={caseId} />);

    expect(screen.getByLabelText(/search this case's documents/i)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// Monitoring
// --------------------------------------------------------------------------- //

describe("SearchMetricsPanel", () => {
  it("reports the four figures the spec names", async () => {
    mockFetch({ "/api/v1/search/metrics": { body: searchMetricsPayload() } });

    renderWithQuery(<SearchMetricsPanel />);

    expect(await screen.findByText("25")).toBeInTheDocument();
    expect(screen.getByText("139 ms")).toBeInTheDocument();
    expect(screen.getByText("74%")).toBeInTheDocument();
    // The stat tile, not the breakdown row that carries the same figure.
    expect(screen.getByText("Failures").nextElementSibling).toHaveTextContent("2");
  });

  it("warns when the embedding model cannot load", async () => {
    mockFetch({
      "/api/v1/search/metrics": {
        body: searchMetricsPayload({ embedding_available: false }),
      },
    });

    renderWithQuery(<SearchMetricsPanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/cannot be loaded/i);
  });

  it("warns when the vector database is unreachable", async () => {
    mockFetch({
      "/api/v1/search/metrics": {
        body: searchMetricsPayload({ vector_store_available: false }),
      },
    });

    renderWithQuery(<SearchMetricsPanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/not reachable/i);
  });

  it("says searching is switched off rather than showing an outage", async () => {
    mockFetch({
      "/api/v1/search/metrics": { body: searchMetricsPayload({ enabled: false }) },
    });

    renderWithQuery(<SearchMetricsPanel />);

    expect(await screen.findByText(/disabled on this deployment/i)).toBeInTheDocument();
  });

  it("states that the counters are per instance and reset on restart", async () => {
    // Otherwise the figures quietly mean less than they appear to.
    mockFetch({ "/api/v1/search/metrics": { body: searchMetricsPayload() } });

    renderWithQuery(<SearchMetricsPanel />);

    expect(await screen.findByText(/counted on this api instance since/i)).toBeInTheDocument();
  });

  it("breaks failures down by cause", async () => {
    mockFetch({ "/api/v1/search/metrics": { body: searchMetricsPayload() } });

    renderWithQuery(<SearchMetricsPanel />);

    expect(await screen.findByText("Search index unavailable")).toBeInTheDocument();
  });

  it("renders nothing rather than an error when the caller may not monitor", async () => {
    mockFetch({
      "/api/v1/search/metrics": { status: 403, body: errorEnvelope("forbidden") },
    });

    const { container } = renderWithQuery(<SearchMetricsPanel />);

    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
