/**
 * In-memory access token store.
 *
 * The access token is held in a module-level variable and **never** written to
 * `localStorage`, `sessionStorage`, or a JS-readable cookie. Those are readable
 * by any injected script, so a single XSS would leak a long-lived credential.
 * Keeping it in memory limits the blast radius to the current page instance.
 *
 * Session persistence across reloads does not need this token to survive: the
 * httpOnly refresh cookie does that job, and the app exchanges it for a new
 * access token on startup.
 */

let accessToken: string | null = null;

/** Listeners notified when the token is cleared, so the UI can react. */
type ClearListener = () => void;
const clearListeners = new Set<ClearListener>();

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string): void {
  accessToken = token;
}

export function clearAccessToken(): void {
  accessToken = null;
  for (const listener of clearListeners) listener();
}

/** Subscribe to token clearing (used to force the UI back to signed-out). */
export function onAccessTokenCleared(listener: ClearListener): () => void {
  clearListeners.add(listener);
  return () => clearListeners.delete(listener);
}

/** Test helper: reset all module state between tests. */
export function resetTokenStore(): void {
  accessToken = null;
  clearListeners.clear();
}
