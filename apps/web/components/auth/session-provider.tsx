"use client";

import * as React from "react";

import { restoreSession } from "@/lib/api/auth";
import { refreshAccessToken } from "@/lib/api/client";
import { onAccessTokenCleared } from "@/lib/api/token-store";
import { useSessionStore } from "@/stores/session-store";

/**
 * Session lifecycle manager.
 *
 * Runs once near the root of the tree and owns the four session concerns from
 * the Authentication spec:
 *
 * 1. **Initialization** — on mount, exchange the httpOnly refresh cookie for an
 *    access token and load the current user.
 * 2. **Persistence** — because step 1 runs on every load, the session survives a
 *    page refresh without the token ever being written to storage.
 * 3. **Automatic refresh** — a timer renews the access token shortly before it
 *    expires, so a user mid-task never hits an expired-token round trip.
 * 4. **Automatic logout** — if a refresh fails (revoked session, expired refresh
 *    token), local state is cleared and the guards redirect to `/login`.
 *
 * Renders nothing itself; it only manages state.
 */

/**
 * Renew this many milliseconds before the token actually expires, so a slow
 * request cannot race the expiry.
 */
const REFRESH_MARGIN_MS = 60_000;

/** Floor for the refresh timer, guarding against a pathologically short TTL. */
const MIN_REFRESH_DELAY_MS = 15_000;

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const setSession = useSessionStore((state) => state.setSession);
  const clearSession = useSessionStore((state) => state.clearSession);
  const status = useSessionStore((state) => state.status);

  // Access-token lifetime reported by the API, used to schedule the next renewal.
  const [expiresIn, setExpiresIn] = React.useState<number | null>(null);

  // --- 1 & 2. Initialize / restore the session on mount --------------------- //
  React.useEffect(() => {
    let cancelled = false;

    void (async () => {
      try {
        const session = await restoreSession();
        if (cancelled) return;

        if (session) {
          setSession(session.user);
          setExpiresIn(session.expiresIn > 0 ? session.expiresIn : null);
        } else {
          clearSession();
        }
      } catch {
        // Any unexpected failure resolves to "not signed in" rather than leaving
        // the app stuck on a loading screen.
        if (!cancelled) clearSession();
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [setSession, clearSession]);

  // --- 3. Proactively refresh before the access token expires --------------- //
  React.useEffect(() => {
    if (status !== "authenticated") return;

    // The restore path does not report a lifetime; fall back to renewing on the
    // standard 15-minute access-token window.
    const lifetimeMs = (expiresIn ?? 15 * 60) * 1000;
    const delay = Math.max(lifetimeMs - REFRESH_MARGIN_MS, MIN_REFRESH_DELAY_MS);

    const timer = window.setTimeout(() => {
      void (async () => {
        const token = await refreshAccessToken();
        if (!token) {
          // 4. Automatic logout: the session can no longer be renewed.
          clearSession();
          return;
        }
        // Re-arm the timer for the next window.
        setExpiresIn((current) => current ?? 15 * 60);
      })();
    }, delay);

    return () => window.clearTimeout(timer);
  }, [status, expiresIn, clearSession]);

  // --- 4. Automatic logout when the token is cleared elsewhere -------------- //
  React.useEffect(
    () =>
      onAccessTokenCleared(() => {
        // Only downgrade an established session; during bootstrap the store is
        // already `loading` and the restore effect decides the outcome.
        if (useSessionStore.getState().status === "authenticated") clearSession();
      }),
    [clearSession],
  );

  return <>{children}</>;
}
