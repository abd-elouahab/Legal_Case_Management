"use client";

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { ApiError, NetworkError } from "@/lib/api/errors";
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
 * Translate a failure into a message safe to show the user.
 *
 * Branches on the API's machine-readable `code` rather than on message text,
 * which is localizable and may change.
 */
export function timelineErrorMessage(error: unknown): string {
  if (error instanceof NetworkError) return error.message;

  if (error instanceof ApiError) {
    switch (error.code) {
      case "timeline_event_not_found":
        return "This activity entry no longer exists.";
      case "case_not_found":
        return "That case no longer exists. Refresh the page and try again.";
      case "validation_error":
        return error.details[0]?.message ?? "Check the filters you applied.";
      case "forbidden":
        return "You do not have permission to view this case's activity.";
      case "invalid_token":
      case "token_expired":
      case "missing_token":
        return "Your session has expired. Sign in again to continue.";
      default:
        return error.message || "Something went wrong. Please try again.";
    }
  }

  return "Something went wrong. Please try again.";
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
