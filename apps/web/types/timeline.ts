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
] as const;

/** A type the platform is known to record. Not the full domain of `eventType`. */
export type KnownTimelineEventType = (typeof TIMELINE_EVENT_TYPES)[number];

/** Human-readable labels for the filter menu (future: i18n keys). */
export const TIMELINE_EVENT_TYPE_LABELS: Record<KnownTimelineEventType, string> = {
  case_created: "Case created",
  case_updated: "Case updated",
  case_archived: "Case archived",
  case_restored: "Case restored",
  status_changed: "Status changed",
  priority_changed: "Priority changed",
  lawyer_assigned: "Lawyer assigned",
  lawyer_removed: "Lawyer removed",
  representative_assigned: "Representative assigned",
  representative_removed: "Representative removed",
  document_uploaded: "Document uploaded",
  document_updated: "Document updated",
  document_replaced: "Document replaced",
  document_deleted: "Document deleted",
  document_downloaded: "Document downloaded",
};

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

/** The label for an event type, falling back to a readable form of the identifier. */
export function timelineEventLabel(eventType: string): string {
  const known = TIMELINE_EVENT_TYPE_LABELS[eventType as KnownTimelineEventType];
  if (known) return known;

  // A type this build has never heard of still reads as English rather than as a
  // database value.
  const words = eventType.replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Event";
}
