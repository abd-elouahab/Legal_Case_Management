import * as React from "react";
import { Scale } from "lucide-react";

/**
 * Authentication (public) layout.
 *
 * Shared layout for unauthenticated, public pages (e.g. sign-in). It is a
 * standalone centered surface — no sidebar or app header — so it stays isolated
 * from the protected shell. No authentication logic lives here.
 */
export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-8 bg-background px-4 py-12">
      <div className="flex items-center gap-2.5">
        <span className="flex size-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Scale className="h-6 w-6" />
        </span>
        <div className="flex flex-col leading-tight">
          <span className="text-sm font-semibold text-foreground">
            Legal Platform
          </span>
          <span className="text-xs text-muted-foreground">Case Management</span>
        </div>
      </div>
      <div className="w-full max-w-sm">{children}</div>
    </div>
  );
}
