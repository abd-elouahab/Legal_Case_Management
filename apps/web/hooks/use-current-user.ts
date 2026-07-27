"use client";

import { useSessionStore } from "@/stores/session-store";
import type { SessionUser } from "@/types/user";

/**
 * The currently authenticated user, or `null` when there is no session.
 *
 * A thin wrapper over the session store so components depend on a stable hook.
 * Returns `null` while the session is still being restored — use
 * {@link useSession} when you need to distinguish "loading" from "signed out".
 */
export function useCurrentUser(): SessionUser | null {
  return useSessionStore((state) => state.user);
}
