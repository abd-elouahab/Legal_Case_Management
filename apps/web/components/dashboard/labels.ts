import type { LucideIcon } from "lucide-react";
import {
  Activity,
  Bell,
  Bot,
  CalendarClock,
  CalendarDays,
  FileBarChart,
  FileStack,
  FileText,
  Gauge,
  HardDrive,
  LayoutDashboard,
  ListChecks,
  MessagesSquare,
  Scale,
  ScanText,
  Sparkles,
  Upload,
  UsersRound,
  Zap,
} from "lucide-react";

import { ROUTES } from "@/lib/routes";
import type { QuickActionKey, WidgetKey } from "@/types/dashboard";

/**
 * Dashboard icons and destinations.
 *
 * **The API sends no words, and neither does this file any more.** A widget, a
 * metric, and a bucket each arrive carrying a stable `key`; the *sentences* live
 * in `apps/web/messages/*.json` under the `dashboard` namespace, and the URLs
 * live in `lib/routes.ts`. Two reasons, and both are requirements rather than
 * taste: `code-standards.md` forbids hardcoded user-facing strings anywhere they
 * cannot be translated — a module constant is exactly such a place — and
 * `19-dashboard-analytics.md` requires navigation to stay a frontend concern, so
 * a quick action names an *action* and this file decides where it goes.
 *
 * What remains here is the half of a widget's presentation that **is not
 * language**: which glyph it wears and which page its "see all" opens. Those are
 * identical in English, French, and Arabic, so putting them in a catalogue would
 * be three copies of one fact.
 *
 * **An unknown key still renders rather than throwing.** A label is now looked up
 * with `useTranslations`, and a key no catalogue defines resolves through
 * `getMessageFallback` to a humanized form of itself — the same "support
 * additional widgets later" property the old `humanizeKey` gave, moved to the one
 * place the whole application already falls back in.
 */

// --------------------------------------------------------------------------- //
// Widgets
// --------------------------------------------------------------------------- //

export interface WidgetChrome {
  icon: LucideIcon;
  /** Where "see all" goes, when the widget has a fuller view behind it. */
  href?: string;
}

/** Icon and destination for every widget. Its words are `dashboard.widgets.<key>`. */
export const WIDGET_CHROME: Record<WidgetKey, WidgetChrome> = {
  quick_actions: { icon: Zap },
  notifications: { icon: Bell, href: ROUTES.notifications },
  recent_activity: { icon: Activity },
  my_cases: { icon: Scale, href: ROUTES.cases },
  recent_cases: { icon: Scale, href: ROUTES.cases },
  case_status_overview: { icon: ListChecks, href: ROUTES.cases },
  case_analytics: { icon: Gauge },
  upcoming_hearings: { icon: CalendarClock, href: ROUTES.courtUpdates },
  hearing_calendar: { icon: CalendarDays, href: ROUTES.courtUpdates },
  recent_documents: { icon: FileText, href: ROUTES.documents },
  ocr_status: { icon: ScanText, href: ROUTES.documents },
  document_analytics: { icon: FileStack },
  ai_reports: { icon: FileBarChart, href: ROUTES.reports },
  recent_conversations: { icon: MessagesSquare, href: ROUTES.aiAssistant },
  ai_analytics: { icon: Sparkles },
  timeline_activity: { icon: Activity },
  storage_usage: { icon: HardDrive },
  active_users: { icon: UsersRound, href: ROUTES.users },
  processing_queues: { icon: Gauge },
};

/**
 * Which widgets carry a description under their title.
 *
 * A set rather than an optional string, because "does this widget have a
 * subtitle" is a layout decision that must not change with the language — a
 * translator who left `description` out of one catalogue would otherwise silently
 * restyle the card.
 */
export const WIDGETS_WITH_DESCRIPTION = new Set<WidgetKey>([
  "quick_actions",
  "notifications",
  "recent_activity",
  "my_cases",
  "case_status_overview",
  "upcoming_hearings",
  "ocr_status",
  "ai_reports",
  "ai_analytics",
  "timeline_activity",
  "storage_usage",
  "active_users",
  "processing_queues",
]);

// --------------------------------------------------------------------------- //
// Quick actions
// --------------------------------------------------------------------------- //

export interface QuickActionChrome {
  icon: LucideIcon;
  href: string;
}

/**
 * Where each quick action goes, and what it looks like.
 *
 * The server decides *whether* a caller may use one — against the permissions the
 * destination endpoint itself requires — and this decides what it looks like. The
 * split is why a shortcut can never appear for somebody the destination would
 * refuse. Its label is `dashboard.quickActions.<key>`.
 */
export const QUICK_ACTION_CHROME: Record<QuickActionKey, QuickActionChrome> = {
  create_case: { icon: Scale, href: ROUTES.cases },
  upload_document: { icon: Upload, href: ROUTES.documents },
  generate_report: { icon: FileBarChart, href: ROUTES.reports },
  open_assistant: { icon: Bot, href: ROUTES.aiAssistant },
  view_calendar: { icon: CalendarDays, href: ROUTES.courtUpdates },
};

/** The dashboard's own icon, for the page header and the empty state. */
export const DASHBOARD_ICON: LucideIcon = LayoutDashboard;
