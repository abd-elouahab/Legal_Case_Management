"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";

import { logout as logoutRequest } from "@/lib/api/auth";
import { ROUTES } from "@/lib/routes";
import { useSessionStore } from "@/stores/session-store";

/**
 * Sign the user out.
 *
 * Revokes the session server-side, clears local session state, drops every
 * cached query (so one user's data can never surface for the next), and returns
 * to the login page.
 *
 * Always resolves: a failed server call still clears local state, because the
 * user asked to sign out.
 */
export function useLogout(): { logout: () => Promise<void>; isPending: boolean } {
  const router = useRouter();
  const queryClient = useQueryClient();
  const clearSession = useSessionStore((state) => state.clearSession);
  const [isPending, setIsPending] = React.useState(false);

  const logout = React.useCallback(async () => {
    setIsPending(true);
    try {
      await logoutRequest();
    } finally {
      clearSession();
      queryClient.clear();
      router.replace(ROUTES.login);
      setIsPending(false);
    }
  }, [clearSession, queryClient, router]);

  return { logout, isPending };
}
