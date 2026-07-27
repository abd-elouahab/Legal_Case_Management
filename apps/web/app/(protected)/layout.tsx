import * as React from "react";

import { RequireAuth } from "@/components/auth/require-auth";
import { AppShell } from "@/components/layout/app-shell";

/**
 * Protected layout.
 *
 * Gates every authenticated feature page behind {@link RequireAuth}, then wraps
 * it in the reusable {@link AppShell} (sidebar + header + main + footer).
 *
 * Guarding at the layout level means each new page under `(protected)` is
 * protected by construction — there is no per-page check to forget. The Next.js
 * proxy (`proxy.ts`) additionally redirects before render when no refresh cookie
 * is present, so unauthenticated visitors never download the shell.
 */
export default function ProtectedLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <RequireAuth>
      <AppShell>{children}</AppShell>
    </RequireAuth>
  );
}
