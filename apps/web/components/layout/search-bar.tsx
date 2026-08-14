"use client";

import { Search } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

/**
 * Global search bar — PLACEHOLDER.
 *
 * Renders the search affordance in the top navigation. Non-functional in this
 * spec (no querying, no API): semantic search across cases and documents is
 * wired up in a later feature. Kept accessible and keyboard-focusable so the
 * shell's layout and focus order are correct.
 */
export function SearchBar({ className }: { className?: string }) {
  const t = useTranslations("shell.search");

  return (
    <div className={cn("relative w-full max-w-sm", className)}>
      <Search
        className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
        aria-hidden="true"
      />
      <input
        type="search"
        disabled
        aria-label={t("label")}
        placeholder={t("placeholder")}
        className="h-9 w-full rounded-md border border-input bg-muted/40 ps-9 pe-16 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed"
      />
      <kbd className="pointer-events-none absolute end-2.5 top-1/2 hidden -translate-y-1/2 select-none items-center gap-1 rounded border border-border bg-background px-1.5 font-mono text-[10px] font-medium text-muted-foreground sm:inline-flex">
        ⌘K
      </kbd>
    </div>
  );
}
