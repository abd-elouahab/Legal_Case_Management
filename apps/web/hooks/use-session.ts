"use client";

import { useSessionStore } from "@/stores/session-store";
import type { SessionStatus } from "@/types/auth";
import type { SessionUser } from "@/types/user";

/**
 * Read the current session.
 *
 * `status` distinguishes "still resolving" from "definitely signed out", which
 * route guards need in order to avoid redirecting during the initial session
 * restore.
 */
export function useSession(): {
  user: SessionUser | null;
  status: SessionStatus;
  isAuthenticated: boolean;
  isLoading: boolean;
} {
  const user = useSessionStore((state) => state.user);
  const status = useSessionStore((state) => state.status);

  return {
    user,
    status,
    isAuthenticated: status === "authenticated",
    isLoading: status === "loading",
  };
}
