"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { LoadingState } from "@/components/shared/loading-state";
import { useSession } from "@/hooks/use-session";
import { DEFAULT_AUTHENTICATED_ROUTE } from "@/lib/routes";

/**
 * Guard for public auth pages.
 *
 * Implements "authenticated users should never see the login page again": once a
 * session is established, visiting `/login` forwards to the dashboard.
 *
 * The form renders immediately — including while the session is still being
 * restored — because `proxy.ts` already redirects anyone holding a refresh cookie
 * away from `/login` at the edge. Blocking on the session check here would make
 * every anonymous visitor stare at a spinner through a `/auth/refresh` round trip
 * that is *expected* to fail, on the app's most important public page.
 *
 * The redirect below still covers the case the proxy cannot: a session
 * established in this tab after the page was already open.
 */
export function RedirectIfAuthenticated({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { status } = useSession();

  React.useEffect(() => {
    if (status === "authenticated") {
      router.replace(DEFAULT_AUTHENTICATED_ROUTE);
    }
  }, [status, router]);

  if (status === "authenticated") {
    return <LoadingState label="Taking you to your dashboard…" />;
  }

  return <>{children}</>;
}
