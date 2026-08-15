/**
 * Tests for the AI Legal Assistant client.
 *
 * Cover what the user is *shown* and what the client *sends*: the transcript,
 * citations, the composer, streaming, suggested follow-ups, feedback, the
 * conversation list, the monitoring panel, and what an unauthorized role gets
 * instead.
 *
 * The API is the real boundary — its 401/403, the per-owner scope, the
 * retrieval, and the grounding are covered by
 * `tests/integration/test_assistant.py`.
 */

import { describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AssistantMetricsPanel } from "@/components/ai/assistant-metrics-panel";
import { AssistantWorkspace } from "@/components/ai/assistant-workspace";
import { ChatComposer } from "@/components/ai/chat-composer";
import { ChatMessage } from "@/components/ai/chat-message";
import { CitationList } from "@/components/ai/citation-list";
import { FollowUpSuggestions } from "@/components/ai/follow-up-suggestions";
import { ASSISTANT_ENDPOINTS } from "@/lib/api/config";
import {
  buildConversationQuery,
  fetchAssistantMetrics,
  fetchConversation,
  sendMessage,
} from "@/lib/api/assistant";
import { ROUTES } from "@/lib/routes";
import { messageFormSchema } from "@/lib/validation/assistant";
import { useSessionStore } from "@/stores/session-store";
import {
  citationRelevancePercent,
  type AssistantCitation,
  type ConversationMessage,
} from "@/types/assistant";
import type { UserRole } from "@/types/user";
import {
  assistantCitationPayload,
  assistantMetricsPayload,
  conversationDetailPayload,
  conversationMessagePayload,
  conversationPagePayload,
  conversationPayload,
  errorEnvelope,
  messageExchangePayload,
  mockFetch,
  sessionUserWithRole,
} from "./helpers";

