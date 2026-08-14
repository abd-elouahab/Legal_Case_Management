"use client";

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { NetworkError } from "@/lib/api/errors";
import { useErrorMessage, type ErrorCodeMap } from "@/hooks/use-error-message";
import { fetchCaseTimeline, fetchTimelineEvent } from "@/lib/api/timeline";
import type { TimelineEvent } from "@/types/timeline";
import type { TimelinePage, TimelineQuery } from "@/types/timeline-management";

/**
 * Server state for the Timeline.
 *
 * TanStack Query per `architecture.md`: timeline events are server state, so they
 * are cached rather than mirrored into a client store.
 *
 * **Queries only — there are no mutations.** The timeline is append-only and
 * written by the services that cause the events, so nothing here writes. What
 * *does* need care is invalidation from the other direction: a case edit or a
 * document upload produces new events, so those hooks invalidate
 * {@link timelineKeys} (see `hooks/use-cases.ts` and `hooks/use-documents.ts`).
 * Without that, a user would archive a case and watch its timeline not mention it.
 */

/**
 * Query keys.
 *
 * Scoped by case first, so invalidating one case's timeline does not discard
 * every other cached page. The list key includes the full query object, so
 * changing a filter is a different cache entry rather than a refetch that
 * discards the previous page — which is what makes paging back and forth instant.
 */
export const timelineKeys = {
  all: ["timeline"] as const,
  cases: () => [...timelineKeys.all, "case"] as const,
  case: (caseId: string) => [...timelineKeys.cases(), caseId] as const,
  caseList: (caseId: string, query: TimelineQuery) =>
    [...timelineKeys.case(caseId), query] as const,
  events: () => [...timelineKeys.all, "event"] as const,
  event: (eventId: string) => [...timelineKeys.events(), eventId] as const,
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
const TIMELINE_ERRORS: ErrorCodeMap = {
  timeline_event_not_found: "entryNotFound",
  case_not_found: "caseNotFound",
  validation_error: "invalidFilters",
  forbidden: "noAccess",
  missing_token: "sessionExpired",
};

export function useTimelineErrorMessage(): (error: unknown) => string {
  return useErrorMessage("timeline.errors", TIMELINE_ERRORS);
}

/**
 * One page of a case's timeline.
 *
 * `placeholderData` keeps the previous page on screen while the next one loads,
 * so paging or filtering does not blank the list out and shift the layout.
 */
export function useCaseTimeline(
  caseId: string,
  query: TimelineQuery,
  options: { enabled?: boolean } = {},
): UseQueryResult<TimelinePage, unknown> {
  return useQuery({
    queryKey: timelineKeys.caseList(caseId, query),
    queryFn: () => fetchCaseTimeline(caseId, query),
    enabled: (options.enabled ?? true) && Boolean(caseId),
    placeholderData: (previous) => previous,
  });
}

/** One timeline event by identifier. */
export function useTimelineEvent(
  eventId: string,
  options: { enabled?: boolean } = {},
): UseQueryResult<TimelineEvent, unknown> {
  return useQuery({
    queryKey: timelineKeys.event(eventId),
    queryFn: () => fetchTimelineEvent(eventId),
    enabled: (options.enabled ?? true) && Boolean(eventId),
  });
}
