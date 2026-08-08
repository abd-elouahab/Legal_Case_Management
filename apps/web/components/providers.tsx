"use client";

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SessionProvider } from "@/components/auth/session-provider";
import { RealtimeProvider } from "@/components/realtime/realtime-provider";
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
 *     → SessionProvider (auth lifecycle) → RealtimeProvider (live updates)
 *       → TooltipProvider (Radix) → children (+ Toaster overlay)
 *
 * `SessionProvider` sits inside `QueryClientProvider` because signing out clears
 * the query cache, and above everything that reads the session so a single
 * session restore serves the whole tree.
 *
 * `RealtimeProvider` sits inside both, and needs both: it opens the socket only
 * once a session exists (the connection authenticates with that session's access
 * token) and turns every event it receives into an invalidation on that query
 * client. It renders nothing and blocks nothing — a deployment with the channel
 * off, or a browser that cannot open a socket, leaves every screen working
 * exactly as it did before, on the polling each feature already does.
 *
 * The platform is dark-only for now; the theme is forced in the root layout.
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
      forcedTheme="dark"
      enableSystem={false}
      disableTransitionOnChange
    >
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <RealtimeProvider>
            <TooltipProvider delayDuration={200}>{children}</TooltipProvider>
          </RealtimeProvider>
        </SessionProvider>
        <Toaster />
      </QueryClientProvider>
    </ThemeProvider>
  );
}
