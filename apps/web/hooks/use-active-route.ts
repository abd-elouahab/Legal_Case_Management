"use client";

import { usePathname } from "next/navigation";

import { ROUTES } from "@/lib/routes";

/**
 * Active-route matching for navigation highlighting.
 *
 * A nav item is active when the current pathname equals its href, or is nested
 * beneath it (`/cases/123` activates `/cases`). The dashboard matches exactly
 * so it does not stay highlighted for every nested route.
 */
export function useActiveRoute(): (href: string) => boolean {
  const pathname = usePathname();

  return (href: string): boolean => {
    if (href === ROUTES.dashboard) return pathname === ROUTES.dashboard;
    return pathname === href || pathname.startsWith(`${href}/`);
  };
}
