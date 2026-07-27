/**
 * HTTP client for the platform API.
 *
 * Responsibilities:
 *
 * * attach the in-memory access token as a Bearer credential;
 * * send the httpOnly refresh cookie (`credentials: "include"`);
 * * transparently refresh an expired access token and replay the request once;
 * * normalize every failure into an {@link ApiError} / {@link NetworkError}.
 *
 * Because regular requests authenticate with a header rather than a cookie, a
 * forged cross-site request carries no usable credential — the cookie is only
 * meaningful to the refresh and logout endpoints, and `SameSite` keeps the
 * browser from attaching it cross-site anyway.
 */

import { AUTH_ENDPOINTS, apiUrl } from "@/lib/api/config";
import { ApiError, NetworkError, toApiError } from "@/lib/api/errors";
import {
  clearAccessToken,
  getAccessToken,
  setAccessToken,
} from "@/lib/api/token-store";
import { tokenResponseSchema } from "@/lib/validation/auth";

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  /** JSON-serializable request body. */
  body?: unknown;
  /**
   * Skip the automatic refresh-and-retry. Used by the auth calls themselves so
   * a failing refresh cannot recurse.
   */
  skipAuthRefresh?: boolean;
  /** Omit the Authorization header (login and refresh need no credential). */
  anonymous?: boolean;
  signal?: AbortSignal;
}

/**
 * A single in-flight refresh shared by all callers.
 *
 * Without this, several requests failing with `token_expired` at once would each
 * start their own refresh; because refresh tokens are single-use and rotated,
 * all but the first would be rejected and the user would be signed out.
 */
let refreshInFlight: Promise<string | null> | null = null;

async function rawFetch(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json" };

  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  if (!options.anonymous) {
    const token = getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  try {
    return await fetch(apiUrl(path), {
      method: options.method ?? "GET",
      headers,
      // Required so the browser sends and accepts the httpOnly refresh cookie.
      credentials: "include",
      ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
      ...(options.signal ? { signal: options.signal } : {}),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new NetworkError();
  }
}

/**
 * Exchange the refresh cookie for a new access token.
 *
 * Returns the new token, or `null` when the session cannot be renewed.
 * Concurrent callers share one request.
 */
export async function refreshAccessToken(): Promise<string | null> {
  refreshInFlight ??= (async () => {
    try {
      const response = await rawFetch(AUTH_ENDPOINTS.refresh, {
        method: "POST",
        anonymous: true,
        skipAuthRefresh: true,
      });

      if (!response.ok) {
        clearAccessToken();
        return null;
      }

      const parsed = tokenResponseSchema.safeParse(await response.json());
      if (!parsed.success) {
        clearAccessToken();
        return null;
      }

      setAccessToken(parsed.data.access_token);
      return parsed.data.access_token;
    } catch {
      // Network failure: keep the token so a transient outage does not sign the
      // user out; the next request will try again.
      return null;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

/**
 * Perform an API request, returning the parsed JSON body.
 *
 * On a `token_expired` response the access token is refreshed once and the
 * request replayed, so an expiring token is invisible to callers.
 *
 * @throws {ApiError} for any non-2xx response.
 * @throws {NetworkError} when the server is unreachable.
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  let response = await rawFetch(path, options);

  if (response.status === 401 && !options.skipAuthRefresh && !options.anonymous) {
    const error = await toApiError(response.clone());

    // Only an expired token is retryable; a revoked or malformed one is final.
    if (error.isTokenExpired) {
      const renewed = await refreshAccessToken();
      if (renewed) {
        response = await rawFetch(path, { ...options, skipAuthRefresh: true });
      }
    }
  }

  if (!response.ok) throw await toApiError(response);

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Test helper: drop any shared in-flight refresh. */
export function resetRefreshState(): void {
  refreshInFlight = null;
}
