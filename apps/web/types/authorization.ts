/**
 * Authorization identifiers.
 *
 * Mirrors the backend's permission catalog (`apps/api/core/permissions.py`).
 * These are the *names* of capabilities only — which role holds which permission
 * is decided by the API and delivered with the session, so the policy exists in
 * exactly one place and the client cannot drift from it.
 *
 * A union type rather than magic strings (per the code standards): referencing a
 * permission that the platform does not define is a compile error.
 */

import type { UserRole } from "@/types/user";

export const PERMISSIONS = [
  // User management
  "users:create",
  "users:view",
  "users:update",
  "users:delete",

  // Case management
  "cases:create",
  "cases:view",
  "cases:update",
  "cases:delete",
  "cases:assign",

  // Document management
  "documents:upload",
  "documents:view",
  "documents:update",
  "documents:delete",

  // OCR processing
  "ocr:view",
  "ocr:retry",
  "ocr:monitor",

  // Document indexing
  "indexing:view",
  "indexing:reindex",
  "indexing:monitor",

  // Semantic search
  "search:query",
  "search:monitor",

  // Timeline
  "timeline:view",
  "timeline:create",

  // Reports
  //
  // `reports:view` is not a row grant: a report belongs to the user who
  // generated it, and every read on the API is keyed by them — so holding it
  // gives a caller their own history and nobody else's. There is deliberately no
  // `reports:view-all`.
  "reports:view",
  "reports:generate",
  "reports:monitor",

  // Notifications
  //
  // `notifications:view` is not a row grant either: a notification belongs to
  // exactly one recipient and every read on the API is keyed by them, so holding
  // it gives a caller their own feed and nobody else's — the same shape
  // `reports:view` has, and the reason there is no `notifications:view-all`.
  // `notifications:manage` gates addressing the whole platform.
  "notifications:view",
  "notifications:manage",
  "notifications:monitor",

  // AI
  //
  // `ai:ask` is putting one question to the RAG pipeline; `ai:chat` is the
  // assistant's conversational surface. Sending a message needs both, because a
  // message does both — a deployment may grant one and withhold the other, and
  // the API refuses the combination the client cannot see.
  "ai:ask",
  "ai:chat",
  "ai:generate-report",
  "ai:monitor",

  // Settings
  "settings:view",
  "settings:update",
] as const;

export type Permission = (typeof PERMISSIONS)[number];

/**
 * Named constants for the permissions referenced in application code.
 *
 * `PERMISSION.casesView` instead of a bare `"cases:view"` gives call sites
 * autocomplete and turns a typo into a compile error, while the value stays the
 * exact identifier the API understands. `satisfies` keeps the two in step: an
 * entry whose value is not a defined permission fails to compile.
 */
export const PERMISSION = {
  usersCreate: "users:create",
  usersView: "users:view",
  usersUpdate: "users:update",
  usersDelete: "users:delete",

  casesCreate: "cases:create",
  casesView: "cases:view",
  casesUpdate: "cases:update",
  casesDelete: "cases:delete",
  casesAssign: "cases:assign",

  documentsUpload: "documents:upload",
  documentsView: "documents:view",
  documentsUpdate: "documents:update",
  documentsDelete: "documents:delete",

  ocrView: "ocr:view",
  ocrRetry: "ocr:retry",
  ocrMonitor: "ocr:monitor",

  indexingView: "indexing:view",
  indexingReindex: "indexing:reindex",
  indexingMonitor: "indexing:monitor",

  searchQuery: "search:query",
  searchMonitor: "search:monitor",

  timelineView: "timeline:view",
  timelineCreate: "timeline:create",

  reportsView: "reports:view",
  reportsGenerate: "reports:generate",
  reportsMonitor: "reports:monitor",

  notificationsView: "notifications:view",
  notificationsManage: "notifications:manage",
  notificationsMonitor: "notifications:monitor",

  aiAsk: "ai:ask",
  aiChat: "ai:chat",
  aiGenerateReport: "ai:generate-report",
  aiMonitor: "ai:monitor",

  settingsView: "settings:view",
  settingsUpdate: "settings:update",
} as const satisfies Record<string, Permission>;

/**
 * A declarative access requirement.
 *
 * Every clause present must pass (they combine with AND); an empty rule means
 * "any authenticated user". This one shape is what the hooks, the `Protected`
 * component, the route guard, and the navigation config all speak, so a
 * requirement is expressed identically wherever it appears.
 */
export interface AccessRule {
  /** The caller must hold this permission. */
  permission?: Permission;
  /** The caller must hold **at least one** of these permissions. */
  anyOf?: readonly Permission[];
  /** The caller must hold **every** one of these permissions. */
  allOf?: readonly Permission[];
  /** The caller must hold one of these roles. Prefer permissions where possible. */
  roles?: readonly UserRole[];
}

/** What an access rule is evaluated against: the caller's role and grants. */
export interface AccessSubject {
  role: UserRole;
  permissions: readonly Permission[];
}
