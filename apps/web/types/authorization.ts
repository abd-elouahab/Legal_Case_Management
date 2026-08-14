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

  // Dashboard
  //
  // **One permission, and the absence of a second is the design.** There is no
  // `dashboard:view`: the dashboard is the landing page for every authenticated
  // role, and a permission every role holds is not a permission. What a dashboard
  // *contains* is decided per widget by the API, against the capability that owns
  // each widget's rows — so the route is guarded by authentication alone and the
  // widgets gate themselves.
  "dashboard:monitor",

  // Settings
  //
  // **Four, and two of them grant nothing about anybody else.** `settings:view`
  // and `settings:update` are the caller's *own* preferences — every role holds
  // both, because a role that could not change its own theme or language would be
  // a role the platform is unusable in, and no endpoint behind them takes a user
  // identifier. `settings:manage` is the *platform's* configuration (maintenance
  // mode, and the defaults every account that has expressed no opinion follows),
  // held by administrators only; `settings:monitor` is the operational view, like
  // every other `*:monitor`.
  "settings:view",
  "settings:update",
  "settings:manage",
  "settings:monitor",

  // Monitoring & observability
  //
  // **Two, and neither is a `*:monitor`** — which is the substance of it rather
  // than a naming quirk. Eleven features each added a `<feature>:monitor` gating
  // *their own* operational view; this one *is* the operational view, so its
  // permissions are named for what they grant. Both are administrator-only:
  // `22-monitoring.md` is explicit that regular users must never reach a
  // monitoring endpoint, and every figure behind them is platform-wide, so there
  // is nothing to scope and no narrower version to hand anybody.
  //
  // `monitoring:export` is not referenced in this application at all — it gates
  // the Prometheus scrape endpoint, whose caller is a scraper rather than a
  // browser. It is listed so the type stays the API's full vocabulary.
  "monitoring:view",
  "monitoring:export",
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

  dashboardMonitor: "dashboard:monitor",

  settingsView: "settings:view",
  settingsUpdate: "settings:update",
  settingsManage: "settings:manage",
  settingsMonitor: "settings:monitor",

  monitoringView: "monitoring:view",
  monitoringExport: "monitoring:export",
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
