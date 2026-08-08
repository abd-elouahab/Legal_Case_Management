"use client";

import * as React from "react";
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
import { useReportTemplates } from "@/hooks/use-reports";
import {
  REPORT_STATUSES,
  REPORT_STATUS_LABELS,
  reportTypeLabel,
  type ReportStatus,
  type ReportType,
} from "@/types/report";
import type { ReportListQuery } from "@/types/report-management";

/**
 * Filters for the report history: a title search, a status, and a type.
 *
 * Deliberately three. A report history is one user's own list of a handful of
 * documents per matter, and the only questions anyone asks of it are "which ones
 * are still running", "where is the hearing prep one", and "the one about the
 * Atlas case" — the third being the `case_id` the case workspace passes rather
 * than a control here, since nobody picks a case from a dropdown to find a report
 * they opened the case to look for.
 *
 * **The type options come from the server's catalogue**, so a sixth template
 * appears in this filter without a frontend change — the same reason the generate
 * dialog fetches them.
 *
 * **The search runs on submit, not as you type.** Each keystroke would be a
 * request, and a title search is a phrase somebody finished writing rather than a
 * prefix they are still typing.
 */
export function ReportFilters({
  query,
  onChange,
  onReset,
  disabled = false,
}: {
  query: ReportListQuery;
  onChange: (patch: Partial<ReportListQuery>) => void;
  onReset: () => void;
  disabled?: boolean;
}) {
  const templates = useReportTemplates();
  const [search, setSearch] = React.useState(query.search ?? "");

  // Keep the box in step when the query is reset from outside — adjusted during
  // render rather than in an effect
  // (https://react.dev/learn/you-might-not-need-an-effect).
  const [lastSearch, setLastSearch] = React.useState(query.search ?? "");
  if ((query.search ?? "") !== lastSearch) {
    setLastSearch(query.search ?? "");
    setSearch(query.search ?? "");
  }

  const hasFilters =
    Boolean(query.search) || query.status !== null || query.reportType !== null;

  return (
    <form
      className="flex flex-col gap-3 sm:flex-row sm:items-end"
      onSubmit={(event) => {
        event.preventDefault();
        onChange({ search: search.trim() || null });
      }}
    >
      <div className="flex flex-1 flex-col gap-2">
        <Label htmlFor="report-search">Search</Label>
        <div className="flex gap-2">
          <Input
            id="report-search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search report titles"
            disabled={disabled}
          />
          <Button type="submit" variant="outline" disabled={disabled}>
            <Search className="h-4 w-4" aria-hidden="true" />
            <span className="sr-only sm:not-sr-only">Search</span>
          </Button>
        </div>
      </div>

      <div className="flex flex-col gap-2 sm:w-44">
        <Label htmlFor="report-status">Status</Label>
        <Select
          value={query.status ?? "all"}
          onValueChange={(value) =>
            onChange({ status: value === "all" ? null : (value as ReportStatus) })
          }
          disabled={disabled}
        >
          <SelectTrigger id="report-status">
            <SelectValue placeholder="All" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            {REPORT_STATUSES.map((status) => (
              <SelectItem key={status} value={status}>
                {REPORT_STATUS_LABELS[status]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-col gap-2 sm:w-52">
        <Label htmlFor="report-type">Type</Label>
        <Select
          value={query.reportType ?? "all"}
          onValueChange={(value) =>
            onChange({ reportType: value === "all" ? null : (value as ReportType) })
          }
          disabled={disabled}
        >
          <SelectTrigger id="report-type">
            <SelectValue placeholder="All" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All types</SelectItem>
            {(templates.data ?? []).map((template) => (
              <SelectItem key={template.reportType} value={template.reportType}>
                {reportTypeLabel(template.reportType)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {hasFilters ? (
        <Button type="button" variant="ghost" onClick={onReset} disabled={disabled}>
          <X className="h-4 w-4" aria-hidden="true" />
          Clear
        </Button>
      ) : null}
    </form>
  );
}
