import type { LucideIcon } from "lucide-react";
import {
  Bell,
  Bot,
  FileText,
  Gavel,
  LayoutDashboard,
  Scale,
  Settings,
  Users,
} from "lucide-react";

import { ROUTES } from "@/lib/routes";

/**
 * Navigation configuration.
 *
 * Single, reusable source for the sidebar and the breadcrumb label map. Every
 * item points at a {@link ROUTES} constant so paths stay consistent across the
 * shell.
 *
 * `title` values are placeholder copy for now. When localization (next-intl)
 * lands they become translation keys — the structure below is designed so only
 * the `title` field needs to change.
 */

export interface NavItem {
  /** Human-readable label (future: i18n key). */
  title: string;
  /** Target path — always a {@link ROUTES} value. */
  href: string;
  /** Lucide icon rendered at `h-5 w-5` per the UI context. */
  icon: LucideIcon;
  /** Optional short description for tooltips / collapsed states. */
  description?: string;
}

export interface NavSection {
  /** Section label (future: i18n key). May be omitted for the primary group. */
  title?: string;
  items: NavItem[];
}

/**
 * Primary sidebar navigation. Order and items match the platform structure
 * documented in `ui-context.md`.
 */
export const sidebarNavigation: NavSection[] = [
  {
    items: [
      {
        title: "Dashboard",
        href: ROUTES.dashboard,
        icon: LayoutDashboard,
        description: "Key case metrics at a glance",
      },
      {
        title: "Cases",
        href: ROUTES.cases,
        icon: Scale,
        description: "Manage legal cases",
      },
      {
        title: "Documents",
        href: ROUTES.documents,
        icon: FileText,
        description: "Legal documents and files",
      },
      {
        title: "Lawyers",
        href: ROUTES.lawyers,
        icon: Users,
        description: "Assigned lawyers",
      },
      {
        title: "Court Updates",
        href: ROUTES.courtUpdates,
        icon: Gavel,
        description: "Hearings and court decisions",
      },
      {
        title: "Reports",
        href: ROUTES.reports,
        icon: FileText,
        description: "Generated legal reports",
      },
    ],
  },
  {
    title: "Workspace",
    items: [
      {
        title: "Notifications",
        href: ROUTES.notifications,
        icon: Bell,
        description: "Alerts and reminders",
      },
      {
        title: "AI Assistant",
        href: ROUTES.aiAssistant,
        icon: Bot,
        description: "Legal document Q&A and summaries",
      },
      {
        title: "Settings",
        href: ROUTES.settings,
        icon: Settings,
        description: "Workspace preferences",
      },
    ],
  },
];

/** Flat list of every navigation item, useful for lookups. */
export const navItems: NavItem[] = sidebarNavigation.flatMap(
  (section) => section.items,
);

/**
 * Path → label lookup used by the breadcrumb helper. Includes every navigation
 * destination plus shell-only routes that never appear in the sidebar.
 */
export const routeLabels: Record<string, string> = {
  [ROUTES.dashboard]: "Dashboard",
  ...Object.fromEntries(navItems.map((item) => [item.href, item.title])),
  [ROUTES.accessDenied]: "Access Denied",
  [ROUTES.login]: "Sign In",
};
