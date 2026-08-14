import type { LucideIcon } from "lucide-react";
import {
  Activity,
  Bell,
  Bot,
  FileBarChart,
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
 * **Every label here is a translation key**, not a sentence. `21-localization.md`
 * forbids hardcoded user-facing text, and a navigation item is the clearest case
 * for it: the same destination appears in the sidebar, in a tooltip, and in the
 * breadcrumb trail, so a label written three times would be three places to
 * translate. The keys resolve against the `navigation` namespace in
 * `messages/*.json`, and the components that render them are the only place a
 * word appears.
 */

export interface NavItem {
  /** Translation key under `navigation.items`. */
  titleKey: string;
  /** Target path — always a {@link ROUTES} value. */
  href: string;
  /** Lucide icon rendered at `h-5 w-5` per the UI context. */
  icon: LucideIcon;
  /** Translation key under `navigation.descriptions`, for tooltips. */
  descriptionKey?: string;
  /**
   * Permission requirement for this destination. Omitted means every
   * authenticated user may see it.
   */
  access?: AccessRule;
}

export interface NavSection {
  /** Translation key under `navigation.sections`. Omitted for the primary group. */
  titleKey?: string;
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
        titleKey: "dashboard",
        href: ROUTES.dashboard,
        icon: LayoutDashboard,
        descriptionKey: "dashboard",
        // The landing page for every role; its widgets gate themselves.
      },
      {
        titleKey: "cases",
        href: ROUTES.cases,
        icon: Scale,
        descriptionKey: "cases",
        access: { permission: PERMISSION.casesView },
      },
      {
        titleKey: "documents",
        href: ROUTES.documents,
        icon: FileText,
        descriptionKey: "documents",
        access: { permission: PERMISSION.documentsView },
      },
      {
        titleKey: "search",
        href: ROUTES.search,
        icon: Search,
        descriptionKey: "search",
        // Semantic search over the case file. Placed directly after Documents
        // because it searches exactly what that page lists — and gated on its
        // own capability rather than on `documents:view`, since retrieval has a
        // cost (a query embedding per request) that reading a list does not.
        // Which passages a caller reaches is decided by the API, per case
        // assignment.
        access: { permission: PERMISSION.searchQuery },
      },
      {
        titleKey: "users",
        href: ROUTES.users,
        icon: UsersRound,
        descriptionKey: "users",
        // User Management: administrators only, by capability rather than role.
        access: { permission: PERMISSION.usersView },
      },
      {
        titleKey: "lawyers",
        href: ROUTES.lawyers,
        icon: Users,
        descriptionKey: "lawyers",
        // Case-facing view of lawyers and their assignments (a later feature).
        // Distinct from Users, which manages accounts across all three roles.
        access: { permission: PERMISSION.usersView },
      },
      {
        titleKey: "courtUpdates",
        href: ROUTES.courtUpdates,
        icon: Gavel,
        descriptionKey: "courtUpdates",
        // Court activity is part of the case record, so it follows case access.
        access: { permission: PERMISSION.casesView },
      },
      {
        titleKey: "reports",
        href: ROUTES.reports,
        icon: FileBarChart,
        descriptionKey: "reports",
        // Gated on `reports:view`, which is *reading your own history* rather
        // than a row grant — every read on the API is keyed by the requester, so
        // a lawyer holding this sees the reports they generated and nobody
        // else's. Generating additionally needs `reports:generate` and
        // `ai:generate-report`, which the page's own controls check: a
        // destination nobody can generate in is still worth reaching, because
        // the reports already there are readable and exportable.
        access: { permission: PERMISSION.reportsView },
      },
    ],
  },
  {
    titleKey: "workspace",
    items: [
      {
        titleKey: "notifications",
        href: ROUTES.notifications,
        icon: Bell,
        descriptionKey: "notifications",
        access: { permission: PERMISSION.notificationsView },
      },
      {
        titleKey: "aiAssistant",
        href: ROUTES.aiAssistant,
        icon: Bot,
        descriptionKey: "aiAssistant",
        access: { permission: PERMISSION.aiChat },
      },
      {
        titleKey: "settings",
        href: ROUTES.settings,
        icon: Settings,
        descriptionKey: "settings",
        access: { permission: PERMISSION.settingsView },
      },
      {
        titleKey: "monitoring",
        href: ROUTES.monitoring,
        icon: Activity,
        descriptionKey: "monitoring",
        // The platform's operational state, and the **only destination in this
        // config that is not about the platform's subject matter**. It is gated
        // on `monitoring:view`, which administrators alone hold — `22-monitoring.md`
        // is explicit that regular users must never reach a monitoring endpoint —
        // so for a lawyer or a court representative this item does not exist:
        // the sidebar filter and the route guard read this same rule, and the API
        // refuses independently of both.
        access: { permission: PERMISSION.monitoringView },
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
 * Path → translation key, used by the breadcrumb helper.
 *
 * Includes every navigation destination plus the shell-only routes that never
 * appear in the sidebar. **Keys rather than labels**, so a breadcrumb and the
 * sidebar item it corresponds to cannot drift — and so `lib/breadcrumbs.ts` stays
 * a pure function with no translator in it, which is what lets it be unit-tested
 * without a provider.
 */
export const routeLabelKeys: Record<string, string> = {
  [ROUTES.dashboard]: "dashboard",
  ...Object.fromEntries(navItems.map((item) => [item.href, item.titleKey])),
  [ROUTES.accessDenied]: "accessDenied",
  [ROUTES.login]: "signIn",
};
