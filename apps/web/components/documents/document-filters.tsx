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
import type { DocumentListQueryState } from "@/hooks/use-document-list-query";
import { usePermissions } from "@/hooks/use-permissions";
import {
  DOCUMENT_CATEGORIES,
  SUPPORTED_DOCUMENT_EXTENSIONS,
  type DocumentCategory,
} from "@/types/document";
import { PERMISSION } from "@/types/authorization";

/**
 * Search and filter controls for the document list.
 *
 * "Any" is modelled as a sentinel value rather than an empty string, because a
 * Radix `SelectItem` cannot have an empty value — it reserves that for "nothing
 * selected", which would make the placeholder unreachable once a filter is set.
 * The sentinel is translated back to `null` here, so nothing outside this file
 * knows about it.
 *
 * The uploader filter only appears for callers who may read the user directory.
 * A lawyer sees only documents on their own cases, and cannot resolve a name from
 * an identifier anyway, so the control would be one they could not populate.
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

export function DocumentFilters({ list }: { list: DocumentListQueryState }) {
  const { can } = usePermissions();
  // Uploaders can be any role, but the two the platform assigns to cases are the
  // ones that realistically appear; the directory answers both from one query
  // each rather than a second "who has uploaded something" endpoint.
  const lawyers = useCaseAssignees("lawyer");
  const representatives = useCaseAssignees("court");
  const canFilterByUploader = can(PERMISSION.usersView);
  const uploaders = [...lawyers.users, ...representatives.users];
  const t = useTranslations("documents.filters");
  const tCategories = useTranslations("documents.categories");
  const tActions = useTranslations("common.actions");

  return (
    <div className="flex flex-col gap-4 rounded-xl border border-border p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end">
        <div className="flex flex-1 flex-col gap-2">
          <Label htmlFor="document-search">{tActions("search")}</Label>
          <div className="relative">
            <Search
              className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="document-search"
              type="search"
              value={list.searchInput}
              onChange={(event) => list.setSearch(event.target.value)}
              placeholder={t("searchPlaceholder")}
              className="ps-9"
            />
          </div>
        </div>

        <div className="flex flex-col gap-2 lg:w-56">
          <Label htmlFor="document-category-filter">{t("category")}</Label>
          <Select
            value={list.query.category ?? ANY}
            onValueChange={(value) =>
              list.setCategory(value === ANY ? null : (value as DocumentCategory))
            }
          >
            <SelectTrigger id="document-category-filter">
              <SelectValue placeholder={t("allCategories")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>{t("allCategories")}</SelectItem>
              {DOCUMENT_CATEGORIES.map((option) => (
                <SelectItem key={option} value={option}>
                  {tCategories(option)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-2 lg:w-40">
          <Label htmlFor="document-type-filter">{t("fileType")}</Label>
          <Select
            value={list.query.fileExtension ?? ANY}
            onValueChange={(value) => list.setFileExtension(value === ANY ? null : value)}
          >
            <SelectTrigger id="document-type-filter">
              <SelectValue placeholder={t("allTypes")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>{t("allTypes")}</SelectItem>
              {SUPPORTED_DOCUMENT_EXTENSIONS.map((option) => (
                <SelectItem key={option} value={option}>
                  {option.toUpperCase()}
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
        {canFilterByUploader ? (
          <div className="flex flex-col gap-2">
            <Label htmlFor="document-uploader-filter">{t("uploadedBy")}</Label>
            <Select
              value={list.query.uploadedBy ?? ANY}
              onValueChange={(value) => list.setUploadedBy(value === ANY ? null : value)}
            >
              <SelectTrigger id="document-uploader-filter">
                <SelectValue placeholder={t("anyone")} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ANY}>{t("anyone")}</SelectItem>
                {uploaders.map((user) => (
                  <SelectItem key={user.id} value={user.id}>
                    {user.fullName}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : null}

        <DateFilter
          id="document-uploaded-from"
          label={t("uploadedFrom")}
          value={list.query.uploadedFrom}
          onChange={(value) => list.setUploadedRange(value, list.query.uploadedTo)}
        />
        <DateFilter
          id="document-uploaded-to"
          label={t("uploadedUntil")}
          value={list.query.uploadedTo}
          onChange={(value) => list.setUploadedRange(list.query.uploadedFrom, value)}
        />
      </div>
    </div>
  );
}