vi.mock("next/navigation", () => ({
  usePathname: () => ROUTES.aiAssistant,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));

/** The conversation every fixture in this file describes. */
const CONVERSATION_ID = "66666666-6666-4666-8666-666666666666";

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

/** The app's `AssistantCitation`, built from the wire fixture the API would send. */
function citationFor(overrides: Record<string, unknown> = {}): AssistantCitation {
  const payload = assistantCitationPayload(overrides);

  return {
    marker: payload.marker,
    documentId: payload.document_id,
    documentName: payload.document_name,
    documentVersion: payload.document_version,
    pageNumber: payload.page_number,
    caseId: payload.case_id,
    score: payload.score,
    excerpt: payload.excerpt,
    excerptTruncated: payload.excerpt_truncated,
    referenced: payload.referenced,
  };
}

/** The app's `ConversationMessage`, built from the wire fixture. */
function messageFor(overrides: Record<string, unknown> = {}): ConversationMessage {
  const payload = conversationMessagePayload(overrides);

  return {
    id: payload.id,
    conversationId: payload.conversation_id,
    sequence: payload.sequence,
    role: payload.role as ConversationMessage["role"],
    content: payload.content,
    language: payload.language,
    citations: payload.citations.map((citation) =>
      citationFor(citation as Record<string, unknown>),
    ),
    suggestions: payload.suggestions,
    citationCount: payload.citation_count,
    documentCount: payload.document_count,
    grounded: payload.grounded,
    insufficientEvidence: payload.insufficient_evidence,
    truncated: payload.truncated,
    provider: payload.provider,
    model: payload.model,
    promptName: payload.prompt_name,
    promptVersion: payload.prompt_version,
    durationMs: payload.duration_ms,
    retrievalMs: payload.retrieval_ms,
    generationMs: payload.generation_ms,
    promptTokens: payload.prompt_tokens,
    completionTokens: payload.completion_tokens,
    totalTokens: payload.total_tokens,
    retrievedCount: payload.retrieved_count,
    contextCount: payload.context_count,
    contextTurns: payload.context_turns,
    topScore: payload.top_score,
    editedAt: payload.edited_at,
    createdAt: payload.created_at,
    feedback: null,
  };
}

// --------------------------------------------------------------------------- //
// Types and validation
// --------------------------------------------------------------------------- //

describe("assistant validation", () => {
  it("accepts a real question", () => {
    expect(messageFormSchema.safeParse({ content: "Quand le loyer est-il payable ?" }).success).toBe(
      true,
    );
  });

  it("rejects a question of punctuation", () => {
    // Retrieving on it returns arbitrary passages, and the model then writes a
    // confident paragraph out of them.
    expect(messageFormSchema.safeParse({ content: "???" }).success).toBe(false);
  });

  it("rejects an empty question", () => {
    expect(messageFormSchema.safeParse({ content: "   " }).success).toBe(false);
  });

  it("accepts an Arabic question", () => {
    expect(messageFormSchema.safeParse({ content: "متى يؤدى الكراء؟" }).success).toBe(true);
  });
});

describe("citationRelevancePercent", () => {
  it("renders a similarity as a percentage", () => {
    expect(citationRelevancePercent(0.8421)).toBe(84);
  });

  it("clamps a negative similarity to zero", () => {
    // A negative percentage reads as a data error rather than as "not relevant".
    expect(citationRelevancePercent(-0.4)).toBe(0);
  });
});

// --------------------------------------------------------------------------- //
// The API client
// --------------------------------------------------------------------------- //

describe("assistant API client", () => {
  it("maps a conversation from the wire format", async () => {
    mockFetch({ [ASSISTANT_ENDPOINTS.conversations]: { body: conversationDetailPayload() } });

    const conversation = await fetchConversation("66666666-6666-4666-8666-666666666666");

    expect(conversation.title).toBe("Quand le loyer est-il payable ?");
    expect(conversation.messages).toHaveLength(2);
    expect(conversation.messages[1]?.citations[0]?.documentName).toBe("bail-commercial.pdf");
  });

  it("sends the question in the body, never in the URL", async () => {
    // A question in a query string is written to the proxy's access log, the
    // browser's history, and the `Referer` header of anything loaded next.
    const { requests } = mockFetch({
      [ASSISTANT_ENDPOINTS.conversations]: { body: messageExchangePayload() },
    });

    await sendMessage("66666666-6666-4666-8666-666666666666", {
      content: "Quand le loyer est-il payable ?",
    });

    const request = requests.at(-1)!;
    expect(request.method).toBe("POST");
    expect(request.url).not.toContain("loyer");
    expect(request.body).toMatchObject({ content: "Quand le loyer est-il payable ?" });
  });

  it("omits the filter entirely when no case is pinned", async () => {
    // Sending `{}` would override the conversation's own case with "no filter at
    // all" and quietly widen the search.
    const { requests } = mockFetch({
      [ASSISTANT_ENDPOINTS.conversations]: { body: messageExchangePayload() },
    });

    await sendMessage("66666666-6666-4666-8666-666666666666", { content: "Une question ?" });

    expect(requests.at(-1)!.body).not.toHaveProperty("filters");
  });

  it("sends a pinned case as a retrieval filter", async () => {
    const { requests } = mockFetch({
      [ASSISTANT_ENDPOINTS.conversations]: { body: messageExchangePayload() },
    });

    await sendMessage("66666666-6666-4666-8666-666666666666", {
      content: "Une question ?",
      caseId: "22222222-2222-4222-8222-222222222222",
    });

    expect(requests.at(-1)!.body).toMatchObject({
      filters: { case_id: "22222222-2222-4222-8222-222222222222" },
    });
  });

  it("omits absent filters from the list query", () => {
    expect(buildConversationQuery({})).toBe("");
    expect(buildConversationQuery({ status: "archived" })).toBe("?status=archived");
  });

  it("maps the metrics from the wire format", async () => {
    mockFetch({ [ASSISTANT_ENDPOINTS.metrics]: { body: assistantMetricsPayload() } });

    const metrics = await fetchAssistantMetrics();

    expect(metrics.activeConversations).toBe(9);
    expect(metrics.helpfulRate).toBe(81.82);
  });
});

// --------------------------------------------------------------------------- //
// Citations
// --------------------------------------------------------------------------- //

describe("CitationList", () => {
  // The list is collapsed by default now, so most of these open it first. That
  // step is the assertion for the disclosure itself, and every other assertion
  // below is unchanged: what is reachable did not change, only what is first.
  async function open(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole("button", { name: /show sources/i }));
  }

  it("shows the complete reference for every source", async () => {
    // A generated statement with no provenance is unusable in a legal context.
    const user = userEvent.setup();
    render(<CitationList citations={[citationFor()]} />);
    await open(user);

    expect(screen.getAllByText("bail-commercial.pdf").length).toBeGreaterThan(0);
    expect(screen.getByText(/Page 4/)).toBeInTheDocument();
    expect(screen.getByText(/Version 1/)).toBeInTheDocument();
  });

  it("names the document in the collapsed summary", () => {
    // "2 sources" alone says nothing a reader can act on; the point of the
    // collapsed line is to be worth reading without expanding it.
    render(<CitationList citations={[citationFor()]} />);

    expect(screen.getByText(/1 source · bail-commercial\.pdf/)).toBeInTheDocument();
  });

  it("keeps the score off a cited source and on an uncited one", async () => {
    // Beside a cited source the cosine score answers a question nobody asked and
    // is misread as confidence in the answer. Among the passages the answer did
    // not cite it is the only reason they are on screen.
    const user = userEvent.setup();
    render(
      <CitationList
        citations={[citationFor({ marker: 1 }), citationFor({ marker: 2, referenced: false })]}
      />,
    );
    await open(user);

    expect(screen.queryByText("84% match")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /1 more passage retrieved/i }));
    expect(screen.getByText("84% match")).toBeInTheDocument();
  });

  it("keeps the marker the answer cites", async () => {
    // The `[2]` in the prose and the second entry are the same source, because
    // the pipeline assigned the marker before the model wrote a word.
    const user = userEvent.setup();
    render(<CitationList citations={[citationFor({ marker: 2 })]} />);
    await open(user);

    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("counts a source the answer did not cite rather than dropping it", async () => {
    // A model that forgot a marker has not made the evidence disappear. It moves
    // behind a disclosure; it is never removed, and the count is stated up front.
    const user = userEvent.setup();
    const citation = citationFor({ referenced: false });
    render(<CitationList citations={[citation]} />);

    expect(screen.getByText(/1 passage was retrieved, none cited/)).toBeInTheDocument();

    await open(user);
    await user.click(screen.getByRole("button", { name: /1 more passage retrieved/i }));
    expect(screen.getAllByText("bail-commercial.pdf").length).toBeGreaterThan(0);
  });

  it("counts distinct documents rather than citations", async () => {
    // Two passages of one contract are one source to a lawyer.
    render(
      <CitationList
        citations={[
          citationFor({ marker: 1, document_id: "aaaaaaaa-1111-4111-8111-111111111111" }),
          citationFor({
            marker: 2,
            page_number: 7,
            document_id: "bbbbbbbb-2222-4222-8222-222222222222",
          }),
        ]}
      />,
    );

    expect(screen.getByText(/2 sources from 2 documents$/)).toBeInTheDocument();
  });

  it("hides the excerpt until it is asked for", async () => {
    const user = userEvent.setup();
    const citation = citationFor();
    render(<CitationList citations={[citation]} />);
    await open(user);

    expect(screen.queryByText(citation.excerpt)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /show excerpt/i }));

    expect(screen.getByText(citation.excerpt)).toBeInTheDocument();
  });

  it("links to the case, the one destination the reader is certainly entitled to open", async () => {
    const user = userEvent.setup();
    render(<CitationList citations={[citationFor()]} />);
    await open(user);

    expect(screen.getByRole("link", { name: /open case/i })).toHaveAttribute(
      "href",
      `${ROUTES.cases}/22222222-2222-4222-8222-222222222222`,
    );
  });

  it("renders nothing when an answer has no sources", () => {
    const { container } = render(<CitationList citations={[]} />);

    expect(container).toBeEmptyDOMElement();
  });
});

