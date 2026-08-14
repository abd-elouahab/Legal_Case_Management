"use client";

import { useTranslations } from "next-intl";
import { Search, X } from "lucide-react";

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
import type { CaseListQueryState } from "@/hooks/use-case-list-query";
import {
  CASE_PRIORITIES,
  CASE_STATUSES,
  type CasePriority,
  type CaseStatus,
} from "@/types/case";

/**
 * Search and filter controls for the case list.
 *
 * "Any" is modelled as a sentinel value rather than an empty string, because a
 * Radix `SelectItem` cannot have an empty value — it reserves that for "nothing
 * selected", which would make the placeholder unreachable once a filter is set.
 * The sentinel is translated back to `null` here, so nothing outside this file
 * knows about it.
 *
 * The two assignee filters only appear for callers who may read the user
 * directory. A lawyer sees only their own cases, so "filter by assigned lawyer"
 * would be a control with one possible answer.
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
      <Input
        id={id}
        type="date"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

export function CaseFilters({ list }: { list: CaseListQueryState }) {
  const lawyers = useCaseAssignees("lawyer");
  const representatives = useCaseAssignees("court");
  const t = useTranslations("cases.filters");
  const tStatuses = useTranslations("cases.statuses");
  const tPriorities = useTranslations("cases.priorities");
  const tActions = useTranslations("common.actions");

  return (
    <div className="flex flex-col gap-4 rounded-xl border border-border p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end">
        <div className="flex flex-1 flex-col gap-2">
          <Label htmlFor="case-search">{tActions("search")}</Label>
          <div className="relative">
            <Search
              className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="case-search"
              type="search"
              value={list.textInput.search}
              onChange={(event) => list.setSearch(event.target.value)}
              placeholder={t("searchPlaceholder")}
              className="ps-9"
            />
          </div>
        </div>

        <div className="flex flex-col gap-2 lg:w-52">
          <Label htmlFor="case-status-filter">{t("status")}</Label>
          <Select
            value={list.query.status ?? ANY}
            onValueChange={(value) =>
              list.setStatus(value === ANY ? null : (value as CaseStatus))
            }
          >
            <SelectTrigger id="case-status-filter">
              <SelectValue placeholder={t("allStatuses")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>{t("allStatuses")}</SelectItem>
              {CASE_STATUSES.map((option) => (
                <SelectItem key={option} value={option}>
                  {tStatuses(option)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-2 lg:w-44">
          <Label htmlFor="case-priority-filter">{t("priority")}</Label>
          <Select
            value={list.query.priority ?? ANY}
            onValueChange={(value) =>
              list.setPriority(value === ANY ? null : (value as CasePriority))
            }
          >
            <SelectTrigger id="case-priority-filter">
              <SelectValue placeholder={t("allPriorities")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>{t("allPriorities")}</SelectItem>
              {CASE_PRIORITIES.map((option) => (
                <SelectItem key={option} value={option}>
                  {tPriorities(option)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {list.isFiltered ? (
          <Button type="button" variant="ghost" onClick={list.reset}>
            <X className="h-4 w-4" />
            {tActions("clear")}
          </Button>
        ) : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <div className="flex flex-col gap-2">
          <Label htmlFor="case-court-filter">{t("court")}</Label>
          <Input
            id="case-court-filter"
            type="search"
            value={list.textInput.courtName}
            onChange={(event) => list.setCourtName(event.target.value)}
            placeholder={t("anyCourt")}
          />
        </div>

        {lawyers.isAvailable ? (
          <div className="flex flex-col gap-2">
            <Label htmlFor="case-lawyer-filter">{t("assignedLawyer")}</Label>
            <Select
              value={list.query.assignedLawyerId ?? ANY}
              onValueChange={(value) =>
                list.setAssignedLawyerId(value === ANY ? null : value)
              }
            >
              <SelectTrigger id="case-lawyer-filter">
                <SelectValue placeholder={t("anyLawyer")} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ANY}>{t("anyLawyer")}</SelectItem>
                {lawyers.users.map((user) => (
                  <SelectItem key={user.id} value={user.id}>
                    {user.fullName}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : null}

        {representatives.isAvailable ? (
          <div className="flex flex-col gap-2">
            <Label htmlFor="case-representative-filter">{t("assignedRepresentative")}</Label>
            <Select
              value={list.query.assignedCourtRepresentativeId ?? ANY}
              onValueChange={(value) =>
                list.setAssignedCourtRepresentativeId(value === ANY ? null : value)
              }
            >
              <SelectTrigger id="case-representative-filter">
                <SelectValue placeholder={t("anyRepresentative")} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ANY}>{t("anyRepresentative")}</SelectItem>
                {representatives.users.map((user) => (
                  <SelectItem key={user.id} value={user.id}>
                    {user.fullName}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <DateFilter
          id="case-filing-from"
          label={t("filedFrom")}
          value={list.query.filingDateFrom}
          onChange={(value) => list.setFilingDateRange(value, list.query.filingDateTo)}
        />
        <DateFilter
          id="case-filing-to"
          label={t("filedUntil")}
          value={list.query.filingDateTo}
          onChange={(value) => list.setFilingDateRange(list.query.filingDateFrom, value)}
        />
        <DateFilter
          id="case-hearing-from"
          label={t("hearingFrom")}
          value={list.query.hearingDateFrom}
          onChange={(value) => list.setHearingDateRange(value, list.query.hearingDateTo)}
        />
        <DateFilter
          id="case-hearing-to"
          label={t("hearingUntil")}
          value={list.query.hearingDateTo}
          onChange={(value) => list.setHearingDateRange(list.query.hearingDateFrom, value)}
        />
      </div>
    </div>
  );
}
