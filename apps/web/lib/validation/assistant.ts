/**
 * Zod schemas for the AI Legal Assistant.
 *
 * Response validation, plus the two *forms* this feature has — the message
 * composer and the rename dialog. API responses are external input, so they are
 * parsed before entering application state (per the code standards). The rules
 * mirror `apps/api/schemas/conversation.py`; where they must agree, the API is
 * the authority.
 *
 * The client-side rules exist to spare a round trip and to put the message next
 * to the field, **not** as a security boundary: the API validates the same
 * request independently, and a caller bypassing this reaches exactly the same
 * refusal.
 */

import { z } from "zod";

import {
  CONVERSATION_ROLES,
  CONVERSATION_STATUSES,
  FEEDBACK_RATINGS,
} from "@/types/assistant";

/** Shortest question worth sending, matching `MIN_QUESTION_LENGTH` on the server. */
export const MIN_MESSAGE_LENGTH = 2;

/** Longest question the API accepts, matching `RAG_QUESTION_MAX_LENGTH`. */
export const MAX_MESSAGE_LENGTH = 1000;

/** Longest conversation title, matching `ASSISTANT_TITLE_MAX_LENGTH`. */
export const MAX_TITLE_LENGTH = 120;

/** Longest note on a rating, matching `MAX_FEEDBACK_COMMENT_LENGTH`. */
export const MAX_FEEDBACK_COMMENT_LENGTH = 1000;

/**
 * The message composer.
 *
 * A message of punctuation is rejected here for the same reason it is on the
 * server: retrieving on it returns arbitrary passages, and the model then writes
 * a confident paragraph out of them — which is the fabrication this whole
 * pipeline exists to prevent.
 */
export const messageFormSchema = z.object({
  content: z
    .string()
    .trim()
    .min(MIN_MESSAGE_LENGTH, `Enter at least ${MIN_MESSAGE_LENGTH} characters.`)
    .max(MAX_MESSAGE_LENGTH, `Questions are limited to ${MAX_MESSAGE_LENGTH} characters.`)
    .refine(
      (value) => /[\p{L}\p{N}]/u.test(value),
      "Enter a word or a number to ask about.",
    ),
});

export type MessageFormValues = z.infer<typeof messageFormSchema>;

/** The rename dialog. */
export const conversationTitleFormSchema = z.object({
  title: z
    .string()
    .trim()
    .min(1, "Enter a name for this conversation.")
    .max(MAX_TITLE_LENGTH, `Names are limited to ${MAX_TITLE_LENGTH} characters.`),
});

export type ConversationTitleFormValues = z.infer<typeof conversationTitleFormSchema>;

/** The optional note on a rating. */
export const feedbackFormSchema = z.object({
  rating: z.enum(FEEDBACK_RATINGS),
  comment: z
    .string()
    .trim()
    .max(MAX_FEEDBACK_COMMENT_LENGTH, `Notes are limited to ${MAX_FEEDBACK_COMMENT_LENGTH} characters.`)
    .optional(),
});

export type FeedbackFormValues = z.infer<typeof feedbackFormSchema>;

// --------------------------------------------------------------------------- //
// Responses
// --------------------------------------------------------------------------- //

/**
 * One citation, in the pipeline's own wire shape.
 *
 * `document_name` is `.catch("")` rather than required for a reason worth
 * stating: the API falls back to the document identifier when a filename cannot
 * be resolved, and a citation whose label failed to render must still show the
 * page and the case it points at. A parse failure here would discard an entire
 * answer over a missing label.
 */
export const assistantCitationSchema = z.object({
  marker: z.number(),
  document_id: z.string(),
  document_name: z.string().catch(""),
  document_version: z.number(),
  page_number: z.number(),
  case_id: z.string(),
  score: z.number(),
  excerpt: z.string(),
  excerpt_truncated: z.boolean().default(false),
  referenced: z.boolean().default(false),
});

export const messageFeedbackSchema = z.object({
  rating: z.enum(FEEDBACK_RATINGS),
  comment: z.string().nullable().default(null),
  created_at: z.string(),
  updated_at: z.string(),
});

/**
 * One message.
 *
 * `language` is a tolerant string rather than an enum, matching the search
 * types: the label is settled per message by a heuristic on the server, and a
 * future backend may report a code this build has never heard of. A message must
 * still render.
 */
