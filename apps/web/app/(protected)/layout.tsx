import * as React from "react";

import { RequireAuth } from "@/components/auth/require-auth";
import { RouteGuard } from "@/components/auth/route-guard";
import { AppShell } from "@/components/layout/app-shell";

/**
 * Protected layout.
 *
 * Gates every authenticated feature page behind {@link RequireAuth}, wraps it in
 * the reusable {@link AppShell} (sidebar + header + main + footer), then applies
 * the route's permission requirement via {@link RouteGuard}.
 *
 * The order is deliberate. Authentication comes first: an anonymous visitor is
 * sent to sign in rather than told they lack permission, since they may well be
 * entitled to the page once signed in. The shell wraps the guard rather than the
 * reverse, so a denied user still sees the navigation and can go somewhere they
 * *can* reach instead of landing on a bare error page.
 *
 * Guarding at the layout level means each new page under `(protected)` is both
 * authenticated and authorized by construction. The Next.js proxy (`proxy.ts`)
 * additionally redirects before render when no refresh cookie is present, and
 * the API authorizes every request independently — these client-side guards
 * decide what is *shown*, never what is *allowed*.
 */
export default function ProtectedLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <RequireAuth>
      <AppShell>
        <RouteGuard>{children}</RouteGuard>
      </AppShell>
    </RequireAuth>
  );
}