// --------------------------------------------------------------------------- //
// Messages
// --------------------------------------------------------------------------- //

describe("ChatMessage", () => {
  it("shows the answer, and its sources once asked for", async () => {
    const user = userEvent.setup();
    renderWithQuery(
      <ChatMessage message={messageFor()} conversationId="66666666-6666-4666-8666-666666666666" />,
    );

    expect(
      screen.getByText(/Le loyer mensuel est payable d'avance le premier jour/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /show sources/i }));
    expect(screen.getAllByText("bail-commercial.pdf").length).toBeGreaterThan(0);
  });

  it("says plainly when retrieval found nothing at all", () => {
    // A reader must never mistake "I found nothing" for an answer that happens
    // to be short. `retrieved_count: 0` is the corpus case, and the only one
    // where telling the reader to check indexing is sound advice.
    renderWithQuery(
      <ChatMessage
        message={messageFor({
          grounded: false,
          insufficient_evidence: true,
          retrieved_count: 0,
          citations: [],
          citation_count: 0,
          document_count: 0,
          content: "Je n'ai trouvé aucun document justificatif.",
        })}
        conversationId="66666666-6666-4666-8666-666666666666"
      />,
    );

    expect(screen.getByText(/No supporting document was found/)).toBeInTheDocument();
  });

  it("distinguishes passages found but judged insufficient from nothing found", () => {
    // The two outcomes share `insufficient_evidence: true` and must not share a
    // sentence. Passages *were* retrieved here, so the documents are indexed and
    // searchable — advising the reader to check indexing would send them to
    // debug a problem they do not have, beside a passage count that contradicts
    // it. This is the case a counting question ("how many articles?") produces:
    // retrieval works, but no single passage states the answer.
    renderWithQuery(
      <ChatMessage
        message={messageFor({
          grounded: false,
          insufficient_evidence: true,
          retrieved_count: 8,
          citations: [],
          citation_count: 0,
          document_count: 0,
          content: "Je n'ai trouvé aucun document justificatif.",
        })}
        conversationId="66666666-6666-4666-8666-666666666666"
      />,
    );

    expect(screen.getByText(/none of the passages found answer this question/)).toBeInTheDocument();
    expect(screen.queryByText(/No supporting document was found/)).not.toBeInTheDocument();
  });

  it("warns that a truncated answer stops early", () => {
    // The one way a legal reader could be actively misled by this screen.
    renderWithQuery(
      <ChatMessage
        message={messageFor({ truncated: true })}
        conversationId="66666666-6666-4666-8666-666666666666"
      />,
    );

    expect(screen.getByText(/reached the length limit/)).toBeInTheDocument();
  });

  it("marks an answer that was read against an earlier question", () => {
    // It changes how the answer should be read, and is invisible from the
    // answer alone.
    renderWithQuery(
      <ChatMessage
        message={messageFor({ context_turns: 1 })}
        conversationId="66666666-6666-4666-8666-666666666666"
      />,
    );

    expect(screen.getByText("Follow-up")).toBeInTheDocument();
  });

  it("offers no rating on the user's own question", () => {
    renderWithQuery(
      <ChatMessage
        message={messageFor({ role: "user" })}
        conversationId="66666666-6666-4666-8666-666666666666"
      />,
    );

    expect(screen.queryByRole("button", { name: /helpful/i })).not.toBeInTheDocument();
  });

  it("offers a rating and a copy control on an answer", () => {
    renderWithQuery(
      <ChatMessage message={messageFor()} conversationId="66666666-6666-4666-8666-666666666666" />,
    );

    expect(screen.getByRole("button", { name: /rate this answer helpful/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy this answer/i })).toBeInTheDocument();
  });

  it("shows an existing rating as pressed", () => {
    renderWithQuery(
      <ChatMessage
        message={{
          ...messageFor(),
          feedback: {
            rating: "helpful",
            comment: null,
            createdAt: "2026-08-06T10:01:00Z",
            updatedAt: "2026-08-06T10:01:00Z",
          },
        }}
        conversationId="66666666-6666-4666-8666-666666666666"
      />,
    );

    expect(screen.getByRole("button", { name: /remove helpful rating/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

// --------------------------------------------------------------------------- //
// Composer
// --------------------------------------------------------------------------- //

describe("ChatComposer", () => {
  it("sends on submit", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<ChatComposer onSend={onSend} />);

    await user.type(screen.getByLabelText(/ask the assistant/i), "Quel est le loyer ?");
    await user.click(screen.getByRole("button", { name: /send question/i }));

    expect(onSend).toHaveBeenCalledWith("Quel est le loyer ?");
  });

  it("sends on Enter and breaks the line on Shift+Enter", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<ChatComposer onSend={onSend} />);

    const box = screen.getByLabelText(/ask the assistant/i);
    await user.type(box, "Quel est le loyer ?");
    await user.type(box, "{Shift>}{Enter}{/Shift}");
    expect(onSend).not.toHaveBeenCalled();

    await user.type(box, "{Enter}");
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("refuses a question of punctuation without a round trip", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<ChatComposer onSend={onSend} />);

    await user.type(screen.getByLabelText(/ask the assistant/i), "???{Enter}");

    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("keeps the box usable while an answer is in flight", () => {
    // Taking the keyboard away mid-thought is the most annoying thing a chat
    // interface can do; only the send button is disabled.
    render(<ChatComposer onSend={vi.fn()} isSending />);

    expect(screen.getByLabelText(/ask the assistant/i)).not.toBeDisabled();
    expect(screen.getByRole("button", { name: /send question/i })).toBeDisabled();
  });

  it("disables the box for a caller who may not ask", () => {
    render(<ChatComposer onSend={vi.fn()} disabled />);

    expect(screen.getByLabelText(/ask the assistant/i)).toBeDisabled();
  });
});

// --------------------------------------------------------------------------- //
// Suggestions
// --------------------------------------------------------------------------- //

describe("FollowUpSuggestions", () => {
  it("fills the box rather than sending", async () => {
    // A suggestion is a starting point a professional may want to narrow, and
    // one click that silently spends a model call is not a shortcut.
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <FollowUpSuggestions suggestions={["Quelle est la durée du bail ?"]} onSelect={onSelect} />,
    );

    await user.click(screen.getByRole("button", { name: /quelle est la durée du bail/i }));

    expect(onSelect).toHaveBeenCalledWith("Quelle est la durée du bail ?");
  });

  it("renders nothing when there is nothing worth asking next", () => {
    // The same for an ungrounded answer, a deployment with suggestions off, and
    // a suggestion call that failed — all three mean the same thing to a reader.
    const { container } = render(<FollowUpSuggestions suggestions={[]} onSelect={vi.fn()} />);

    expect(container).toBeEmptyDOMElement();
  });
});

// --------------------------------------------------------------------------- //
// The workspace
// --------------------------------------------------------------------------- //

describe("AssistantWorkspace", () => {
  it("refuses a role without ai:chat", () => {
    // The court role holds no AI capability at all.
    signInAs("court");
    mockFetch({ [ASSISTANT_ENDPOINTS.conversations]: { body: conversationPagePayload([]) } });

    renderWithQuery(<AssistantWorkspace />);

    expect(screen.queryByLabelText(/ask the assistant/i)).not.toBeInTheDocument();
  });

  it("opens the most recent conversation on arrival", async () => {
    signInAs("lawyer");
    // The detail key is registered first because the scripted fetch matches on
    // the first key the URL contains, and every detail URL also contains the
    // list URL.
    mockFetch({
      [ASSISTANT_ENDPOINTS.conversation(CONVERSATION_ID)]: {
        body: conversationDetailPayload(),
      },
      [ASSISTANT_ENDPOINTS.conversations]: { body: conversationPagePayload() },
    });

    renderWithQuery(<AssistantWorkspace />);

    await waitFor(() => {
      expect(
        screen.getByText(/Le loyer mensuel est payable d'avance le premier jour/),
      ).toBeInTheDocument();
    });
  });

  it("invites a first question when a conversation is empty", async () => {
    signInAs("lawyer");
    mockFetch({
      [ASSISTANT_ENDPOINTS.conversation(CONVERSATION_ID)]: {
        body: conversationDetailPayload([], { message_count: 0 }),
      },
      [ASSISTANT_ENDPOINTS.conversations]: {
        body: conversationPagePayload([conversationPayload({ message_count: 0 })]),
      },
    });

    renderWithQuery(<AssistantWorkspace />);

    await waitFor(() => {
      expect(screen.getByText(/Ask your first question/)).toBeInTheDocument();
    });
  });

  it("shows an empty state when there are no conversations at all", async () => {
    signInAs("lawyer");
    mockFetch({ [ASSISTANT_ENDPOINTS.conversations]: { body: conversationPagePayload([]) } });

    renderWithQuery(<AssistantWorkspace />);

    await waitFor(() => {
      expect(screen.getByText(/No conversations yet/)).toBeInTheDocument();
    });
  });

  it("tells a reader who cannot ask that they can only read", async () => {
    signInAs("lawyer");
    act(() => {
      useSessionStore.setState({
        user: {
          ...sessionUserWithRole("lawyer"),
          permissions: sessionUserWithRole("lawyer").permissions.filter(
            (permission) => permission !== "ai:ask",
          ),
        },
        status: "authenticated",
      });
    });
    mockFetch({ [ASSISTANT_ENDPOINTS.conversations]: { body: conversationPagePayload([]) } });

    renderWithQuery(<AssistantWorkspace />);

    await waitFor(() => {
      expect(screen.getByText(/does not allow asking the AI assistant new questions/)).toBeInTheDocument();
    });
  });
});

// --------------------------------------------------------------------------- //
// Monitoring
// --------------------------------------------------------------------------- //

describe("AssistantMetricsPanel", () => {
  it("shows the figures the spec names", async () => {
    signInAs("administrator");
    mockFetch({ [ASSISTANT_ENDPOINTS.metrics]: { body: assistantMetricsPayload() } });

    renderWithQuery(<AssistantMetricsPanel />);

    await waitFor(() => {
      expect(screen.getByText("Active conversations")).toBeInTheDocument();
    });
    expect(screen.getByText("9")).toBeInTheDocument();
    expect(screen.getByText("5.67 messages")).toBeInTheDocument();
    expect(screen.getByText("2.8s")).toBeInTheDocument();
  });

  it("renders nothing for a caller without ai:monitor", () => {
    // The panel has no useful "you cannot see this" state, and requesting it
    // would put a 403 in the console on every page load.
    signInAs("lawyer");
    const { requests } = mockFetch({
      [ASSISTANT_ENDPOINTS.metrics]: { body: assistantMetricsPayload() },
    });

    const { container } = renderWithQuery(<AssistantMetricsPanel />);

    expect(container).toBeEmptyDOMElement();
    expect(requests).toHaveLength(0);
  });

  it("says plainly when the assistant is disabled", async () => {
    signInAs("administrator");
    mockFetch({
      [ASSISTANT_ENDPOINTS.metrics]: { body: assistantMetricsPayload({ enabled: false }) },
    });

    renderWithQuery(<AssistantMetricsPanel />);

    await waitFor(() => {
      expect(screen.getByText(/assistant is disabled on this deployment/)).toBeInTheDocument();
    });
  });

  it("shows a dash rather than zero when nobody has rated anything", async () => {
    // `0%` would read as "every answer was unhelpful".
    signInAs("administrator");
    mockFetch({
      [ASSISTANT_ENDPOINTS.metrics]: {
        body: assistantMetricsPayload({ helpful_rate: null, total_feedback: 0 }),
      },
    });

    renderWithQuery(<AssistantMetricsPanel />);

    await waitFor(() => {
      expect(screen.getByText("Rated helpful")).toBeInTheDocument();
    });
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("says that the request counters reset with the process", async () => {
    // Otherwise the figures quietly mean less than they appear to.
    signInAs("administrator");
    mockFetch({ [ASSISTANT_ENDPOINTS.metrics]: { body: assistantMetricsPayload() } });

    renderWithQuery(<AssistantMetricsPanel />);

    await waitFor(() => {
      expect(screen.getByText(/reset when it restarts/)).toBeInTheDocument();
    });
  });

  it("stays silent when the metrics cannot be loaded", async () => {
    signInAs("administrator");
    mockFetch({
      [ASSISTANT_ENDPOINTS.metrics]: { status: 503, body: errorEnvelope("service_unavailable") },
    });

    const { container } = renderWithQuery(<AssistantMetricsPanel />);

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });
});
