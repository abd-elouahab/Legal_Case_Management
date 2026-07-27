import { create } from "zustand";

import type { SessionStatus } from "@/types/auth";
import type { SessionUser } from "@/types/user";

/**
 * Authenticated session state.
 *
 * Holds only the *identity* of the signed-in user plus the session lifecycle
 * status. The access token deliberately lives outside this store (see
 * `lib/api/token-store.ts`) so it is never captured in React state, devtools, or
 * a serialized snapshot.
 *
 * The store starts in `loading`: on first render the app does not yet know
 * whether the httpOnly refresh cookie yields a session. Route guards must treat
 * `loading` as "undecided" and render a pending state rather than redirecting,
 * or a returning user would be bounced to the login page on every reload.
 */

interface SessionState {
  user: SessionUser | null;
  status: SessionStatus;
  /** Record a signed-in user (after login or a successful session restore). */
  setSession: (user: SessionUser) => void;
  /** Mark the session as resolved-and-absent. */
  clearSession: () => void;
  /** Return to the undecided state (used when re-checking the session). */
  setLoading: () => void;
}

export const useSessionStore = create<SessionState>((set) => ({
  user: null,
  status: "loading",
  setSession: (user) => set({ user, status: "authenticated" }),
  clearSession: () => set({ user: null, status: "unauthenticated" }),
  setLoading: () => set({ status: "loading" }),
}));
