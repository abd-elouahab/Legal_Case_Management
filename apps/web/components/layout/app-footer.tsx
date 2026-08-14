"use client";

import { useTranslations } from "next-intl";

/**
 * Application footer.
 *
 * Minimal shell footer beneath the main content. Purely presentational — the
 * year is computed at render time; there is no business logic here.
 */
export function AppFooter() {
  const t = useTranslations("shell.footer");
  const year = new Date().getFullYear();

  return (
    <footer className="border-t border-border px-4 py-4 sm:px-6">
      <div className="mx-auto flex w-full max-w-7xl flex-col items-center justify-between gap-2 text-xs text-muted-foreground sm:flex-row">
        {/* The year is interpolated rather than concatenated, so a locale that
            writes it differently — or puts it elsewhere in the sentence — can. */}
        <p>{t("copyright", { year })}</p>
        <p>{t("tagline")}</p>
      </div>
    </footer>
  );
}
