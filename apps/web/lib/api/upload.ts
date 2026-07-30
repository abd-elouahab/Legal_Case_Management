/**
 * Multipart upload and binary download for the platform API.
 *
 * Two things `lib/api/client.ts` deliberately does not do, because neither is
 * expressible with a JSON `fetch`:
 *
 * * **Uploads with progress.** `fetch` reports nothing while a request body is
 *   being sent — there is no upload-progress event, and streaming request bodies
 *   are not available across the browsers this platform targets.
 *   `XMLHttpRequest` is the only way to drive a real progress bar, and a legal
 *   document can be 25 MB on a slow connection, where an indeterminate spinner
 *   tells the user nothing.
 * * **Binary downloads.** A document is fetched as a `Blob`, not parsed as JSON.
 *   It has to go through an authenticated request rather than a plain `<a href>`
 *   or `<iframe src>`, because the access token lives in memory and is sent as an
 *   `Authorization` header — a browser-initiated navigation would arrive
 *   anonymous and be refused.
 *
 * Both reuse the rest of the client's contract: the same Bearer credential, the
 * same cookie handling, the same {@link ApiError} envelope, and the same
 * refresh-once-and-replay behaviour on an expired token.
 */

import { refreshAccessToken } from "@/lib/api/client";
import { apiUrl } from "@/lib/api/config";
import { NetworkError, toApiError } from "@/lib/api/errors";
import { getAccessToken } from "@/lib/api/token-store";

/** A raw HTTP result, before it is interpreted as success or failure. */
interface RawResult {
  status: number;
  body: string;
  contentType: string;
}

export interface UploadOptions {
  method?: "POST" | "PATCH" | "PUT";
  /** Called with 0–100 as the request body is transmitted. */
  onProgress?: (percent: number) => void;
  signal?: AbortSignal;
}

/**
 * Send one multipart request via `XMLHttpRequest`.
 *
 * Resolves with the raw result for *any* HTTP status — interpreting it is the
 * caller's job, so a 4xx can be turned into an {@link ApiError} with the server's
 * envelope intact rather than a generic failure.
 */
function sendMultipart(
  path: string,
  form: FormData,
  options: UploadOptions,
): Promise<RawResult> {
  return new Promise<RawResult>((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(options.method ?? "POST", apiUrl(path), true);
    // The httpOnly refresh cookie must travel with the request, exactly as it
    // does for `fetch(..., { credentials: "include" })`.
    request.withCredentials = true;
    request.setRequestHeader("Accept", "application/json");

    const token = getAccessToken();
    if (token) request.setRequestHeader("Authorization", `Bearer ${token}`);

    // Deliberately not setting Content-Type: the browser has to add the
    // multipart boundary itself, and setting the header by hand omits it.

    if (options.onProgress) {
      const report = options.onProgress;
      request.upload.addEventListener("progress", (event) => {
        // `lengthComputable` is false for a chunked body, where a percentage
        // would be a fabrication.
        if (event.lengthComputable && event.total > 0) {
          report(Math.min(100, Math.round((event.loaded / event.total) * 100)));
        }
      });
    }

    request.addEventListener("load", () => {
      resolve({
        status: request.status,
        body: request.responseText,
        contentType: request.getResponseHeader("Content-Type") ?? "application/json",
      });
    });

    request.addEventListener("error", () => reject(new NetworkError()));
    request.addEventListener("timeout", () => reject(new NetworkError()));
    request.addEventListener("abort", () =>
      reject(new DOMException("The upload was cancelled.", "AbortError")),
    );

    if (options.signal) {
      if (options.signal.aborted) {
        request.abort();
        return;
      }
      options.signal.addEventListener("abort", () => request.abort(), { once: true });
    }

    request.send(form);
  });
}

/** Reconstruct a `Response` so the shared error mapping can be reused verbatim. */
function asResponse(result: RawResult): Response {
  return new Response(result.body || null, {
    status: result.status,
    headers: { "Content-Type": result.contentType },
  });
}

/**
 * Perform a multipart request, returning the parsed JSON body.
 *
 * On a `token_expired` response the access token is refreshed once and the
 * request replayed, matching {@link apiRequest}. The body is a `FormData` held in
 * memory, so replaying it costs nothing.
 *
 * @throws {ApiError} for any non-2xx response.
 * @throws {NetworkError} when the server is unreachable.
 */
export async function apiUpload<T>(
  path: string,
  form: FormData,
  options: UploadOptions = {},
): Promise<T> {
  let result = await sendMultipart(path, form, options);

  if (result.status === 401) {
    const error = await toApiError(asResponse(result));
    if (error.isTokenExpired && (await refreshAccessToken())) {
      result = await sendMultipart(path, form, options);
    }
  }

  if (result.status < 200 || result.status >= 300) {
    throw await toApiError(asResponse(result));
  }

  return JSON.parse(result.body) as T;
}

/** A file fetched from the API, with the name and type the server declared. */
export interface FetchedFile {
  blob: Blob;
  filename: string;
  mimeType: string;
}

/**
 * Read the filename the server chose out of a `Content-Disposition` header.
 *
 * Prefers the RFC 5987 `filename*` form, which is the only one that survives a
 * non-ASCII name — an Arabic or French document title is mangled by the plain
 * `filename` parameter, which exists solely as a fallback.
 */
export function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;

  const extended = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (extended?.[1]) {
    try {
      return decodeURIComponent(extended[1].trim());
    } catch {
      // A malformed encoding is not worth failing a download over.
    }
  }

  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain?.[1]?.trim() || fallback;
}

async function sendBinary(path: string): Promise<Response> {
  const headers: Record<string, string> = {};
  const token = getAccessToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  try {
    return await fetch(apiUrl(path), { method: "GET", headers, credentials: "include" });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new NetworkError();
  }
}

/**
 * Fetch a binary resource as a {@link Blob}.
 *
 * Used for both download and preview: the token is not a cookie, so the file
 * cannot be reached by pointing an `<a>` or an `<iframe>` at the URL. The caller
 * turns the blob into an object URL — which is also what keeps the document out
 * of the browser's history and off the network a second time.
 *
 * @throws {ApiError} for any non-2xx response, with the server's envelope intact
 *   (a 415 from the preview endpoint is what tells the UI to offer a download).
 * @throws {NetworkError} when the server is unreachable.
 */
export async function apiFetchFile(path: string, fallbackName: string): Promise<FetchedFile> {
  let response = await sendBinary(path);

  if (response.status === 401) {
    const error = await toApiError(response.clone());
    if (error.isTokenExpired && (await refreshAccessToken())) {
      response = await sendBinary(path);
    }
  }

  if (!response.ok) throw await toApiError(response);

  const blob = await response.blob();
  return {
    blob,
    filename: filenameFromDisposition(
      response.headers.get("Content-Disposition"),
      fallbackName,
    ),
    mimeType: response.headers.get("Content-Type") ?? blob.type ?? "application/octet-stream",
  };
}

/**
 * Save a fetched file to the user's machine.
 *
 * A temporary object URL driven by a synthetic anchor click — the standard way to
 * hand a blob to the browser's download manager. The URL is revoked immediately
 * afterwards, because an object URL keeps the whole blob alive in memory until it
 * is, and a legal document can be tens of megabytes.
 */
export function saveFile(file: FetchedFile): void {
  const url = URL.createObjectURL(file.blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = file.filename;
  anchor.rel = "noopener";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
