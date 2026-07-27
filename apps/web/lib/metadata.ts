import type { Metadata } from "next";

/**
 * Page metadata helper.
 *
 * The root layout defines a title template (`%s · Legal Case Management
 * Platform`), so page metadata only needs to supply the leading title segment.
 * This helper keeps per-page `metadata` exports terse and consistent.
 *
 * @example
 * export const metadata = createMetadata("Cases", "Manage legal cases");
 */
export function createMetadata(title: string, description?: string): Metadata {
  return {
    title,
    ...(description ? { description } : {}),
  };
}
