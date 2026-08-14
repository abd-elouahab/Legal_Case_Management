"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";

import { LoadingState } from "@/components/shared/loading-state";
import { useSession } from "@/hooks/use-session";
import { ROUTES } from "@/lib/routes";

/**
 * Client-side guard for protected routes.
 *
 * Redirects to `/login` once the session is known to be absent. This is the
 * authoritative check: the Next.js proxy can only see *whether* a refresh cookie
 * exists, not whether it is still valid, so a revoked or expired session is
 * caught here.
 *
 * While the session is still resolving it renders a loading state instead of
 * redirecting — otherwise every page reload would bounce a signed-in user to the
 * login page before the refresh call completes.
 */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { status } = useSession();
  const t = useTranslations("auth.states");

  React.useEffect(() => {
    if (status === "unauthenticated") {
      router.replace(ROUTES.login);
    }
  }, [status, router]);

  if (status === "authenticated") return <>{children}</>;

  // `loading` (still resolving) and `unauthenticated` (redirect in flight) both
  // show a pending state; protected content is never rendered unauthenticated.
  return (
    <div className="flex min-h-svh items-center justify-center">
      <LoadingState
        label={status === "loading" ? t("restoringSession") : t("redirecting")}
      />
    </div>
  );
}
