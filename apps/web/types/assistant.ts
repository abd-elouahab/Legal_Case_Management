/**
 * AI Legal Assistant types.
 *
 * Mirror the API's conversation payloads (`apps/api/schemas/conversation.py`).
 * Union types rather than magic strings, per the code standards: referencing a
 * role, a status, or a rating the platform does not define is a compile error.
 *
 * **A citation is the RAG pipeline's shape, reused verbatim.** The API returns
 * the pipeline's own citation objects, unmodified — the spec requires the
 * assistant to *display citations without modifying them* — so declaring a
 * parallel type here would be a second vocabulary to keep in step, and the first
 * change touching only one of them would produce a citation this client renders
 * and the API never sends.
 *
 * **Nothing here describes a prompt, a passage, a vector, or a retrieval
 * strategy.** The assistant asks a question and renders an answer; how that
 * answer was built is the pipeline's business, and a field for it here is how
 * that boundary would quietly move.
 */

/** Who wrote a message. */
export const CONVERSATION_ROLES = ["user", "assistant"] as const;
export type ConversationRole = (typeof CONVERSATION_ROLES)[number];

/** Whether a conversation is in the working set. */
export const CONVERSATION_STATUSES = ["active", "archived"] as const;
export type ConversationStatus = (typeof CONVERSATION_STATUSES)[number];

/** How a user rated an answer. Exactly the two the spec requires at minimum. */
export const FEEDBACK_RATINGS = ["helpful", "not_helpful"] as const;
export type FeedbackRating = (typeof FEEDBACK_RATINGS)[number];

/**
 * One source an answer was grounded in.
 *
 * The four references the pipeline guarantees — document, version, page, case —
 * plus the marker the prose cites it by, the excerpt the model actually read, and
 * whether the answer referenced it. Deliberately no chunk number, no point
 * identifier, and no embedding model: none of them means anything to a lawyer.
 */
export interface AssistantCitation {
  marker: number;
  documentId: string;
  documentName: string;
  documentVersion: number;
  pageNumber: number;
  caseId: string;
  score: number;
  /** The passage as it was placed in the prompt — clipped, when the budget clipped it. */
  excerpt: string;
  excerptTruncated: boolean;
  /**
   * Whether the answer's prose actually cites this source. Sources the model was
   * given but did not use are still returned, so the list stays complete — and
   * marked, so it stays honest about which references the sentence rests on.
   */
  referenced: boolean;
}

/** A rating left on one answer. */
export interface MessageFeedback {
  rating: FeedbackRating;
  comment: string | null;
  createdAt: string;
  updatedAt: string;
}

/**
 * One turn of a conversation.
 *
 * A user message carries content and little else; an assistant message carries
 * the answer, its citations, its suggested follow-ups, and the provenance an
 * evaluation needs. One type for both because they are one list to render, and a
 * second type would make drawing a transcript a type test per row.
 */
export interface ConversationMessage {
  id: string;
  conversationId: string;
  /** Position in the thread, 1-based and contiguous. */
  sequence: number;
  role: ConversationRole;
  content: string;
  /** ISO 639-1 code, or `null` on a message whose language was never settled. */
  language: string | null;

  citations: AssistantCitation[];
  suggestions: string[];
  citationCount: number;
  /** Distinct documents cited — what a "sources: 3 documents" line reads. */
  documentCount: number;

  /**
   * Whether the answer was built from retrieved passages. `false` means the
   * assistant declined — never that it answered from the model's own knowledge.
   */
  grounded: boolean | null;
  insufficientEvidence: boolean | null;
  /** Whether the answer stops at the model's output ceiling, mid-thought. */
  truncated: boolean;

  provider: string | null;
  model: string | null;
  promptName: string | null;
  promptVersion: number | null;

  durationMs: number | null;
  retrievalMs: number | null;
  generationMs: number | null;
  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;

  retrievedCount: number | null;
  contextCount: number | null;
  /** Earlier questions carried in to resolve a follow-up. `0` means it stood alone. */
  contextTurns: number;
  topScore: number | null;

  editedAt: string | null;
  createdAt: string;
  feedback: MessageFeedback | null;
}

/** One conversation as a list row. */
export interface Conversation {
  id: string;
  title: string;
  titleIsCustom: boolean;
  status: ConversationStatus;
  language: string | null;
  caseId: string | null;

