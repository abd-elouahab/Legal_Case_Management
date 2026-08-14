"use client";

import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  NOTIFICATION_CATEGORIES,
  NOTIFICATION_PRIORITIES,
  NOTIFICATION_TYPES,
  type NotificationListQuery,
  type NotificationPriority,
  type NotificationType,
} from "@/types/notification";

/**
 * Filters for the notification history.
 *
 * The four `16-notifications.md` asks for — read state, category, type, and
 * priority — and **every one of them executes on the server**. Filtering a page
 * after it arrives would return short pages, make the totals lie, and stop
 * working the moment somebody had more notifications than one page holds.
 *
 * `"any"` is a sentinel rather than an empty string, because Radix's `Select`
 * treats `""` as "no value" and would render the placeholder instead of the
 * option somebody chose. It never reaches the API: the change handler maps it
 * back to `null`, and `buildNotificationListParams` omits nulls entirely.
 *
 * Changing any filter resets to page 1 — a filter applied on page four of the
 * old result set would land on a page that no longer exists.
 */

const ANY = "any";

export function NotificationFilters({
  query,
  onChange,
  onReset,
}: {
  query: NotificationListQuery;
  onChange: (patch: Partial<NotificationListQuery>) => void;
  onReset: () => void;
}) {
  const t = useTranslations("notifications.filters");
  const tCategories = useTranslations("notifications.categories");
  const tPriorities = useTranslations("notifications.priorities");
  const tTypes = useTranslations("notifications.types");
  const tActions = useTranslations("common.actions");

  const isFiltered =
    query.unreadOnly ||
    query.category !== null ||
    query.notificationType !== null ||
    query.priority !== null;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        variant={query.unreadOnly ? "default" : "outline"}
        size="sm"
        onClick={() => onChange({ unreadOnly: !query.unreadOnly, page: 1 })}
        aria-pressed={query.unreadOnly}
      >
        {t("unreadOnly")}
      </Button>

      <Select
        value={query.category ?? ANY}
        onValueChange={(value) =>
          onChange({ category: value === ANY ? null : value, page: 1 })
        }
      >
        <SelectTrigger className="w-[11rem]" aria-label={t("byCategory")}>
          <SelectValue placeholder={t("allCategories")} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>{t("allCategories")}</SelectItem>
          {NOTIFICATION_CATEGORIES.map((category) => (
            <SelectItem key={category} value={category}>
              {tCategories(category)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Select
        value={query.notificationType ?? ANY}
        onValueChange={(value) =>
          onChange({
            notificationType: value === ANY ? null : (value as NotificationType),
            page: 1,
          })
        }
      >
        <SelectTrigger className="w-[9.5rem]" aria-label={t("byType")}>
          <SelectValue placeholder={t("allTypes")} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>{t("allTypes")}</SelectItem>
          {NOTIFICATION_TYPES.map((type) => (
            <SelectItem key={type} value={type}>
              {tTypes(type)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Select
        value={query.priority ?? ANY}
        onValueChange={(value) =>
          onChange({
            priority: value === ANY ? null : (value as NotificationPriority),
            page: 1,
          })
        }
      >
        <SelectTrigger className="w-[9.5rem]" aria-label={t("byPriority")}>
          <SelectValue placeholder={t("allPriorities")} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>{t("allPriorities")}</SelectItem>
          {NOTIFICATION_PRIORITIES.map((priority) => (
            <SelectItem key={priority} value={priority}>
              {tPriorities(priority)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {isFiltered ? (
        <Button variant="ghost" size="sm" onClick={onReset}>
          {tActions("clearFilters")}
        </Button>
      ) : null}
    </div>
  );
}
