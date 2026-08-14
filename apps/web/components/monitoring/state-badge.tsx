"use client";

import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";
import {
  ALERT_SEVERITY_CLASSES,
  HEALTH_STATE_CLASSES,
  type AlertSeverity,
  type HealthState,
} from "@/types/monitoring";

/**
 * The page's two coloured chips: a health state and an alert severity.
 *
 * One component per vocabulary rather than a generic one, because the two carry
 * different meanings and read from different palettes — and because a shared
 * `variant` prop is how *degraded* and *warning* eventually end up the same
 * colour by accident, which would make the page harder to scan rather than
 * easier.
 *
 * **The colours are tokens, never literals.** `ui-context.md` forbids hardcoded
 * colours; each class here resolves through the design system's state palette, so
 * both themes are handled without this file knowing which one is active.
 */

export function HealthBadge({
  state,
  className,
}: {
  state: HealthState;
  className?: string;
}) {
  const t = useTranslations("monitoring.states");

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium",
        HEALTH_STATE_CLASSES[state],
        className,
      )}
    >
      {t(state)}
    </span>
  );
}

export function SeverityBadge({
  severity,
  className,
}: {
  severity: AlertSeverity;
  className?: string;
}) {
  const t = useTranslations("monitoring.severities");

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium",
        ALERT_SEVERITY_CLASSES[severity],
        className,
      )}
    >
      {t(severity)}
    </span>
  );
}
