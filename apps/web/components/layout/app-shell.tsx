import * as React from "react";

import { AppFooter } from "@/components/layout/app-footer";
import { AppHeader } from "@/components/layout/app-header";
import { AppSidebar } from "@/components/layout/app-sidebar";

/**
 * Protected application shell.
 *
 * Assembles the responsive layout used by every authenticated page: persistent
 * sidebar (drawer on mobile) + sticky top header + scrollable main content +
 * footer. Pages are rendered into the `<main>` landmark.
 *
 * Server Component wrapper — the interactive pieces (sidebar, header) are
 * client components composed here.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-svh w-full">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-primary focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-primary-foreground"
      >
        Skip to content
      </a>
      <AppSidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <AppHeader />
        <main id="main-content" className="flex-1">
          {children}
        </main>
        <AppFooter />
      </div>
    </div>
  );
}
