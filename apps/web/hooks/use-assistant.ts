"use client";

import * as React from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import {
  createConversation,
  deleteConversation,
  fetchAssistantMetrics,
  fetchConversation,
  fetchConversations,
  sendMessage,
  streamMessage,
  submitFeedback,
  updateConversation,
  withdrawFeedback,
} from "@/lib/api/assistant";
import { ApiError, NetworkError } from "@/lib/api/errors";
import { useErrorMessage, type ErrorCodeMap } from "@/hooks/use-error-message";
import type {
  AssistantCitation,
  AssistantMetrics,
  Conversation,
  ConversationCreateInput,
  ConversationDetail,
  ConversationListParams,
  ConversationMessage,
  ConversationPage,
  ConversationUpdateInput,
  FeedbackRating,
  MessageExchange,
  MessageInput,
} from "@/types/assistant";

/**
 * Server state for the AI Legal Assistant.
 *
 * TanStack Query per `architecture.md`: conversations and transcripts are server
 * state, so they are fetched and cached rather than mirrored into a client store.
 *
 * **Sending a message is a mutation, and reading a conversation is a query**, and
 * the split is not a formality: a message costs a retrieval, a model call, and two
 * database rows, so it must happen on submit and never on render. The
 * conversation it lands in is then invalidated, which is what makes the list row
 * and the transcript agree without either being written to twice.
 *
 * No business logic lives in components: these hooks are the only place the UI
 * talks to the assistant API.
 */

/** Conversations per page in the sidebar list. */
export const CONVERSATION_PAGE_SIZE = 20;

/** Query keys. */
export const assistantKeys = {
  all: ["assistant"] as const,
  conversations: () => [...assistantKeys.all, "conversations"] as const,
  conversationList: (params: ConversationListParams) =>
    [...assistantKeys.conversations(), params] as const,
  conversation: (id: string) => [...assistantKeys.all, "conversation", id] as const,
  metrics: () => [...assistantKeys.all, "metrics"] as const,
};

/**
 * Translate a failure into a sentence in the reader's language.
 *
 * Branches on the API's machine-readable `code` rather than on message text —
 * which the server writes in English, with no knowledge of who is reading it.
 * `hooks/use-error-message.ts` records why that matters; the short version is
 * that an interface which is Arabic everywhere except when something goes wrong
 * is not localized. Codes with no entry here fall through to the shared
 * `errors.*` sentences and then to a generic one.
 */
const ASSISTANT_ERRORS: ErrorCodeMap = {
  assistant_disabled: "disabled",
  rag_disabled: "disabled",
  retrieval_unavailable: "retrievalUnavailable",
  llm_unavailable: "modelUnavailable",
  llm_failure: "modelFailure",
  malformed_response: "malformedResponse",
  timeout: "timeout",
  context_overflow: "contextOverflow",
  conversation_not_found: "conversationNotFound",
  conversation_message_not_found: "messageNotFound",
  conversation_archived: "conversationArchived",
  conversation_full: "conversationFull",
  invalid_feedback_target: "invalidFeedbackTarget",
  invalid_question: "invalidQuestion",
  search_filter_too_broad: "filterTooBroad",
  case_not_found: "caseNotFound",
  forbidden: "noCaseAccess",
  missing_token: "sessionExpired",
};

export function useAssistantErrorMessage(): (error: unknown) => string {
  return useErrorMessage("assistant.errors", ASSISTANT_ERRORS);
}

/** Whether a failure is a dependency outage rather than anything about the request. */
export function isAssistantUnavailable(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    [
      "assistant_disabled",
      "rag_disabled",
      "retrieval_unavailable",
      "llm_unavailable",
      "llm_failure",
      "timeout",
      "malformed_response",
      "context_overflow",
    ].includes(error.code)
  );
}

// --------------------------------------------------------------------------- //
// Conversations
// --------------------------------------------------------------------------- //

/** The caller's own conversations, most recently active first. */
export function useConversations(
  params: ConversationListParams = {},
): UseQueryResult<ConversationPage, unknown> {
  const query = { pageSize: CONVERSATION_PAGE_SIZE, ...params };
  return useQuery({
    queryKey: assistantKeys.conversationList(query),
    queryFn: () => fetchConversations(query),
  });
}

/**
 * One conversation and the first page of its transcript.
 *
 * Disabled without an identifier rather than called with a placeholder: a query
 * that fetched `/conversations/undefined` would answer 404 and put a spurious
 * error on a screen where nothing has been opened yet.
 */
export function useConversation(
  id: string | null,
): UseQueryResult<ConversationDetail, unknown> {
  return useQuery({
    queryKey: assistantKeys.conversation(id ?? ""),
    queryFn: () => fetchConversation(id as string),
    enabled: Boolean(id),
  });
}