export const conversationMessageSchema = z.object({
  id: z.string(),
  conversation_id: z.string(),
  sequence: z.number(),
  role: z.enum(CONVERSATION_ROLES),
  content: z.string(),
  language: z.string().nullable().default(null),

  citations: z.array(assistantCitationSchema).default([]),
  suggestions: z.array(z.string()).default([]),
  citation_count: z.number().default(0),
  document_count: z.number().default(0),

  grounded: z.boolean().nullable().default(null),
  insufficient_evidence: z.boolean().nullable().default(null),
  truncated: z.boolean().default(false),

  provider: z.string().nullable().default(null),
  model: z.string().nullable().default(null),
  prompt_name: z.string().nullable().default(null),
  prompt_version: z.number().nullable().default(null),

  duration_ms: z.number().nullable().default(null),
  retrieval_ms: z.number().nullable().default(null),
  generation_ms: z.number().nullable().default(null),
  prompt_tokens: z.number().nullable().default(null),
  completion_tokens: z.number().nullable().default(null),
  total_tokens: z.number().nullable().default(null),

  retrieved_count: z.number().nullable().default(null),
  context_count: z.number().nullable().default(null),
  context_turns: z.number().default(0),
  top_score: z.number().nullable().default(null),

  edited_at: z.string().nullable().default(null),
  created_at: z.string(),
  feedback: messageFeedbackSchema.nullable().default(null),
});

export const conversationSchema = z.object({
  id: z.string(),
  title: z.string(),
  title_is_custom: z.boolean().default(false),
  status: z.enum(CONVERSATION_STATUSES),
  language: z.string().nullable().default(null),
  case_id: z.string().nullable().default(null),

  message_count: z.number(),
  last_message_at: z.string().nullable().default(null),
  last_message_preview: z.string().nullable().default(null),

  created_at: z.string(),
  updated_at: z.string(),
});

export const conversationDetailSchema = conversationSchema.extend({
  messages: z.array(conversationMessageSchema).default([]),
  has_more_messages: z.boolean().default(false),
});

export const conversationPageSchema = z.object({
  items: z.array(conversationSchema),
  total_records: z.number(),
  page: z.number(),
  page_size: z.number(),
  total_pages: z.number(),
});

export const conversationMessagePageSchema = z.object({
  items: z.array(conversationMessageSchema),
  total_records: z.number(),
  page: z.number(),
  page_size: z.number(),
  total_pages: z.number(),
});

export const messageExchangeSchema = z.object({
  conversation: conversationSchema,
  user_message: conversationMessageSchema,
  assistant_message: conversationMessageSchema,
});

export const assistantMetricsSchema = z.object({
  since: z.string(),

  total_conversations: z.number(),
  active_conversations: z.number(),
  archived_conversations: z.number(),
  total_messages: z.number(),
  average_conversation_length: z.number().nullable().default(null),

  total_requests: z.number(),
  successful_requests: z.number(),
  failed_requests: z.number(),
  success_rate: z.number(),
  failure_rate: z.number(),
  streamed_requests: z.number().default(0),

  average_response_ms: z.number().nullable().default(null),
  average_response_seconds: z.number().nullable().default(null),

  grounded_answers: z.number(),
  insufficient_evidence: z.number(),
  grounding_rate: z.number(),

  total_feedback: z.number(),
  helpful_feedback: z.number(),
  not_helpful_feedback: z.number(),
  helpful_rate: z.number().nullable().default(null),
  rated_messages_rate: z.number().nullable().default(null),

  failures_by_code: z.record(z.string(), z.number()).default({}),

  suggestions_enabled: z.boolean(),
  streaming_enabled: z.boolean(),
  enabled: z.boolean(),
});

/**
 * The payload of one streamed `final` event.
 *
 * Parsed as strictly as the blocking response, because it carries the same
 * thing: a client that trusted an unvalidated stream frame would render whatever
 * arrived on a connection it has already committed to.
 */
export const streamFinalSchema = z.object({
  answer: z.string(),
  language: z.string(),
  grounded: z.boolean(),
  insufficient_evidence: z.boolean(),
  truncated: z.boolean().default(false),
  citations: z.array(assistantCitationSchema).default([]),
  retrieved_count: z.number().default(0),
  context_count: z.number().default(0),
  duration_ms: z.number().default(0),
  retrieval_ms: z.number().default(0),
  generation_ms: z.number().nullable().default(null),
});

export const streamRetrievalSchema = z.object({
  retrieved_count: z.number().default(0),
});

export const streamDeltaSchema = z.object({
  text: z.string(),
});

export const streamErrorSchema = z.object({
  error: z.string().default("internal_error"),
  message: z.string().default("An unexpected error occurred."),
});
