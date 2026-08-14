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
import { roleMessageKey, USER_ROLES, USER_STATUSES } from "@/types/user";
import type { UserRole, UserStatus } from "@/types/user";

/**
 * Search and filter controls for the user directory.
 *
 * "All" is modelled as a sentinel value rather than an empty string, because a
 * Radix `SelectItem` cannot have an empty value — it reserves that for "nothing
 * selected", which would make the placeholder unreachable once a filter is set.
 * The sentinel is translated back to `null` here, so nothing outside this file
 * knows about it.
 */

const ANY = "__any__";

export function UserFilters({
  search,
  role,
  status,
  onSearchChange,
  onRoleChange,
  onStatusChange,
  onReset,
  isFiltered,
}: {
  search: string;
  role: UserRole | null;
  status: UserStatus | null;
  onSearchChange: (value: string) => void;
  onRoleChange: (role: UserRole | null) => void;
  onStatusChange: (status: UserStatus | null) => void;
  onReset: () => void;
  isFiltered: boolean;
}) {
  const t = useTranslations("users.filters");
  const tRoles = useTranslations("users.roles");
  const tStatuses = useTranslations("users.statuses");
  const tActions = useTranslations("common.actions");

  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
      <div className="flex flex-1 flex-col gap-2">
        <Label htmlFor="user-search">{tActions("search")}</Label>
        <div className="relative">
          <Search
            className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="user-search"
            type="search"
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
            placeholder={t("searchPlaceholder")}
            className="ps-9"
          />
        </div>
      </div>

      <div className="flex flex-col gap-2 sm:w-48">
        <Label htmlFor="user-role-filter">{t("role")}</Label>
        <Select
          value={role ?? ANY}
          onValueChange={(value) => onRoleChange(value === ANY ? null : (value as UserRole))}
        >
          <SelectTrigger id="user-role-filter">
            <SelectValue placeholder={t("allRoles")} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>{t("allRoles")}</SelectItem>
            {USER_ROLES.map((option) => (
              <SelectItem key={option} value={option}>
                {tRoles(roleMessageKey(option))}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-col gap-2 sm:w-44">
        <Label htmlFor="user-status-filter">{t("status")}</Label>
        <Select
          value={status ?? ANY}
          onValueChange={(value) => onStatusChange(value === ANY ? null : (value as UserStatus))}
        >
          <SelectTrigger id="user-status-filter">
            <SelectValue placeholder={t("allStatuses")} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>{t("allStatuses")}</SelectItem>
            {USER_STATUSES.map((option) => (
              <SelectItem key={option} value={option}>
                {tStatuses(option)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {isFiltered ? (
        <Button type="button" variant="ghost" onClick={onReset} className="sm:mb-0">
          <X className="h-4 w-4" />
          {tActions("clear")}
        </Button>
      ) : null}
    </div>
  );
}
