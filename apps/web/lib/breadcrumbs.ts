import { routeLabelKeys } from "@/config/navigation";
import { ROUTES } from "@/lib/routes";

export interface Breadcrumb {
  /**
   * Translation key under `navigation.routes`, or `null` for a segment the
   * navigation config does not name — a case id, a user id, or any future
   * dynamic route.
   *
   * **Keys rather than labels, so this function stays pure.** Translating here
   * would need a translator, which would make a breadcrumb impossible to compute
   * outside a React tree and impossible to unit-test without a provider. The
   * component resolves it, and falls back to `fallbackLabel` when there is no
   * key — which is `21-localization.md`'s *"the application should never expose
   * translation keys to users"* applied to the one place a raw path segment
   * could have reached a screen.
   */
  labelKey: string | null;
  /** Humanized form of the raw segment, used when `labelKey` is `null`. */
  fallbackLabel: string;
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
 * buildBreadcrumbs("/cases") // dashboard › cases
 */
export function buildBreadcrumbs(pathname: string): Breadcrumb[] {
  const segments = pathname.split("/").filter(Boolean);

  if (segments.length === 0) {
    return [
      {
        labelKey: routeLabelKeys[ROUTES.dashboard],
        fallbackLabel: "Dashboard",
        href: ROUTES.dashboard,
        isCurrent: true,
      },
    ];
  }

  const crumbs: Breadcrumb[] = [];
  const isDashboardRoot = pathname === ROUTES.dashboard;

  // Anchor every trail at the Dashboard, except when it is the current page.
  if (!isDashboardRoot) {
    crumbs.push({
      labelKey: routeLabelKeys[ROUTES.dashboard],
      fallbackLabel: "Dashboard",
      href: ROUTES.dashboard,
      isCurrent: false,
    });
  }

  let cumulative = "";
  segments.forEach((segment, index) => {
    cumulative += `/${segment}`;
    const isCurrent = index === segments.length - 1;
    crumbs.push({
      labelKey: routeLabelKeys[cumulative] ?? null,
      fallbackLabel: humanizeSegment(segment),
      href: cumulative,
      isCurrent,
    });
  });

  return crumbs;
}
