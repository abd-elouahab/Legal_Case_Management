/**
 * AI Legal Assistant API calls.
 *
 * Thin, typed wrappers over the `/assistant` endpoints. Every response is
 * validated with Zod and mapped from the API's snake_case wire format to the
 * app's camelCase domain types, so nothing above this layer sees a wire shape —
 * and a backend change that alters a payload fails here, loudly, instead of
 * surfacing as `undefined` in a chat bubble.
 *
 * **Conversations only.** There is no call here that retrieves a passage, renders
 * a prompt, or asks a model for anything: the API exposes none under this prefix,
 * because every answer is produced by the RAG pipeline behind the message
 * endpoint.
 *
 * The one thing that is genuinely different from every other client module is
 * {@link streamMessage}, which reads a Server-Sent Events body rather than a JSON
 * one — see its own note for why it cannot go through {@link apiRequest}.
 */

import { apiRequest } from "@/lib/api/client";
import { ASSISTANT_ENDPOINTS, apiUrl } from "@/lib/api/config";
import { NetworkError, toApiError } from "@/lib/api/errors";
import { getAccessToken } from "@/lib/api/token-store";
import {
  assistantMetricsSchema,
  conversationDetailSchema,
  conversationMessagePageSchema,
  conversationMessageSchema,
  conversationPageSchema,
  conversationSchema,
  messageExchangeSchema,
  messageFeedbackSchema,
  streamDeltaSchema,
  streamErrorSchema,
  streamFinalSchema,
  streamRetrievalSchema,
} from "@/lib/validation/assistant";
import type {
  AssistantCitation,
  AssistantMetrics,
  AssistantStreamEvent,
  Conversation,
  ConversationCreateInput,
  ConversationDetail,
  ConversationListParams,
  ConversationMessage,
  ConversationMessagePage,
  ConversationPage,
  ConversationUpdateInput,
  FeedbackRating,
  MessageExchange,
  MessageFeedback,
  MessageInput,
} from "@/types/assistant";

type CitationWire = ReturnType<typeof streamFinalSchema.parse>["citations"][number];
type MessageWire = ReturnType<typeof conversationMessageSchema.parse>;
type ConversationWire = ReturnType<typeof conversationSchema.parse>;
type DetailWire = ReturnType<typeof conversationDetailSchema.parse>;
type MetricsWire = ReturnType<typeof assistantMetricsSchema.parse>;
type FeedbackWire = NonNullable<MessageWire["feedback"]>;

// --------------------------------------------------------------------------- //
// Wire → domain
// --------------------------------------------------------------------------- //