/** Start a new conversation. */
export function useCreateConversation(): UseMutationResult<
  ConversationDetail,
  unknown,
  ConversationCreateInput
> {
  const client = useQueryClient();

  return useMutation({
    mutationFn: (input: ConversationCreateInput) => createConversation(input),
    onSuccess: (conversation) => {
      client.setQueryData(assistantKeys.conversation(conversation.id), conversation);
      void client.invalidateQueries({ queryKey: assistantKeys.conversations() });
    },
  });
}

/** Rename a conversation, archive it, or restore it. */
export function useUpdateConversation(): UseMutationResult<
  Conversation,
  unknown,
  { id: string; input: ConversationUpdateInput }
> {
  const client = useQueryClient();

  return useMutation({
    mutationFn: ({ id, input }: { id: string; input: ConversationUpdateInput }) =>
      updateConversation(id, input),
    onSuccess: (conversation) => {
      void client.invalidateQueries({ queryKey: assistantKeys.conversations() });
      void client.invalidateQueries({
        queryKey: assistantKeys.conversation(conversation.id),
      });
    },
  });
}

/** Withdraw a conversation. */
export function useDeleteConversation(): UseMutationResult<void, unknown, string> {
  const client = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: (_result, id) => {
      client.removeQueries({ queryKey: assistantKeys.conversation(id) });
      void client.invalidateQueries({ queryKey: assistantKeys.conversations() });
    },
  });
}

// --------------------------------------------------------------------------- //
// Messaging
// --------------------------------------------------------------------------- //

/**
 * Send one message and wait for the whole answer.
 *
 * Deliberately **not retried**. A 503 means retrieval or the model is
 * unavailable, and retrying costs another retrieval to fail the same way; a 403,
 * a 409, or a 422 will never succeed on a repeat. Worse, an automatic retry of a
 * request that *did* reach the model would append a second answer to the
 * transcript. The user retries by pressing send, which is also the only signal
 * that they still want it.
 */
export function useSendMessage(): UseMutationResult<
  MessageExchange,
  unknown,
  { conversationId: string; input: MessageInput }
> {
  const client = useQueryClient();

  return useMutation({
    mutationFn: ({ conversationId, input }: { conversationId: string; input: MessageInput }) =>
      sendMessage(conversationId, input),
    retry: false,
    onSuccess: (exchange) => {
      void client.invalidateQueries({
        queryKey: assistantKeys.conversation(exchange.conversation.id),
      });
      void client.invalidateQueries({ queryKey: assistantKeys.conversations() });
    },
  });
}

/** What a streaming exchange looks like while it is in flight. */
export interface PendingAnswer {
  /** The question, echoed locally so it appears the instant it is sent. */
  question: string;
  /** The answer so far. Replaced by the authoritative text when `final` arrives. */
  answer: string;
  /** Passages retrieved, once the `retrieval` event has arrived. */
  retrievedCount: number | null;
  /** Whether the first fragment has arrived — the difference between "searching" and "writing". */
  streaming: boolean;
  citations: AssistantCitation[];
}

/** Everything a chat screen needs to run one conversation. */
export interface ChatSession {
  /** True from the moment a message is sent until its answer is stored. */
  isSending: boolean;
  /** The exchange in flight, or `null`. */
  pending: PendingAnswer | null;
  error: unknown;
  send: (input: MessageInput) => void;
  /** Discard the current error, so a retry starts from a clean screen. */
  reset: () => void;
}

/**
 * Run one conversation, streaming when the platform allows it.
 *
 * The whole reason this is a hook rather than a mutation is the **pending
 * answer**: a streamed reply exists on screen before it exists on the server, so
 * something has to hold the partial text, and it must be discarded the moment the
 * authoritative transcript arrives — otherwise the last answer would render twice,
 * once from the stream and once from the refetched conversation.
 *
 * **Falls back to the blocking endpoint** when streaming is turned off for the
 * deployment or when the browser cannot read a stream. The two paths differ only
 * in how the text arrives; both end with the conversation invalidated and the
 * stored transcript on screen.
 */
