/**
 * What each domain event makes stale.
 *
 * The single place the platform decides "this happened, therefore refetch that".
 * It is deliberately a **table rather than a switch inside a component**: every
 * feature's hooks already own their query keys, and a case workspace, a document
 * list, and a report panel that each interpreted events for themselves would be
 * three places for the mapping to drift.
 *
 * **An event invalidates; it never patches.** A `case.status_changed` event
 * carries the new status, and it would be easy to write it straight into the
 * cached case — and wrong. The event is a *notification*, delivered to everyone
 * following the case; the cached case is an *authorized read*, scoped to this
 * caller and carrying the fields they are entitled to. Patching one from the
 * other would mean a client rendering data that never passed through an
 * authorization check, and it would drift the moment a field is added to the API
 * and not to the event. Invalidating costs one request and is always correct.
 *
 * That is also why the payloads on the wire are so thin (see
 * `apps/api/core/events.py`): the server is not trying to send you the new
 * state, it is telling you to go and read it.
 */

import type { QueryClient } from "@tanstack/react-query";

import { caseKeys } from "@/hooks/use-cases";
import { documentKeys } from "@/hooks/use-documents";
import { indexingKeys } from "@/hooks/use-indexing";
import { ocrKeys } from "@/hooks/use-ocr";
import { reportKeys } from "@/hooks/use-reports";
import { timelineKeys } from "@/hooks/use-timeline";
import type { RealtimeEvent } from "@/types/realtime";

/** A cache key prefix to invalidate. */
type QueryKey = readonly unknown[];

/**
 * Which caches one event makes stale.
 *
 * Exported separately from {@link applyRealtimeEvent} so it can be unit-tested
 * as a pure function — "does a completed OCR run invalidate the document list?"
 * is a question about this table, not about a query client.
 */
export function staleKeysFor(event: RealtimeEvent): QueryKey[] {
  const keys: QueryKey[] = [];
  const caseId = event.caseId;
  const documentId =
    typeof event.payload.document_id === "string" ? event.payload.document_id : null;
  const reportId =
    typeof event.payload.report_id === "string" ? event.payload.report_id : null;

  switch (event.event) {
    // --- Cases ------------------------------------------------------------ //
    case "case.created":
      // The list only: there is no detail to invalidate for a case this client
      // has never read, and inventing a key for one would leave an empty entry
      // in the cache that the next real read has to displace.
      keys.push(caseKeys.lists());
      break;

    case "case.updated":
    case "case.archived":
    case "case.restored":
    case "case.status_changed":
    case "case.priority_changed":
    case "case.assignment_changed":
      keys.push(caseKeys.lists());
      if (caseId) keys.push(caseKeys.detail(caseId));
      break;

    // --- Documents -------------------------------------------------------- //
    case "document.uploaded":
    case "document.deleted":
      keys.push(documentKeys.lists());
      if (documentId) keys.push(documentKeys.detail(documentId));
      break;

    case "document.updated":
    case "document.replaced":
      keys.push(documentKeys.lists());
      if (documentId) {
        keys.push(documentKeys.detail(documentId));
        // A replacement creates a new version, which restarts both derived
        // pipelines. Without these the badges beside the file would keep showing
        // the previous version's completed extraction and index.
        keys.push(ocrKeys.result(documentId));
        keys.push(ocrKeys.history(documentId));
        keys.push(indexingKeys.index(documentId));
        keys.push(indexingKeys.history(documentId));
      }
      break;

    // --- OCR --------------------------------------------------------------- //
    case "ocr.started":
    case "ocr.completed":
    case "ocr.failed":
      keys.push(ocrKeys.lists());
      if (documentId) {
        keys.push(ocrKeys.result(documentId));
        keys.push(ocrKeys.history(documentId));
        // The extracted *text* too, but only on completion: it is a large
        // response, and refetching it when a run merely started would download a
        // previous version's text to replace it with the same thing.
        if (event.event === "ocr.completed") keys.push(ocrKeys.text(documentId));
      }
      break;

    // --- Indexing ---------------------------------------------------------- //
    case "indexing.started":
    case "indexing.completed":
    case "indexing.failed":
      keys.push(indexingKeys.lists());
      if (documentId) {
        keys.push(indexingKeys.index(documentId));
        keys.push(indexingKeys.history(documentId));
      }
      break;

    // --- Reports ----------------------------------------------------------- //
    case "report.started":
    case "report.generated":
    case "report.failed":
      keys.push(reportKeys.lists());
      if (reportId) keys.push(reportKeys.detail(reportId));
      break;

    case "report.progress":
      // The **detail only**. Progress moves once per section — every few seconds
      // for the length of a run — and invalidating the history list on each tick
      // would turn one report into a dozen refetches of a table whose only
      // changing cell is a progress bar the open dialog is already showing.
      if (reportId) keys.push(reportKeys.detail(reportId));
      break;

    // --- Timeline ---------------------------------------------------------- //
    case "timeline.updated":
      // Scoped to the case whose history grew. `timelineKeys.all` would refetch
      // every case timeline the client has cached, which on the case list is
      // every case it has visited.
      if (caseId) keys.push(timelineKeys.case(caseId));
      break;

    // --- Presence ---------------------------------------------------------- //
    case "presence.changed":
      // Nothing. Presence *visualization* is out of this feature's scope, so
      // there is no cache to make stale — the event is delivered so a future
      // feature has it, and listed here so that "presence is handled" is visible
      // rather than looking like an omission.
      break;
  }

  return keys;
}

/**
 * Invalidate everything one event makes stale.
 *
 * Fire-and-forget: `invalidateQueries` resolves when the refetches finish, and
 * awaiting that in an event handler would serialize the handling of a burst of
 * events behind the network. Nothing depends on the result.
 */
export function applyRealtimeEvent(client: QueryClient, event: RealtimeEvent): void {
  for (const queryKey of staleKeysFor(event)) {
    void client.invalidateQueries({ queryKey });
  }
}
