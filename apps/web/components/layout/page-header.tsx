"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

/**
 * Page header block.
 *
 * Renders the large page title, an optional description, and an optional slot
 * for page-level actions (buttons, filters). Used at the top of feature pages
 * beneath the app header's breadcrumbs.
 *
 * **A Client Component since `21-localization.md`, and the reason is where the
 * language lives.** The platform deliberately has *no locale in the URL* — the
 * language is a **setting**, resolved from an authenticated request and applied by
 * `components/i18n/locale-provider.tsx` — so the server rendering `<html>` does
 * not know it and cannot translate a title. Every page under `(protected)` stays a
 * Server Component; only this three-element block crosses into the client, which
 * is the smallest boundary that lets a page title be Arabic.
 *
 * **Two ways to name a page, and they are not interchangeable.** `titleKey` is for
 * a *fixed* page — Cases, Documents, Settings — whose heading is a sentence in the
 * catalogues. `title` is for a page named after a **row**: a case's own title, a
 * user's own name. Those are data rather than copy, they arrive already resolved
 * from the API, and a translation key for them could not exist. A component that
 * passed both would be describing a page two ways, so the key wins and the literal
 * is the fallback.
 */
export function PageHeader({
  title,
  titleKey,
  description,
  descriptionKey,
  actions,
  className,
}: {
  /** An already-resolved heading — the name of a row, not of a page. */
  title?: string;
  /** A key under the `pages` namespace, for a page with a fixed name. */
  titleKey?: string;
  /** An already-resolved subtitle. */
  description?: string;
  /** A key under the `pages` namespace. */
  descriptionKey?: string;
  actions?: React.ReactNode;
  className?: string;
}) {
  const t = useTranslations("pages");

  const heading = titleKey ? t(titleKey) : (title ?? "");
  const subheading = descriptionKey ? t(descriptionKey) : description;

  return (
    <div
      className={cn(
        "flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between",
        className,
      )}
    >
      <div className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">
          {heading}
        </h1>
        {subheading ? (
          <p className="text-sm text-muted-foreground">{subheading}</p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex shrink-0 items-center gap-2">{actions}</div>
      ) : null}
    </div>
  );
}