  messageCount: number;
  lastMessageAt: string | null;
  lastMessagePreview: string | null;

  createdAt: string;
  updatedAt: string;
}

/** A conversation together with the first page of its transcript. */
export interface ConversationDetail extends Conversation {
  messages: ConversationMessage[];
  hasMoreMessages: boolean;
}

/** One page of the conversation list. */
export interface ConversationPage {
  items: Conversation[];
  totalRecords: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

/** One page of a transcript. */
export interface ConversationMessagePage {
  items: ConversationMessage[];
  totalRecords: number;
  page: number;
  pageSize: number;
  totalPages: number;
}

/** What one completed exchange returns. */
export interface MessageExchange {
  conversation: Conversation;
  userMessage: ConversationMessage;
  assistantMessage: ConversationMessage;
}

/** What the conversation list asks for. */
export interface ConversationListParams {
  page?: number;
  pageSize?: number;
  status?: ConversationStatus | null;
  caseId?: string | null;
  search?: string | null;
}

/** What opening a conversation asks for. */
export interface ConversationCreateInput {
  title?: string | null;
  language?: string | null;
  caseId?: string | null;
  firstMessage?: string | null;
}

/** What renaming or archiving a conversation asks for. */
export interface ConversationUpdateInput {
  title?: string;
  status?: ConversationStatus;
}

/** What sending a message asks for. */
export interface MessageInput {
  content: string;
  language?: string | null;
  topK?: number | null;
  minScore?: number | null;
  caseId?: string | null;
}

/**
 * Events a streamed answer delivers, in the order they arrive.
 *
 * `retrieval` once, `delta` any number of times (**zero** when nothing was
 * retrieved, because no model is called at all), then `final` — or `error` when
 * generation fails after the response has already begun.
 *
 * **`final` is authoritative.** A citation marker pointing at a source that was
 * never supplied is removed from it, and a model that declined has its internal
 * token replaced by the platform's own sentence, so the accumulated deltas are a
 * progress indicator that happens to be readable rather than the answer.
 */
export type AssistantStreamEvent =
  | { kind: "retrieval"; retrievedCount: number }
  | { kind: "delta"; text: string }
  | {
      kind: "final";
      answer: string;
      language: string;
      grounded: boolean;
      insufficientEvidence: boolean;
      truncated: boolean;
      citations: AssistantCitation[];
      retrievedCount: number;
      contextCount: number;
      durationMs: number;
      retrievalMs: number;
      generationMs: number | null;
    }
  | { kind: "error"; error: string; message: string };

/** Platform-wide assistant health. Requires `ai:monitor`. */
export interface AssistantMetrics {
  /**
   * When the *request* counting window began. Not decoration: those counters
   * live in the API process, so they reset on restart and each instance counts
   * only its own traffic. The conversation and feedback figures beside them are
   * read from the database and cover everything.
   */
  since: string;

  totalConversations: number;
  activeConversations: number;
  archivedConversations: number;
  totalMessages: number;
  averageConversationLength: number | null;

  totalRequests: number;
  successfulRequests: number;
  failedRequests: number;
  successRate: number;
  failureRate: number;
  streamedRequests: number;

  averageResponseMs: number | null;
  averageResponseSeconds: number | null;

  groundedAnswers: number;
  insufficientEvidence: number;
  groundingRate: number;

  totalFeedback: number;
  helpfulFeedback: number;
  notHelpfulFeedback: number;
  helpfulRate: number | null;
  ratedMessagesRate: number | null;

  failuresByCode: Record<string, number>;

  suggestionsEnabled: boolean;
  streamingEnabled: boolean;
  enabled: boolean;
}

/**
 * A relevance score as a percentage, for display.
 *
 * Cosine similarity is `[-1, 1]`, and a negative score means the passage points
 * away from the question — clamped to 0 rather than shown as a negative
 * percentage, which reads as a data error rather than as "not very relevant".
 * The same rule `relevancePercent` applies in the search types, kept here rather
 * than imported so this module does not depend on that feature.
 */
export function citationRelevancePercent(score: number): number {
  return Math.round(Math.max(0, Math.min(1, score)) * 100);
}

/** A readable label for a rating. */
export const FEEDBACK_LABELS: Record<FeedbackRating, string> = {
  helpful: "Helpful",
  not_helpful: "Not helpful",
};