export function useChatSession(
  conversationId: string | null,
  options: { streaming?: boolean } = {},
): ChatSession {
  const client = useQueryClient();
  const [pending, setPending] = React.useState<PendingAnswer | null>(null);
  const [isSending, setIsSending] = React.useState(false);
  const [error, setError] = React.useState<unknown>(null);
  const blocking = useSendMessage();

  const streamingEnabled =
    (options.streaming ?? true) && typeof globalThis.ReadableStream !== "undefined";

  const finish = React.useCallback(
    (id: string) => {
      // Invalidated *before* the pending answer is cleared, so the stored
      // transcript is already on its way in — clearing first would blank the last
      // answer for however long the refetch takes.
      void client.invalidateQueries({ queryKey: assistantKeys.conversation(id) });
      void client.invalidateQueries({ queryKey: assistantKeys.conversations() });
      setPending(null);
      setIsSending(false);
    },
    [client],
  );

  const send = React.useCallback(
    (input: MessageInput) => {
      if (!conversationId || isSending) return;

      setError(null);
      setIsSending(true);
      setPending({
        question: input.content,
        answer: "",
        retrievedCount: null,
        streaming: false,
        citations: [],
      });

      if (!streamingEnabled) {
        blocking.mutate(
          { conversationId, input },
          {
            onSuccess: () => finish(conversationId),
            onError: (failure) => {
              setError(failure);
              setPending(null);
              setIsSending(false);
            },
          },
        );
        return;
      }

      void (async () => {
        try {
          for await (const event of streamMessage(conversationId, input)) {
            if (event.kind === "retrieval") {
              setPending((current) =>
                current ? { ...current, retrievedCount: event.retrievedCount } : current,
              );
            } else if (event.kind === "delta") {
              setPending((current) =>
                current
                  ? { ...current, answer: current.answer + event.text, streaming: true }
                  : current,
              );
            } else if (event.kind === "final") {
              // The authoritative answer replaces what was accumulated: a
              // dangling citation marker has been removed from it, and a model
              // that declined has had its internal token replaced by the
              // platform's own sentence.
              setPending((current) =>
                current
                  ? {
                      ...current,
                      answer: event.answer,
                      citations: event.citations,
                      streaming: false,
                    }
                  : current,
              );
              finish(conversationId);
            } else {
              // An `error` frame arrives *after* the 200 status line, so it has
              // no HTTP status of its own. It is reported as a 503 because that
              // is what the same failure would have been had it happened one
              // moment earlier — the cause is carried through as `code`, which is
              // what `assistantErrorMessage` actually branches on.
              setError(
                new ApiError({ status: 503, code: event.error, message: event.message }),
              );
              setPending(null);
              setIsSending(false);
            }
          }
        } catch (failure) {
          setError(failure);
          setPending(null);
          setIsSending(false);
        }
      })();
    },
    [blocking, conversationId, finish, isSending, streamingEnabled],
  );

  const reset = React.useCallback(() => {
    setError(null);
    setPending(null);
    setIsSending(false);
  }, []);

  return { isSending, pending, error, send, reset };
}

// --------------------------------------------------------------------------- //
// Feedback
// --------------------------------------------------------------------------- //

/**
 * Rate an answer, or change a rating.
 *
 * Invalidates the conversation rather than writing the rating into the cache by
 * hand: the server owns the timestamps, and a locally-invented `updatedAt` that
 * disagreed with the stored one would be a bug nobody would look for.
 */
export function useSubmitFeedback(): UseMutationResult<
  unknown,
  unknown,
  { conversationId: string; messageId: string; rating: FeedbackRating; comment?: string }
> {
  const client = useQueryClient();

  return useMutation({
    mutationFn: ({
      conversationId,
      messageId,
      rating,
      comment,
    }: {
      conversationId: string;
      messageId: string;
      rating: FeedbackRating;
      comment?: string;
    }) => submitFeedback(conversationId, messageId, rating, comment),
    onSuccess: (_result, variables) => {
      void client.invalidateQueries({
        queryKey: assistantKeys.conversation(variables.conversationId),
      });
    },
  });
}

/** Withdraw a rating. */
export function useWithdrawFeedback(): UseMutationResult<
  void,
  unknown,
  { conversationId: string; messageId: string }
> {
  const client = useQueryClient();

  return useMutation({
    mutationFn: ({
      conversationId,
      messageId,
    }: {
      conversationId: string;
      messageId: string;
    }) => withdrawFeedback(conversationId, messageId),
    onSuccess: (_result, variables) => {
      void client.invalidateQueries({
        queryKey: assistantKeys.conversation(variables.conversationId),
      });
    },
  });
}

// --------------------------------------------------------------------------- //
// Monitoring
// --------------------------------------------------------------------------- //

/** Platform-wide assistant health. Requires `ai:monitor`. */
export function useAssistantMetrics(
  options: { enabled?: boolean } = {},
): UseQueryResult<AssistantMetrics, unknown> {
  return useQuery({
    queryKey: assistantKeys.metrics(),
    queryFn: fetchAssistantMetrics,
    enabled: options.enabled ?? true,
  });
}

/** The last assistant message of a transcript, if there is one. */
export function lastAnswer(messages: ConversationMessage[]): ConversationMessage | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message && message.role === "assistant") return message;
  }
  return null;
}
