import type { LucideIcon } from "lucide-react";
import {
  Bell,
  Bot,
  FileText,
  Gavel,
  LayoutDashboard,
  Scale,
  Search,
  Settings,
  Users,
  UsersRound,
} from "lucide-react";

import { ROUTES } from "@/lib/routes";
import { PERMISSION, type AccessRule } from "@/types/authorization";

/**
 * Navigation configuration.
 *
 * Single, reusable source for the sidebar, the breadcrumb label map, **and the
 * per-route access rules**. Every item points at a {@link ROUTES} constant so
 * paths stay consistent across the shell.
 *
 * Declaring `access` here rather than inside components is what keeps the
 * sidebar and the route guard in agreement: an item the user cannot open is
 * also an item they are never shown, because both read the same rule. Adding a
 * navigation destination therefore means declaring its permission once, here.
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
  /**
   * Permission requirement for this destination. Omitted means every
   * authenticated user may see it.
   */
  access?: AccessRule;
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
        // The landing page for every role; its widgets gate themselves.
      },
      {
        title: "Cases",
        href: ROUTES.cases,
        icon: Scale,
        description: "Manage legal cases",
        access: { permission: PERMISSION.casesView },
      },
      {
        title: "Documents",
        href: ROUTES.documents,
        icon: FileText,
        description: "Legal documents and files",
        access: { permission: PERMISSION.documentsView },
      },
      {
        title: "Search",
        href: ROUTES.search,
        icon: Search,
        description: "Find passages across indexed documents",
        // Semantic search over the case file. Placed directly after Documents
        // because it searches exactly what that page lists — and gated on its
        // own capability rather than on `documents:view`, since retrieval has a
        // cost (a query embedding per request) that reading a list does not.
        // Which passages a caller reaches is decided by the API, per case
        // assignment.
        access: { permission: PERMISSION.searchQuery },
      },
      {
        title: "Users",
        href: ROUTES.users,
        icon: UsersRound,
        description: "Platform accounts and roles",
        // User Management: administrators only, by capability rather than role.
        access: { permission: PERMISSION.usersView },
      },
      {
        title: "Lawyers",
        href: ROUTES.lawyers,
        icon: Users,
        description: "Assigned lawyers",
        // Case-facing view of lawyers and their assignments (a later feature).
        // Distinct from Users, which manages accounts across all three roles.
        access: { permission: PERMISSION.usersView },
      },
      {
        title: "Court Updates",
        href: ROUTES.courtUpdates,
        icon: Gavel,
        description: "Hearings and court decisions",
        // Court activity is part of the case record, so it follows case access.
        access: { permission: PERMISSION.casesView },
      },
      {
        title: "Reports",
        href: ROUTES.reports,
        icon: FileText,
        description: "Generated legal reports",
        access: { permission: PERMISSION.reportsView },
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
        access: { permission: PERMISSION.notificationsView },
      },
      {
        title: "AI Assistant",
        href: ROUTES.aiAssistant,
        icon: Bot,
        description: "Legal document Q&A and summaries",
        access: { permission: PERMISSION.aiChat },
      },
      {
        title: "Settings",
        href: ROUTES.settings,
        icon: Settings,
        description: "Workspace preferences",
        access: { permission: PERMISSION.settingsView },
      },
    ],
  },
];

/** Flat list of every navigation item, useful for lookups. */
export const navItems: NavItem[] = sidebarNavigation.flatMap(
  (section) => section.items,
);

/**
 * Path → access rule, derived from the navigation config above.
 *
 * Derived rather than written out a second time: a route's requirement is
 * declared once, on its nav item, and both the sidebar filter and the route
 * guard read it from here. That is what makes "the sidebar hides what the guard
 * would block" true by construction instead of by convention.
 *
 * Routes absent from this map carry no permission requirement — the Dashboard
 * and the Unauthorized page itself, which every signed-in user must be able to
 * reach.
 */
export const routeAccessRules: ReadonlyArray<{ path: string; access: AccessRule }> =
  navItems
    .filter((item): item is NavItem & { access: AccessRule } => item.access !== undefined)
    .map((item) => ({ path: item.href, access: item.access }));

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
