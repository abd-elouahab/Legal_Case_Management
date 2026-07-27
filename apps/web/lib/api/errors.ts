/**
 * API error types.
 *
 * Every failed request becomes an {@link ApiError} carrying the server's
 * machine-readable code, so callers branch on `error.code` rather than parsing
 * human-readable messages (which are localizable and may change).
 */

import { errorResponseSchema } from "@/lib/validation/auth";
import type { AuthErrorCode } from "@/types/auth";

/** A field-level validation detail from the API envelope. */
export interface ApiErrorDetail {
  field?: string | null;
  message: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: ApiErrorDetail[];
  readonly requestId?: string | null;
  /** Seconds to wait before retrying, from the `Retry-After` header (429s). */
  readonly retryAfterSeconds?: number;

  constructor(params: {
    status: number;
    code: string;
    message: string;
    details?: ApiErrorDetail[];
    requestId?: string | null;
    retryAfterSeconds?: number;
  }) {
    super(params.message);
    this.name = "ApiError";
    this.status = params.status;
    this.code = params.code;
    this.details = params.details ?? [];
    this.requestId = params.requestId;
    this.retryAfterSeconds = params.retryAfterSeconds;
  }

  /** Whether this error means "the access token needs refreshing". */
  get isTokenExpired(): boolean {
    return this.code === "token_expired";
  }

  /** Whether this error means "the session is gone; sign in again". */
  get isAuthFailure(): boolean {
    return this.status === 401;
  }

  /** Whether the caller is being throttled (too many failed sign-ins). */
  get isRateLimited(): boolean {
    return this.status === 429;
  }

  is(code: AuthErrorCode): boolean {
    return this.code === code;
  }
}

/** Parse a `Retry-After` header expressed in seconds. */
function parseRetryAfter(response: Response): number | undefined {
  const header = response.headers.get("Retry-After");
  if (!header) return undefined;

  const seconds = Number.parseInt(header, 10);
  return Number.isFinite(seconds) && seconds > 0 ? seconds : undefined;
}

/** Raised when the API cannot be reached at all (offline, DNS, CORS). */
export class NetworkError extends Error {
  constructor(message = "Unable to reach the server. Check your connection.") {
    super(message);
    this.name = "NetworkError";
  }
}

/**
 * Build an {@link ApiError} from a failed response.
 *
 * Falls back to a generic message when the body is not the expected envelope, so
 * an unexpected upstream failure (e.g. an Nginx error page) never surfaces raw
 * HTML to the user.
 */
export async function toApiError(response: Response): Promise<ApiError> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }

  const retryAfterSeconds = parseRetryAfter(response);

  const parsed = errorResponseSchema.safeParse(body);
  if (parsed.success) {
    return new ApiError({
      status: response.status,
      code: parsed.data.error,
      message: parsed.data.message,
      details: parsed.data.details ?? [],
      requestId: parsed.data.request_id,
      retryAfterSeconds,
    });
  }

  return new ApiError({
    status: response.status,
    code: "unexpected_error",
    message: "Something went wrong. Please try again.",
    retryAfterSeconds,
  });
}
