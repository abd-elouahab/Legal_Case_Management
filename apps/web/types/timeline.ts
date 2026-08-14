/**
 * Timeline types.
 *
 * Mirror the API's timeline payload (`apps/api/schemas/timeline.py`) and the
 * event registry defined in `apps/api/models/timeline.py`.
 *
 * **The registry is an open set, and the types say so.** `eventType` is a plain
 * `string` rather than a union, because the API's own column is: a module shipped
 * later can publish a type this build has never heard of, and the timeline must
 * render it rather than crash. {@link TIMELINE_EVENT_TYPES} lists what the
 * platform records *today* — enough to populate a filter and to look up a label —
 * and every lookup falls back. That is the one place this module deliberately
 * departs from "use enums or union types instead of magic strings": the standard
 * exists to stop a typo becoming a silent bug, and here a closed union would
 * instead make a *valid* server response a client error.
 */

/** Event types the platform records today, in the order the API declares them. */
export const TIMELINE_EVENT_TYPES = [
  "case_created",
  "case_updated",
  "case_archived",
  "case_restored",
  "status_changed",
  "priority_changed",
  "lawyer_assigned",
  "lawyer_removed",
  "representative_assigned",
  "representative_removed",
  "document_uploaded",
  "document_updated",
  "document_replaced",
  "document_deleted",
  "document_downloaded",
  // OCR processing. Added by a later module without any change to the timeline
  // itself, which is exactly what the open registry above is for.
  "ocr_started",
  "ocr_completed",
  "ocr_failed",
  "ocr_retried",
] as const;

/** A type the platform is known to record. Not the full domain of `eventType`. */
export type KnownTimelineEventType = (typeof TIMELINE_EVENT_TYPES)[number];

/**
 * Where an event type's label lives: `timeline.events` in the message catalogues.
 *
 * The catalogue says *"Text extraction started"* rather than *"OCR started"*, for
 * the reason the old constant recorded: a case history is read by lawyers and
 * court staff, not by the people who built the pipeline. A type this build has
 * never heard of renders through the provider's `getMessageFallback`, which
 * humanizes the key — the same thing `timelineEventLabel` did with a regex.
 */
export const TIMELINE_EVENT_NAMESPACE = "timeline.events";


/**
 * Event families, mirroring the API's `TimelineEventCategory`.
 *
 * Closed, unlike the event types: the API *computes* this field and answers with
 * `case` for anything it does not recognise, so the client can only ever receive
 * one of these five.
 */
export const TIMELINE_CATEGORIES = [
  "case",
  "status",
  "priority",
  "assignment",
  "document",
] as const;
export type TimelineCategory = (typeof TIMELINE_CATEGORIES)[number];

/**
 * One recorded activity on a case.
 *
 * `actorName` and `actorRole` are **snapshots** taken when the event happened, not
 * a live lookup — renaming a user does not rewrite history, so a timeline entry
 * shows who acted as they were at the time.
 */
export interface TimelineEvent {
  id: string;
  caseId: string;

  /** A registry identifier. Open set — see the module note. */
  eventType: string;
  /** Family the event belongs to, computed server-side. */
  category: TimelineCategory;

  title: string;
  description: string | null;

  actorId: string | null;
  actorName: string | null;
  /** A `UserRole` value, as text: a role retired later must still read back. */
  actorRole: string | null;

  /** Structured specifics; shape depends on `eventType`. Never null. */
  metadata: Record<string, unknown>;

  /** ISO-8601 timestamp. */
  createdAt: string;
}