function toCitation(payload: CitationWire): AssistantCitation {
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

function toFeedback(payload: FeedbackWire | null): MessageFeedback | null {
  if (!payload) return null;
  return {
    rating: payload.rating,
    comment: payload.comment,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

function toMessage(payload: MessageWire): ConversationMessage {
  return {
    id: payload.id,
    conversationId: payload.conversation_id,
    sequence: payload.sequence,
    role: payload.role,
    content: payload.content,
    language: payload.language,

    citations: payload.citations.map(toCitation),
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
    feedback: toFeedback(payload.feedback),
  };
}

function toConversation(payload: ConversationWire): Conversation {
  return {
    id: payload.id,
    title: payload.title,
    titleIsCustom: payload.title_is_custom,
    status: payload.status,
    language: payload.language,
    caseId: payload.case_id,

    messageCount: payload.message_count,
    lastMessageAt: payload.last_message_at,
    lastMessagePreview: payload.last_message_preview,

    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

function toDetail(payload: DetailWire): ConversationDetail {
  return {
    ...toConversation(payload),
    messages: payload.messages.map(toMessage),
    hasMoreMessages: payload.has_more_messages,
  };
}

function toMetrics(payload: MetricsWire): AssistantMetrics {
  return {
    since: payload.since,

    totalConversations: payload.total_conversations,
    activeConversations: payload.active_conversations,
    archivedConversations: payload.archived_conversations,
    totalMessages: payload.total_messages,
    averageConversationLength: payload.average_conversation_length,

    totalRequests: payload.total_requests,
    successfulRequests: payload.successful_requests,
    failedRequests: payload.failed_requests,
    successRate: payload.success_rate,
    failureRate: payload.failure_rate,
    streamedRequests: payload.streamed_requests,

    averageResponseMs: payload.average_response_ms,
    averageResponseSeconds: payload.average_response_seconds,

    groundedAnswers: payload.grounded_answers,
    insufficientEvidence: payload.insufficient_evidence,
    groundingRate: payload.grounding_rate,

    totalFeedback: payload.total_feedback,
    helpfulFeedback: payload.helpful_feedback,
    notHelpfulFeedback: payload.not_helpful_feedback,
    helpfulRate: payload.helpful_rate,
    ratedMessagesRate: payload.rated_messages_rate,

    failuresByCode: payload.failures_by_code,

    suggestionsEnabled: payload.suggestions_enabled,
    streamingEnabled: payload.streaming_enabled,
    enabled: payload.enabled,
  };
}

// --------------------------------------------------------------------------- //
// Conversations
// --------------------------------------------------------------------------- //

/**
 * Build the query string for the conversation list.
 *
 * "Any" filters are omitted rather than sent as empty values, so the URL
 * reflects what is actually being asked — which also keeps it a stable cache key
 * for TanStack Query.
 */
export function buildConversationQuery(params: ConversationListParams): string {
  const query = new URLSearchParams();

  if (params.page) query.set("page", String(params.page));
  if (params.pageSize) query.set("page_size", String(params.pageSize));
  if (params.status) query.set("status", params.status);
  if (params.caseId) query.set("case_id", params.caseId);
  if (params.search) query.set("search", params.search);

  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

/** List the caller's own conversations. */
export async function fetchConversations(
  params: ConversationListParams = {},
): Promise<ConversationPage> {
  const raw = await apiRequest<unknown>(
    `${ASSISTANT_ENDPOINTS.conversations}${buildConversationQuery(params)}`,
  );
  const parsed = conversationPageSchema.parse(raw);

  return {
    items: parsed.items.map(toConversation),
    totalRecords: parsed.total_records,
    page: parsed.page,
    pageSize: parsed.page_size,
    totalPages: parsed.total_pages,
  };
}

/** Open one conversation together with the first page of its transcript. */
export async function fetchConversation(id: string): Promise<ConversationDetail> {
  const raw = await apiRequest<unknown>(ASSISTANT_ENDPOINTS.conversation(id));
  return toDetail(conversationDetailSchema.parse(raw));
}

/** Start a new conversation, optionally asking its first question. */
export async function createConversation(
  input: ConversationCreateInput = {},
): Promise<ConversationDetail> {
  const raw = await apiRequest<unknown>(ASSISTANT_ENDPOINTS.conversations, {
    method: "POST",
    body: {
      ...(input.title ? { title: input.title } : {}),
      ...(input.language ? { language: input.language } : {}),
      ...(input.caseId ? { case_id: input.caseId } : {}),
      ...(input.firstMessage ? { first_message: input.firstMessage } : {}),
    },
  });
  return toDetail(conversationDetailSchema.parse(raw));
}

/**
 * Rename a conversation, archive it, or restore it.
 *
 * Only the keys present are sent, because the API treats an omitted field as
 * "leave it alone": archiving must not resend — and therefore must not be able to
 * clobber — a title it is not changing.
 */
export async function updateConversation(
  id: string,
  input: ConversationUpdateInput,
): Promise<Conversation> {
  const raw = await apiRequest<unknown>(ASSISTANT_ENDPOINTS.conversation(id), {
    method: "PATCH",
    body: {
      ...(input.title !== undefined ? { title: input.title } : {}),
      ...(input.status !== undefined ? { status: input.status } : {}),
    },
  });
  return toConversation(conversationSchema.parse(raw));
}

/** Withdraw a conversation. The transcript is kept server-side, not destroyed. */
export async function deleteConversation(id: string): Promise<void> {
  await apiRequest<void>(ASSISTANT_ENDPOINTS.conversation(id), { method: "DELETE" });
}

// --------------------------------------------------------------------------- //
// Messages
// --------------------------------------------------------------------------- //

function messageBody(input: MessageInput): Record<string, unknown> {
  return {
    content: input.content,
    ...(input.language ? { language: input.language } : {}),
    ...(input.topK ? { top_k: input.topK } : {}),
    ...(input.minScore !== null && input.minScore !== undefined
      ? { min_score: input.minScore }
      : {}),
    // Omitted entirely rather than sent as an empty object, so the API applies
    // the conversation's own case as the default filter — sending `{}` would
    // override that with "no filter at all" and quietly widen the search.
    ...(input.caseId ? { filters: { case_id: input.caseId } } : {}),
  };
}

/** Read one page of a conversation's transcript, oldest first. */
export async function fetchMessages(
  conversationId: string,
  params: { page?: number; pageSize?: number } = {},
): Promise<ConversationMessagePage> {
  const query = new URLSearchParams();
  if (params.page) query.set("page", String(params.page));
  if (params.pageSize) query.set("page_size", String(params.pageSize));

  const encoded = query.toString();
  const raw = await apiRequest<unknown>(
    `${ASSISTANT_ENDPOINTS.messages(conversationId)}${encoded ? `?${encoded}` : ""}`,
  );
  const parsed = conversationMessagePageSchema.parse(raw);

  return {
    items: parsed.items.map(toMessage),
    totalRecords: parsed.total_records,
    page: parsed.page,
    pageSize: parsed.page_size,
    totalPages: parsed.total_pages,
  };
}

/** Send one message and wait for the whole answer. */
export async function sendMessage(
  conversationId: string,
  input: MessageInput,
): Promise<MessageExchange> {
  const raw = await apiRequest<unknown>(ASSISTANT_ENDPOINTS.messages(conversationId), {
    method: "POST",
    body: messageBody(input),
  });
  const parsed = messageExchangeSchema.parse(raw);

  return {
    conversation: toConversation(parsed.conversation),
    userMessage: toMessage(parsed.user_message),
    assistantMessage: toMessage(parsed.assistant_message),
  };
}

/**
 * Send one message and yield the answer as it arrives.
 *
 * **Why this does not go through {@link apiRequest}.** That helper reads the
 * whole body and parses it as JSON, which is exactly what must not happen here —
 * the point of the endpoint is that the body is still being written. So this
 * calls `fetch` directly and reads the stream, and pays for it by repeating three
 * things the shared client does: attaching the Bearer token, sending the refresh
 * cookie, and normalizing a failure into an {@link ApiError}.
 *
 * **A 401 is not retried here**, deliberately, and that is the one behaviour that
 * differs from every other call. The shared client refreshes and replays a
 * request whose token expired; replaying a *message* would ask the same question
 * twice, which costs a second model call and appends a second turn to the
 * transcript. The composer surfaces the failure instead, and the user presses
 * send again — by which time the token store has been refreshed by any other
 * request on the page.
 *
 * Frames are parsed incrementally against a buffer, because a `delta` can be
 * split across two network chunks: SSE separates events with a blank line, and
 * reading one chunk at a time without buffering would silently drop the tail of
 * every answer that did not arrive whole.
 */
export async function* streamMessage(
  conversationId: string,
  input: MessageInput,
  options: { signal?: AbortSignal } = {},
): AsyncGenerator<AssistantStreamEvent> {
  const token = getAccessToken();

  let response: Response;
  try {
    response = await fetch(apiUrl(ASSISTANT_ENDPOINTS.stream(conversationId)), {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      credentials: "include",
      body: JSON.stringify(messageBody(input)),
      ...(options.signal ? { signal: options.signal } : {}),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new NetworkError();
  }

  // Every request-rejection the API can make — 403, 404, 409, 422, 503 — arrives
  // before the stream begins, because the server primes the pipeline through
  // retrieval before it sends the status line. So a non-2xx here is a normal
  // error with a normal body.
  if (!response.ok) throw await toApiError(response);

  const body = response.body;
  if (!body) throw new NetworkError();

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);

        const event = parseFrame(frame);
        if (event) yield event;

        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    // Releasing the lock is what lets an aborted stream be garbage-collected
    // rather than holding the connection until the process notices.
    reader.releaseLock();
  }
}

/**
 * Parse one SSE frame into a typed event, or `null` when it is not one.
 *
 * Unknown event names are ignored rather than thrown on, which is the whole
 * point of a named-event protocol: a future server may emit a `progress` frame,
 * and a client that fell over on it would break on an upgrade that was designed
 * to be safe.
 */
function parseFrame(frame: string): AssistantStreamEvent | null {
  let name = "";
  let data = "";

  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }

  if (!name || !data) return null;

  let payload: unknown;
  try {
    payload = JSON.parse(data);
  } catch {
    return null;
  }

  switch (name) {
    case "retrieval": {
      const parsed = streamRetrievalSchema.safeParse(payload);
      return parsed.success
        ? { kind: "retrieval", retrievedCount: parsed.data.retrieved_count }
        : null;
    }
    case "delta": {
      const parsed = streamDeltaSchema.safeParse(payload);
      return parsed.success ? { kind: "delta", text: parsed.data.text } : null;
    }
    case "final": {
      const parsed = streamFinalSchema.safeParse(payload);
      if (!parsed.success) return null;
      return {
        kind: "final",
        answer: parsed.data.answer,
        language: parsed.data.language,
        grounded: parsed.data.grounded,
        insufficientEvidence: parsed.data.insufficient_evidence,
        truncated: parsed.data.truncated,
        citations: parsed.data.citations.map(toCitation),
        retrievedCount: parsed.data.retrieved_count,
        contextCount: parsed.data.context_count,
        durationMs: parsed.data.duration_ms,
        retrievalMs: parsed.data.retrieval_ms,
        generationMs: parsed.data.generation_ms,
      };
    }
    case "error": {
      const parsed = streamErrorSchema.safeParse(payload);
      return parsed.success
        ? { kind: "error", error: parsed.data.error, message: parsed.data.message }
        : null;
    }
    default:
      return null;
  }
}

// --------------------------------------------------------------------------- //
// Feedback
// --------------------------------------------------------------------------- //

/** Rate one answer, or change an existing rating. */
export async function submitFeedback(
  conversationId: string,
  messageId: string,
  rating: FeedbackRating,
  comment?: string,
): Promise<MessageFeedback> {
  const raw = await apiRequest<unknown>(
    ASSISTANT_ENDPOINTS.feedback(conversationId, messageId),
    {
      method: "PUT",
      body: { rating, ...(comment ? { comment } : {}) },
    },
  );
  const parsed = messageFeedbackSchema.parse(raw);
  return {
    rating: parsed.rating,
    comment: parsed.comment,
    createdAt: parsed.created_at,
    updatedAt: parsed.updated_at,
  };
}

/** Withdraw a rating. Idempotent — removing one that is not there succeeds. */
export async function withdrawFeedback(
  conversationId: string,
  messageId: string,
): Promise<void> {
  await apiRequest<void>(ASSISTANT_ENDPOINTS.feedback(conversationId, messageId), {
    method: "DELETE",
  });
}

// --------------------------------------------------------------------------- //
// Monitoring
// --------------------------------------------------------------------------- //

/** Fetch platform-wide assistant health. Requires `ai:monitor`. */
export async function fetchAssistantMetrics(): Promise<AssistantMetrics> {
  const raw = await apiRequest<unknown>(ASSISTANT_ENDPOINTS.metrics);
  return toMetrics(assistantMetricsSchema.parse(raw));
}
