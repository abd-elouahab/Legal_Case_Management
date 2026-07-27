import { routeLabels } from "@/config/navigation";
import { ROUTES } from "@/lib/routes";

export interface Breadcrumb {
  /** Display label for the segment. */
  label: string;
  /** Cumulative href up to and including this segment. */
  href: string;
  /** True for the final segment (current page — rendered non-interactive). */
  isCurrent: boolean;
}

/**
 * Convert an `unknown-slug` path segment into a readable Title Case label as a
 * fallback when the segment is not present in {@link routeLabels} (e.g. future
 * dynamic ids).
 */
function humanizeSegment(segment: string): string {
  const decoded = decodeURIComponent(segment);
  return decoded
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

/**
 * Build breadcrumb items from a pathname.
 *
 * The protected app is always rooted at the Dashboard, so the trail starts
 * there unless the current page already is the dashboard. Pure and
 * side-effect-free so it can run on the server or the client.
 *
 * @example
 * buildBreadcrumbs("/cases") // Dashboard › Cases
 */
export function buildBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathname.split("/").filter(Boolean);

  if (segments.length === 0) {
    return [{ label: routeLabels[ROUTES.dashboard], href: ROUTES.dashboard, isCurrent: true }];
  }

  const crumbs: Breadcrumb[] = [];
  const isDashboardRoot = pathname === ROUTES.dashboard;

  // Anchor every trail at the Dashboard, except when it is the current page.
  if (!isDashboardRoot) {
    crumbs.push({
      label: routeLabels[ROUTES.dashboard],
      href: ROUTES.dashboard,
      isCurrent: false,
    });
  }

  let cumulative = "";
  segments.forEach((segment, index) => {
    cumulative += `/${segment}`;
    const isCurrent = index === segments.length - 1;
    crumbs.push({
      label: routeLabels[cumulative] ?? humanizeSegment(segment),
      href: cumulative,
      isCurrent,
    });
  });

  return crumbs;
}
