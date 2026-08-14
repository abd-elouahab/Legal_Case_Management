"use client";

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SessionProvider } from "@/components/auth/session-provider";
import { LocaleProvider } from "@/components/i18n/locale-provider";
import { RealtimeProvider } from "@/components/realtime/realtime-provider";
import { SettingsProvider } from "@/components/settings/settings-provider";
import { ThemeProvider } from "@/components/theme-provider";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";

/**
 * Global application providers.
 *
 * Composes every cross-cutting client provider in one place so the root layout
 * stays a thin Server Component. Order (outer → inner):
 *
 *   ThemeProvider (next-themes) → QueryClientProvider (TanStack Query)
 *     → SessionProvider (auth lifecycle) → SettingsProvider (preferences)
 *       → LocaleProvider (language, direction, translations)
 *         → RealtimeProvider (live updates)
 *           → TooltipProvider (Radix) → children (+ Toaster overlay)
 *
 * `SessionProvider` sits inside `QueryClientProvider` because signing out clears
 * the query cache, and above everything that reads the session so a single
 * session restore serves the whole tree.
 *
 * `SettingsProvider` sits between the session and everything that renders,
 * because it needs a session to load the caller's settings and everything below
 * it wants the result: the resolved theme, the language a notification feed is
 * rendered in, and the date and time formats every timestamp on the platform is
 * written with.
 *
 * `LocaleProvider` sits immediately inside `SettingsProvider`, and the order is
 * forced: the language is a *setting*, so the catalogue cannot be chosen until
 * the preferences are known. It sits **above everything that renders text**, which
 * is the whole application — a component that called `useTranslations` from
 * outside it would be a component whose copy could not be translated. It also
 * owns `dir` and `lang` on the document element, so every screen below inherits
 * direction rather than deciding it.
 *
 * **It is above `RealtimeProvider` rather than below it**, which is only
 * interesting because it looks arbitrary: the connection indicator renders
 * user-facing text ("Updates paused"), so the transport has to sit inside the
 * language rather than beside it.
 *
 * `RealtimeProvider` sits inside both, and needs both: it opens the socket only
 * once a session exists (the connection authenticates with that session's access
 * token) and turns every event it receives into an invalidation on that query
 * client. It renders nothing and blocks nothing — a deployment with the channel
 * off, or a browser that cannot open a socket, leaves every screen working
 * exactly as it did before, on the polling each feature already does.
 *
 * **The theme is no longer forced.** `00-design-system.md` shipped the platform
 * dark-only and `ui-context.md` has always said it *"supports both Dark Mode and
 * Light Mode, with Dark Mode as the default experience"*; `20-settings.md`'s
 * Appearance section is what closed the gap, so `globals.css` now carries a light
 * palette beside the dark one and this provider offers all three choices. Dark
 * stays the default, which is what both documents ask for, and `enableSystem`
 * makes "System" a real third answer rather than a relabelled default.
 *
 * The stored preference lives in **two** places on purpose, and they answer
 * different questions. `next-themes` keeps its own copy in `localStorage`, which
 * is what paints the correct theme before React hydrates — a server that had to
 * be asked would flash the wrong one on every page load. The Settings API is the
 * durable copy, which is what makes the choice survive a new device, the spec's
 * *"settings should survive ... device changes"*. `SettingsProvider` reconciles
 * them: the server's answer wins once it arrives.
 */

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Sensible defaults for a data-heavy collaborative app.
        staleTime: 60 * 1000,
        refetchOnWindowFocus: false,
        retry: 1,
      },
    },
  });
}

let browserQueryClient: QueryClient | undefined;

/**
 * One client on the server per request, and a single lazily-created client on
 * the browser (survives Fast Refresh / re-renders).
 */
function getQueryClient(): QueryClient {
  if (typeof window === "undefined") return makeQueryClient();
  if (!browserQueryClient) browserQueryClient = makeQueryClient();
  return browserQueryClient;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const queryClient = getQueryClient();

  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="dark"
      enableSystem
      disableTransitionOnChange
    >
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <SettingsProvider>
            <LocaleProvider>
              <RealtimeProvider>
                <TooltipProvider delayDuration={200}>{children}</TooltipProvider>
              </RealtimeProvider>
            </LocaleProvider>
          </SettingsProvider>
        </SessionProvider>
        <Toaster />
      </QueryClientProvider>
    </ThemeProvider>
  );
}
