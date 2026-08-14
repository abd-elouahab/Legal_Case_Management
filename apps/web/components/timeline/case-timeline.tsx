"use client";

import { History, SearchX } from "lucide-react";
import { useTranslations } from "next-intl";

import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { Button } from "@/components/ui/button";
import { TimelineEntry } from "@/components/timeline/timeline-entry";
import { TimelineFilters } from "@/components/timeline/timeline-filters";
import { TimelinePagination } from "@/components/timeline/timeline-pagination";
import { TimelineSkeleton } from "@/components/timeline/timeline-skeleton";
import { useTimelineQuery } from "@/hooks/use-timeline-query";
import { useCaseTimeline, useTimelineErrorMessage } from "@/hooks/use-timeline";
import type { TimelineEvent } from "@/types/timeline";

/**
 * The Timeline section of a case workspace: the case's chronological history.
 *
 * A client component because everything here is interactive. It holds no state of
 * its own — query state lives in `useTimelineQuery`, server state in
 * `useCaseTimeline` — so it composes them rather than implementing either.
 *
 * What a user sees is scoped by the API: a caller not assigned to the case is
 * refused outright rather than handed an empty list, which is why the error state
 * below is a real possibility and not defensive padding.
 *
 * This replaces the dashed Timeline placeholder the case details page carried
 * while the module was unbuilt.
 */
export function CaseTimeline({ caseId }: { caseId: string }) {
  const t = useTranslations("timeline");
  const tActions = useTranslations("common.actions");
  const errorMessage = useTimelineErrorMessage();
  const list = useTimelineQuery();
  const { data, isLoading, isFetching, isError, error, refetch } = useCaseTimeline(
    caseId,
    list.query,
  );

  const events: TimelineEvent[] = data?.items ?? [];
  // `isFetching` without `isLoading` is a background refresh — a new page or
  // filter — where the previous data is still on screen.
  const isRefreshing = isFetching && !isLoading;

  return (
    <section className="flex flex-col gap-4" aria-labelledby="case-timeline">
      <h3
        id="case-timeline"
        className="flex items-center gap-2 text-sm font-medium text-foreground"
      >
        <History className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        {t("title")}
      </h3>

      <TimelineFilters list={list} />

      {isLoading ? (
        <TimelineSkeleton />
      ) : isError ? (
        <ErrorState
          title={t("errors.loadTitle")}
          description={errorMessage(error)}
          onRetry={() => void refetch()}
        />
      ) : events.length === 0 ? (
        // Two genuinely different situations: a case nothing has happened to yet
        // needs an explanation, a fruitless search needs a way back.
        list.isFiltered ? (
          <EmptyState
            icon={SearchX}
            titleKey="timeline.empty.filteredTitle"
            descriptionKey="timeline.empty.filteredDescription"
            action={
              <Button variant="outline" onClick={list.reset}>
                {tActions("clearFilters")}
              </Button>
            }
          />
        ) : (
          <EmptyState
            icon={History}
            titleKey="timeline.empty.title"
            descriptionKey="timeline.empty.description"
          />
        )
      ) : (
        <>
          <ul className="flex flex-col">
            {events.map((event) => (
              <TimelineEntry key={event.id} event={event} />
            ))}
          </ul>

          <TimelinePagination
            page={data?.page ?? 1}
            pageSize={data?.pageSize ?? list.query.pageSize}
            totalPages={data?.totalPages ?? 1}
            totalRecords={data?.totalRecords ?? 0}
            onPageChange={list.setPage}
            isLoading={isRefreshing}
          />
        </>
      )}
    </section>
  );
}
