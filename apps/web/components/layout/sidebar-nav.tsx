"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useActiveRoute } from "@/hooks/use-active-route";
import { usePermissions } from "@/hooks/use-permissions";
import { sidebarNavigation } from "@/config/navigation";
import { cn } from "@/lib/utils";

/**
 * Sidebar navigation list.
 *
 * Shared by both the desktop rail and the mobile drawer. Highlights the active
 * route via {@link useActiveRoute}. When `collapsed`, items render icon-only
 * with a tooltip label. `onNavigate` lets the mobile drawer close on selection.
 *
 * Role-aware: an item appears only when the current user satisfies the `access`
 * rule declared for it in `config/navigation.ts`. No permission is named here —
 * the same rules drive the route guard, so the sidebar can never offer a
 * destination the guard would refuse. A section whose items are all hidden
 * disappears with them, rather than leaving an empty heading behind.
 */
export function SidebarNav({
  collapsed = false,
  onNavigate,
}: {
  collapsed?: boolean;
  onNavigate?: () => void;
}) {
  const isActive = useActiveRoute();
  const { allows } = usePermissions();
  // Two namespaces rather than one interpolated path, because `useTranslations`
  // is scoped and a section label and an item label are different vocabularies
  // that happen to sit beside each other.
  const t = useTranslations("navigation.items");
  const tSection = useTranslations("navigation.sections");
  const tA11y = useTranslations("common.a11y");

  const sections = sidebarNavigation
    .map((section) => ({
      ...section,
      items: section.items.filter((item) => item.access === undefined || allows(item.access)),
    }))
    .filter((section) => section.items.length > 0);

  return (
    <nav aria-label={tA11y("primaryNavigation")} className="flex flex-col gap-4">
      {sections.map((section, index) => (
        <div key={section.titleKey ?? `section-${index}`} className="flex flex-col gap-1">
          {section.titleKey && !collapsed ? (
            <p className="px-3 pb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {tSection(section.titleKey)}
            </p>
          ) : null}
          <ul className="flex flex-col gap-1">
            {section.items.map((item) => {
              const active = isActive(item.href);
              const Icon = item.icon;
              const label = t(item.titleKey);

              const link = (
                <Link
                  href={item.href}
                  onClick={onNavigate}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring",
                    collapsed && "justify-center px-0",
                    active
                      ? "bg-sidebar-accent text-sidebar-accent-foreground"
                      : "text-sidebar-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground",
                  )}
                >
                  <Icon className="h-5 w-5 shrink-0" aria-hidden="true" />
                  {!collapsed ? <span className="truncate">{label}</span> : null}
                  {collapsed ? <span className="sr-only">{label}</span> : null}
                </Link>
              );

              return (
                <li key={item.href}>
                  {collapsed ? (
                    <Tooltip>
                      <TooltipTrigger asChild>{link}</TooltipTrigger>
                      <TooltipContent side="right">{label}</TooltipContent>
                    </Tooltip>
                  ) : (
                    link
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}
