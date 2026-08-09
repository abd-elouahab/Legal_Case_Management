"use client";

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
  NOTIFICATION_CATEGORY_LABELS,
  NOTIFICATION_PRIORITIES,
  NOTIFICATION_PRIORITY_LABELS,
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
        Unread only
      </Button>

      <Select
        value={query.category ?? ANY}
        onValueChange={(value) =>
          onChange({ category: value === ANY ? null : value, page: 1 })
        }
      >
        <SelectTrigger className="w-[11rem]" aria-label="Filter by category">
          <SelectValue placeholder="All categories" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>All categories</SelectItem>
          {NOTIFICATION_CATEGORIES.map((category) => (
            <SelectItem key={category} value={category}>
              {NOTIFICATION_CATEGORY_LABELS[category]}
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
        <SelectTrigger className="w-[9.5rem]" aria-label="Filter by type">
          <SelectValue placeholder="All types" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>All types</SelectItem>
          {NOTIFICATION_TYPES.map((type) => (
            <SelectItem key={type} value={type}>
              {type.charAt(0).toUpperCase() + type.slice(1)}
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
        <SelectTrigger className="w-[9.5rem]" aria-label="Filter by priority">
          <SelectValue placeholder="All priorities" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>All priorities</SelectItem>
          {NOTIFICATION_PRIORITIES.map((priority) => (
            <SelectItem key={priority} value={priority}>
              {NOTIFICATION_PRIORITY_LABELS[priority]}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {isFiltered ? (
        <Button variant="ghost" size="sm" onClick={onReset}>
          Clear filters
        </Button>
      ) : null}
    </div>
  );
}
