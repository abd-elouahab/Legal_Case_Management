"use client";

import { TimelineIcon } from "@/components/timeline/timeline-icon";
import { useTranslations } from "next-intl";
import { useDateFormat } from "@/hooks/use-date-format";
import { roleMessageKey, USER_ROLES } from "@/types/user";
import type { TimelineEvent } from "@/types/timeline";

/**
 * One entry in a case's activity timeline.
 *
 * Shows the six things `08-timeline.md` requires: the event icon, its title, the
 * description, the user, their role, and the timestamp.
 *
 *     📄  Document Uploaded
 *         Amina Benali uploaded "Contract.pdf".
 *         Amina Benali · Administrator · Today • 14:32
 *
 * Two deliberate choices about *what is not shown*:
 *
 * * **No raw metadata.** The structured object exists so a future module can
 *   attach specifics without a schema change, and so the API's single-event
 *   endpoint can serve them — not so a user reads JSON. Everything a reader needs
 *   is already in the description, which the publisher writes as a sentence.
 * * **No relative "2 hours ago".** The exact time is what matters in a legal
 *   audit trail, so the timestamp degrades from *Today* to a date rather than to
 *   an elapsed interval, and the precise instant is on the element's `title`.
 *
 * `actorRole` is text rather than a `UserRole`, because it is a snapshot the API
 * may return for a role this build no longer knows. It is looked up leniently and
 * falls back to the stored value.
 */

export function TimelineEntry({ event }: { event: TimelineEvent }) {
  const { formatDateTime, formatEventTime } = useDateFormat();
  const t = useTranslations("timeline");
  const tRoles = useTranslations("users.roles");

  // `actorRole` is a snapshot taken when the event happened, so a role retired
  // since then must still read back. A value the platform still recognises is
  // translated; anything else is shown as stored rather than dropped.
  const role = event.actorRole
    ? (USER_ROLES as readonly string[]).includes(event.actorRole)
      ? tRoles(roleMessageKey(event.actorRole))
      : event.actorRole
    : null;

  return (
    <li className="flex gap-3">
      <div className="flex flex-col items-center">
        <TimelineIcon category={event.category} />
        {/* The thread joining one entry to the next. Purely decorative, and it
            grows with the entry rather than being a fixed height. */}
        <span
          className="mt-1 w-px flex-1 bg-border last:hidden"
          aria-hidden="true"
          data-testid="timeline-thread"
        />
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-1 pb-6">
        <p className="text-sm font-medium text-foreground">{event.title}</p>

        {event.description ? (
          // `whitespace-pre-line` keeps a multi-line description's structure,
          // which the API deliberately preserves rather than collapsing.
          <p className="whitespace-pre-line text-sm text-muted-foreground">
            {event.description}
          </p>
        ) : null}

        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <span className="font-medium text-foreground/80">
            {event.actorName ?? t("system")}
          </span>
          {role ? (
            <>
              <span aria-hidden="true">·</span>
              <span>{role}</span>
            </>
          ) : null}
          <span aria-hidden="true">·</span>
          <time dateTime={event.createdAt} title={formatDateTime(event.createdAt)}>
            {formatEventTime(event.createdAt)}
          </time>
        </p>
      </div>
    </li>
  );
}
