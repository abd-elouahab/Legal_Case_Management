"use client";

import { ArrowDownWideNarrow, ArrowUpWideNarrow, Search, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useCaseAssignees } from "@/hooks/use-case-assignees";
import { usePermissions } from "@/hooks/use-permissions";
import type { TimelineQueryState } from "@/hooks/use-timeline-query";
import { PERMISSION } from "@/types/authorization";
import { TIMELINE_EVENT_TYPES, TIMELINE_EVENT_TYPE_LABELS } from "@/types/timeline";

/**
 * Search, filter, and sort controls for a case timeline.
 *
 * "Any" is modelled as a sentinel value rather than an empty string, because a
 * Radix `SelectItem` cannot have an empty value — it reserves that for "nothing
 * selected", which would make the placeholder unreachable once a filter is set.
 * The sentinel is translated back to `null` here, so nothing outside this file
 * knows about it.
 *
 * The **actor filter only appears for callers who may read the user directory**.
 * A lawyer cannot resolve a name from an identifier, so the control would be one
 * they could not populate — the same rule the document filters apply to
 * "Uploaded by".
 *
 * The event-type list is what the platform records *today*. An event published by
 * a later module still appears in the timeline; it simply is not offered as a
 * filter until this list grows, which is the honest trade for a menu that has to
 * show a label.
 */

const ANY = "__any__";

/** A labelled date input; `<input type="date">` already supplies the picker. */
function DateFilter({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id} className="text-xs text-muted-foreground">
        {label}
      </Label>
      <Input id={id} type="date" value={value} onChange={(event) => onChange(event.target.value)} />
    </div>
  );
}

export function TimelineFilters({ list }: { list: TimelineQueryState }) {
  const { can } = usePermissions();
  const lawyers = useCaseAssignees("lawyer");
  const representatives = useCaseAssignees("court");
  const canFilterByActor = can(PERMISSION.usersView);
  // Two directory queries, one menu. Deduplicated by identifier because the two
  // results are only disjoint as long as no account holds both roles — and a
  // repeated `key` in a React list is a defect whatever made it repeat.
  const actors = [...new Map([...lawyers.users, ...representatives.users].map((u) => [u.id, u])).values()];

  const isNewestFirst = list.query.sortOrder === "desc";

  return (
    <div className="flex flex-col gap-4 rounded-xl border border-border p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end">
        <div className="flex flex-1 flex-col gap-2">
          <Label htmlFor="timeline-search">Search activity</Label>
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="timeline-search"
              type="search"
              value={list.searchInput}
              onChange={(event) => list.setSearch(event.target.value)}
              placeholder="Search by what happened"
              className="pl-9"
            />
          </div>
        </div>

        <div className="flex flex-col gap-2 lg:w-64">
          <Label htmlFor="timeline-type-filter">Activity type</Label>
          <Select
            value={list.query.eventType ?? ANY}
            onValueChange={(value) => list.setEventType(value === ANY ? null : value)}
          >
            <SelectTrigger id="timeline-type-filter">
              <SelectValue placeholder="All activity" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>All activity</SelectItem>
              {TIMELINE_EVENT_TYPES.map((option) => (
                <SelectItem key={option} value={option}>
                  {TIMELINE_EVENT_TYPE_LABELS[option]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <Button
          type="button"
          variant="outline"
          onClick={list.toggleSortOrder}
          // Says what pressing it *will do*, which is what a screen-reader user
          // needs — the visible label already says what is currently applied.
          aria-label={isNewestFirst ? "Sort oldest first" : "Sort newest first"}
        >
          {isNewestFirst ? (
            <ArrowDownWideNarrow className="h-4 w-4" />
          ) : (
            <ArrowUpWideNarrow className="h-4 w-4" />
          )}
          {isNewestFirst ? "Newest first" : "Oldest first"}
        </Button>

        {list.isFiltered ? (
          <Button type="button" variant="ghost" onClick={list.reset}>
            <X className="h-4 w-4" />
            Clear
          </Button>
        ) : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {canFilterByActor ? (
          <div className="flex flex-col gap-2">
            <Label htmlFor="timeline-actor-filter">Performed by</Label>
            <Select
              value={list.query.actorId ?? ANY}
              onValueChange={(value) => list.setActor(value === ANY ? null : value)}
            >
              <SelectTrigger id="timeline-actor-filter">
                <SelectValue placeholder="Anyone" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ANY}>Anyone</SelectItem>
                {actors.map((user) => (
                  <SelectItem key={user.id} value={user.id}>
                    {user.fullName}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : null}

        <DateFilter
          id="timeline-date-from"
          label="From"
          value={list.query.dateFrom}
          onChange={(value) => list.setDateRange(value, list.query.dateTo)}
        />
        <DateFilter
          id="timeline-date-to"
          label="Until"
          value={list.query.dateTo}
          onChange={(value) => list.setDateRange(list.query.dateFrom, value)}
        />
      </div>
    </div>
  );
}
