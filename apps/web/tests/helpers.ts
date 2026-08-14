/**
 * Test helpers for the frontend auth suite.
 *
 * Provides a small scripted `fetch` double so tests describe API behaviour
 * declaratively instead of hand-rolling mock responses.
 */

import { vi } from "vitest";

import { PERMISSIONS, type Permission } from "@/types/authorization";
import type { SessionUser, UserRole } from "@/types/user";

/**
 * Permissions per role, mirroring the API's policy (`apps/api/core/roles.py`).
 *
 * Only tests carry this table: the application always takes permissions from the
 * session the API delivered. Duplicating it here is what lets the fixtures
 * describe a realistic lawyer or court representative without a running backend.
 */
export const ROLE_PERMISSIONS: Record<UserRole, readonly Permission[]> = {
  administrator: PERMISSIONS,
  lawyer: [
    "cases:view",
    "documents:view",
    "documents:upload",
    "ocr:view",
    "ocr:retry",
    "indexing:view",
    "indexing:reindex",
    // Search the documents they can already read. Granted where `ocr:retry`
    // and `indexing:reindex` are withheld from the court role, because this
    // one *reads* rather than operating the pipeline.
    "search:query",
    "timeline:view",
    "timeline:create",
    // Read and generate reports on the cases they are assigned to. Note that
    // `reports:view` is not a row grant: every read on the API is keyed by the
    // requester, so a lawyer reads their own history and nobody else's.
    // `reports:monitor` is withheld, like every other `*:monitor` permission —
    // the platform-wide view is administrative.
    "reports:view",
    "reports:generate",
    // Both AI grants, because a message needs both: `ai:chat` is the
    // conversational surface and `ai:ask` is putting the question to the
    // pipeline. The court role holds neither — that is the one place this
    // platform draws a line between reading the case file and generating an
    // interpretation of it.
    "ai:ask",
    "ai:chat",
    "ai:generate-report",
    // Their own feed and their own preferences, and nothing about anybody
    // else's: every read on the API is keyed by the recipient, which is why
    // there is no `notifications:view-all`. `notifications:manage` (addressing
    // the whole platform) and `notifications:monitor` are both withheld.
    "notifications:view",
    // Their own settings, read and written: both are in the API's
    // `BASE_PERMISSIONS`, because a role that could not change its own theme or
    // language would be a role the platform is unusable in. `settings:manage`
    // (the platform's own configuration) and `settings:monitor` are withheld,
    // like every other administrative grant here.
    "settings:view",
    "settings:update",
  ],
  court: [
    "cases:view",
    "cases:update",
    "documents:view",
    "documents:upload",
    // Read the extracted text of documents they can already read, but not
    // re-run extraction: `ocr:retry` consumes processing capacity, and the
    // court role's description does not extend to operating the pipeline.
    "ocr:view",
    // Same reasoning one stage further on: read whether a document is
    // searchable, but do not rebuild the index — a rebuild re-embeds every
    // passage, which is the most expensive operation the platform performs.
    "indexing:view",
    // Reads strictly less than `ocr:view`, which already gives them the full
    // extracted text of the same documents: withholding it would leave them
    // able to read every page of a filing but not to find a clause in it.
    "search:query",
    "timeline:view",
    "timeline:create",
    "notifications:view",
    "settings:view",
    "settings:update",
  ],
};

export const TEST_USER = {
  id: "8f14e45f-ceea-467a-9f3a-1b2c3d4e5f60",
  email: "amina.benali@example.com",
  full_name: "Amina Benali",
  role: "administrator" as const,
  is_active: true,
  permissions: ROLE_PERMISSIONS.administrator,
  must_change_password: false,
  last_login_at: null,
  created_at: "2026-07-01T09:00:00Z",
};

export const TEST_SESSION_USER: SessionUser = {
  id: TEST_USER.id,
  email: TEST_USER.email,
  name: TEST_USER.full_name,
  role: TEST_USER.role,
  permissions: TEST_USER.permissions,
  mustChangePassword: false,
};

/** A signed-in user with the grants their role actually carries. */
export function sessionUserWithRole(role: UserRole): SessionUser {
  return {
    ...TEST_SESSION_USER,
    id: `user-${role}`,
    email: `${role}@example.com`,
    role,
    permissions: ROLE_PERMISSIONS[role],
  };
}

export function tokenResponse(overrides: Record<string, unknown> = {}) {
  return {
    access_token: "access-token-1",
    refresh_token: "refresh-token-1",
    token_type: "bearer",
    expires_in: 900,
    user: TEST_USER,
    ...overrides,
  };
}

export function errorEnvelope(
  code: string,
  message = "Something went wrong.",
  details: Array<{ field?: string | null; message: string }> = [],
) {
  return { error: code, message, request_id: "req-1", details };
}

// --------------------------------------------------------------------------- //
// User Management fixtures
// --------------------------------------------------------------------------- //

/** A user record in the API's wire format, as `GET /users` returns it. */
export function managedUserPayload(overrides: Record<string, unknown> = {}) {
  const role = (overrides.role as UserRole | undefined) ?? "lawyer";

  return {
    id: "11111111-1111-4111-8111-111111111111",
    email: "karim.zahra@example.com",
    first_name: "Karim",
    last_name: "Zahra",
    full_name: "Karim Zahra",
    phone: "+212 612345678",
    profile_image: null,
    role,
    status: "active",
    is_active: true,
    must_change_password: false,
    last_login_at: "2026-07-20T08:30:00Z",
    created_at: "2026-07-01T09:00:00Z",
    updated_at: "2026-07-20T08:30:00Z",
    created_by: TEST_USER.id,
    updated_by: TEST_USER.id,
    permissions: ROLE_PERMISSIONS[role],
    ...overrides,
  };
}

/** A page of users in the API's wire format. */
export function userPagePayload(
  items: Array<Record<string, unknown>> = [managedUserPayload()],
  overrides: Record<string, unknown> = {},
) {
  return {
    items,
    total_records: items.length,
    page: 1,
    page_size: 20,
    total_pages: 1,
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// Case Management fixtures
// --------------------------------------------------------------------------- //

/** A case assignee or auditor in the API's wire format. */
export function caseUserPayload(overrides: Record<string, unknown> = {}) {
  return {
    id: "22222222-2222-4222-8222-222222222222",
    full_name: "Karim Zahra",
    email: "karim.zahra@example.com",
    role: "lawyer" as UserRole,
    ...overrides,
  };
}

/**
 * A case record in the API's wire format, as `GET /cases` returns it.
 *
 * `allowed_transitions` defaults to the legal moves from `open`, so a test that
 * opens the edit dialog sees a realistic status menu without spelling it out.
 */
export function legalCasePayload(overrides: Record<string, unknown> = {}) {
  return {
    id: "33333333-3333-4333-8333-333333333333",
    case_number: "CASE-2026-0001",
    title: "Benali v. Societe Atlas",
    description: "Breach of a supply contract.",
    category: "Commercial",
    status: "open",
    priority: "high",
    court_name: "Tribunal de Commerce de Casablanca",
    filing_date: "2026-05-10",
    next_hearing_date: "2026-06-10",
    assigned_lawyer_id: caseUserPayload().id,
    assigned_court_representative_id: null,
    assigned_lawyer: caseUserPayload(),
    assigned_court_representative: null,
    created_by: TEST_USER.id,
    updated_by: TEST_USER.id,
    creator: caseUserPayload({
      id: TEST_USER.id,
      full_name: TEST_USER.full_name,
      email: TEST_USER.email,
      role: "administrator",
    }),
    updater: null,
    created_at: "2026-05-10T09:00:00Z",
    updated_at: "2026-05-20T08:30:00Z",
    is_archived: false,
    allowed_transitions: ["open", "in_progress", "waiting_for_hearing", "closed", "archived"],
    ...overrides,
  };
}

/** A page of cases in the API's wire format. */
export function casePagePayload(
  items: Array<Record<string, unknown>> = [legalCasePayload()],
  overrides: Record<string, unknown> = {},
) {
  return {
    items,
    total_records: items.length,
    page: 1,
    page_size: 20,
    total_pages: 1,
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// Document Management fixtures
// --------------------------------------------------------------------------- //

/** One version of a document in the API's wire format. */
export function documentVersionPayload(overrides: Record<string, unknown> = {}) {
  return {
    version: 1,
    original_filename: "contrat-de-bail.pdf",
    file_extension: "pdf",
    mime_type: "application/pdf",
    file_size: 2048,
    file_size_label: "2.0 KB",
    uploaded_by: TEST_USER.id,
    uploader: caseUserPayload({
      id: TEST_USER.id,
      full_name: TEST_USER.full_name,
      email: TEST_USER.email,
      role: "administrator",
    }),
    created_at: "2026-07-20T09:00:00Z",
    ...overrides,
  };
}

/**
 * A document record in the API's wire format, as `GET /documents` returns it.
 *
 * Defaults to a single-version PDF, which is what nearly every row is; a test
 * that cares about versioning overrides `version` and `versions` explicitly.
 */
export function legalDocumentPayload(overrides: Record<string, unknown> = {}) {
  const versions = (overrides.versions as unknown[] | undefined) ?? [documentVersionPayload()];

  return {
    id: "44444444-4444-4444-8444-444444444444",
    case_id: legalCasePayload().id,
    case: {
      id: legalCasePayload().id,
      case_number: "CASE-2026-0001",
      title: "Benali v. Societe Atlas",
    },
    original_filename: "contrat-de-bail.pdf",
    stored_filename: "9f8e7d6c5b4a.pdf",
    file_extension: "pdf",
    mime_type: "application/pdf",
    file_size: 2048,
    file_size_label: "2.0 KB",
    storage_bucket: "legal-documents",
    storage_key: "cases/x/documents/y/v1/9f8e7d6c5b4a.pdf",
    category: "contract",
    description: "Bail commercial signé",
    version: 1,
    version_count: versions.length,
    uploaded_by: TEST_USER.id,
    uploader: caseUserPayload({
      id: TEST_USER.id,
      full_name: TEST_USER.full_name,
      email: TEST_USER.email,
      role: "administrator",
    }),
    uploaded_at: "2026-07-20T09:00:00Z",
    created_at: "2026-07-20T09:00:00Z",
    updated_at: "2026-07-20T09:00:00Z",
    deleted_at: null,
    is_deleted: false,
    is_previewable: true,
    ...overrides,
    versions,
  };
}

/** A page of documents in the API's wire format. */
export function documentPagePayload(
  items: Array<Record<string, unknown>> = [legalDocumentPayload()],
  overrides: Record<string, unknown> = {},
) {
  return {
    items,
    total_records: items.length,
    page: 1,
    page_size: 20,
    total_pages: 1,
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// OCR fixtures
// --------------------------------------------------------------------------- //

/**
 * An extraction run in the API's wire format, as `GET /documents/{id}/ocr`
 * returns it.
 *
 * Defaults to a completed run over the default document. The computed flags
 * (`is_terminal`, `is_active`, `can_retry`) are included because the API computes
 * and serves them — a fixture that omitted them would be asserting against a
 * payload the server never sends, and the client deliberately reads them rather
 * than deriving them itself.
 */
export function ocrResultPayload(overrides: Record<string, unknown> = {}) {
  const status = (overrides.status as string | undefined) ?? "completed";
  const terminal = status === "completed" || status === "failed";

  return {
    id: "66666666-6666-4666-8666-666666666666",
    document_id: legalDocumentPayload().id,
    document_version: 1,
    document: {
      id: legalDocumentPayload().id,
      case_id: legalCasePayload().id,
      original_filename: "contrat-de-bail.pdf",
      file_extension: "pdf",
    },
    status,
    engine: "tesseract",
    engine_version: "5.3.4",
    detected_language: "eng+fra+ara",
    page_count: terminal ? 2 : null,
    confidence: status === "completed" ? 92.4 : null,
    started_at: status === "pending" ? null : "2026-07-20T09:00:01Z",
    finished_at: terminal ? "2026-07-20T09:00:04Z" : null,
    duration_ms: terminal ? 3120 : null,
    duration_seconds: terminal ? 3.12 : null,
    attempt_count: 1,
    error_code: null,
    error_message: null,
    requested_by: null,
    created_at: "2026-07-20T09:00:00Z",
    updated_at: "2026-07-20T09:00:04Z",
    is_terminal: terminal,
    is_active: !terminal,
    can_retry: terminal,
    ...overrides,
  };
}

/** Extracted text in the API's wire format, as `GET /documents/{id}/ocr/text` returns it. */
export function ocrTextPayload(overrides: Record<string, unknown> = {}) {
  const pages = (overrides.pages as Array<Record<string, unknown>> | undefined) ?? [
    {
      page_number: 1,
      text: "Contrat de bail commercial.",
      confidence: 94.1,
      character_count: 27,
      is_empty: false,
    },
    {
      page_number: 2,
      text: "محضر الجلسة",
      confidence: 88.0,
      character_count: 11,
      is_empty: false,
    },
  ];

  return {
    ocr_result_id: ocrResultPayload().id,
    document_id: legalDocumentPayload().id,
    document_version: 1,
    status: "completed",
    detected_language: "eng+fra+ara",
    page_count: pages.length,
    character_count: pages.reduce(
      (total, page) => total + String(page.text ?? "").length,
      0,
    ),
    // U+000C FORM FEED, exactly as the API joins and publishes it.
    full_text: pages.map((page) => String(page.text ?? "")).join("\f"),
    page_separator: "\f",
    ...overrides,
    pages,
  };
}

/** Platform-wide extraction metrics in the API's wire format. */
export function ocrMetricsPayload(overrides: Record<string, unknown> = {}) {
  return {
    window_days: null,
    total_runs: 12,
    pending: 1,
    processing: 1,
    completed: 8,
    failed: 2,
    finished_runs: 10,
    success_rate: 80.0,
    failure_rate: 20.0,
    average_duration_ms: 3120,
    average_duration_seconds: 3.12,
    failures_by_code: { timeout: 1, engine_failure: 1 },
    engine: "tesseract",
    engine_available: true,
    enabled: true,
    supported_extensions: ["jpeg", "jpg", "pdf", "png"],
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// Document indexing fixtures
// --------------------------------------------------------------------------- //

/** One indexing run in the API's wire format, as `GET /documents/{id}/index` returns it. */
export function documentIndexPayload(overrides: Record<string, unknown> = {}) {
  const status = (overrides.status as string | undefined) ?? "indexed";
  const terminal = status === "indexed" || status === "failed";
  const succeeded = status === "indexed";

  return {
    id: "77777777-7777-4777-8777-777777777777",
    document_id: legalDocumentPayload().id,
    document_version: 1,
    case_id: legalCasePayload().id,
    document: {
      id: legalDocumentPayload().id,
      case_id: legalCasePayload().id,
      original_filename: "contrat-de-bail.pdf",
      file_extension: "pdf",
    },
    status,
    chunk_count: succeeded ? 14 : null,
    page_count: succeeded ? 2 : null,
    character_count: succeeded ? 8420 : null,
    embedding_model: succeeded ? "BAAI/bge-m3" : null,
    embedding_dimensions: succeeded ? 1024 : null,
    vector_collection: succeeded ? "document_chunks" : null,
    chunk_size: succeeded ? 1000 : null,
    chunk_overlap: succeeded ? 200 : null,
    detected_language: succeeded ? "fr" : null,
    started_at: status === "pending" ? null : "2026-07-20T09:00:05Z",
    finished_at: terminal ? "2026-07-20T09:00:19Z" : null,
    duration_ms: terminal ? 14200 : null,
    duration_seconds: terminal ? 14.2 : null,
    attempt_count: 1,
    error_code: null,
    error_message: null,
    requested_by: null,
    created_at: "2026-07-20T09:00:04Z",
    updated_at: "2026-07-20T09:00:19Z",
    is_terminal: terminal,
    is_active: !terminal,
    can_reindex: terminal,
    ...overrides,
  };
}

/** Platform-wide indexing metrics in the API's wire format. */
export function indexMetricsPayload(overrides: Record<string, unknown> = {}) {
  return {
    window_days: null,
    total_runs: 12,
    pending: 1,
    indexing: 1,
    indexed: 8,
    failed: 2,
    finished_runs: 10,
    total_chunks: 96,
    average_chunks_per_document: 12.0,
    success_rate: 80.0,
    failure_rate: 20.0,
    average_duration_ms: 14200,
    average_duration_seconds: 14.2,
    failures_by_code: { embedding_failure: 1, vector_store_unavailable: 1 },
    embedding_model: "BAAI/bge-m3",
    embedding_dimensions: 1024,
    embedding_available: true,
    chunker: "recursive-character",
    chunk_size: 1000,
    chunk_overlap: 200,
    vector_collection: "document_chunks",
    vector_store_available: true,
    vector_collection_exists: true,
    stored_vectors: 96,
    enabled: true,
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// AI report fixtures
// --------------------------------------------------------------------------- //

/**
 * One report history row in the API's wire format.
 *
 * Defaults to a **completed** report, because that is the state most assertions
 * need. `is_terminal`, `is_active`, and `progress_percent` are computed here the
 * way the server computes them, so a fixture cannot describe a row the API could
 * never send — a queued report reporting 100% would make a progress test pass
 * against nothing.
 */
export function reportPayload(overrides: Record<string, unknown> = {}) {
  const status = (overrides.status as string | undefined) ?? "completed";
  const active = status === "pending" || status === "processing";
  const total = (overrides.sections_total as number | undefined) ?? 4;
  const done = active ? ((overrides.sections_completed as number | undefined) ?? 1) : total;

  return {
    id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    case_id: "22222222-2222-4222-8222-222222222222",
    conversation_id: null,
    report_type: "case_summary",
    title: "Case Summary — CASE-2026-0001",
    language: "fr",
    status,

    sections_total: total,
    sections_completed: done,

    started_at: active ? "2026-08-07T09:30:00Z" : "2026-08-07T09:30:00Z",
    finished_at: active ? null : "2026-08-07T09:32:10Z",
    duration_ms: active ? null : 130_000,
    duration_seconds: active ? null : 130.0,
    attempt_count: 1,

    retrieved_count: active ? null : 24,
    context_count: active ? null : 18,
    grounded_sections: active ? null : 3,
    character_count: active ? null : 4820,

    provider: "gemini",
    model: "gemini-2.5-flash",
    prompt_name: "rag/answer",
    prompt_version: 1,
    template_version: 1,

    prompt_tokens: active ? null : 4800,
    completion_tokens: active ? null : 1600,
    total_tokens: active ? null : 6400,

    error_code: null,
    error_message: null,

    export_count: 0,
    last_exported_at: null,

    created_at: "2026-08-07T09:29:50Z",
    updated_at: "2026-08-07T09:32:10Z",

    is_terminal: !active,
    is_active: active,
    progress_percent: status === "completed" ? 100 : active ? Math.floor((done / total) * 100) : 0,
    ...overrides,
  };
}

/** One section of a finished report, in the API's wire format. */
export function reportSectionPayload(overrides: Record<string, unknown> = {}) {
  return {
    key: "overview",
    title: "Aperçu",
    content: "Le litige porte sur un bail commercial [1].",
    grounded: true,
    citation_markers: [1],
    retrieved_count: 6,
    context_count: 4,
    duration_ms: 18_000,
    ...overrides,
  };
}

/** One report with its sections and citations, in the API's wire format. */
export function reportDetailPayload(overrides: Record<string, unknown> = {}) {
  return {
    // Overrides are threaded through `reportPayload` rather than applied only at
    // the end, so a fixture asking for `status: "processing"` also gets the
    // `is_active` and `progress_percent` the server would compute for it — a
    // detail payload describing a running report that claims to be finished
    // would make every progress assertion pass against nothing.
    ...reportPayload(overrides),
    sections: [
      reportSectionPayload(),
      reportSectionPayload({
        key: "parties",
        title: "Parties",
        content: "Les documents indexés de cette affaire ne couvrent pas cette section.",
        grounded: false,
        citation_markers: [],
      }),
    ],
    // Deliberately the *RAG pipeline's* citation shape, because that is exactly
    // what the API returns — the report renumbers the marker and changes nothing
    // else.
    citations: [assistantCitationPayload()],
    citation_count: 1,
    document_count: 1,
    references_title: "Références",
    disclaimer:
      "Rapport généré automatiquement à partir des documents indexés de cette affaire. " +
      "Il ne constitue pas un conseil juridique.",
    ...overrides,
  };
}

export function reportPagePayload(
  items: Array<Record<string, unknown>> = [reportPayload()],
  overrides: Record<string, unknown> = {},
) {
  return {
    items,
    total_records: items.length,
    page: 1,
    page_size: 20,
    total_pages: 1,
    ...overrides,
  };
}

/** The report-type catalogue, in the API's wire format. */
export function reportTemplatesPayload() {
  return [
    {
      report_type: "case_summary",
      title: "Synthèse de l'affaire",
      description: "Vue complète du dossier, de bout en bout.",
      sections: [
        { key: "overview", title: "Aperçu" },
        { key: "parties", title: "Parties" },
      ],
      section_count: 2,
    },
    {
      report_type: "hearing_preparation",
      title: "Préparation d'audience",
      description: "Ce qu'il faut avoir sous les yeux à la prochaine audience.",
      sections: [{ key: "overview", title: "Aperçu" }],
      section_count: 1,
    },
  ];
}

/** Platform-wide report metrics in the API's wire format. */
export function reportMetricsPayload(overrides: Record<string, unknown> = {}) {
  return {
    total_reports: 10,
    pending: 1,
    processing: 1,
    completed: 6,
    failed: 2,
    success_rate: 75.0,
    failure_rate: 25.0,
    average_duration_ms: 128_000,
    average_duration_seconds: 128.0,
    average_characters: 4820.0,
    total_sections: 24,
    grounded_sections: 20,
    grounding_rate: 83.33,
    total_exports: 4,
    exported_reports: 3,
    total_prompt_tokens: 28_800,
    total_completion_tokens: 9600,
    metered_reports: 6,
    average_total_tokens: 6400.0,
    reports_by_type: { case_summary: 4, evidence_summary: 2 },
    failures_by_code: { llm_unavailable: 1, insufficient_context: 1 },
    window_days: null,
    available_formats: ["markdown", "pdf"],
    template_version: 1,
    llm_available: true,
    prompt_available: true,
    enabled: true,
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// Notification fixtures
// --------------------------------------------------------------------------- //

/**
 * One notification in the API's wire format.
 *
 * `title` and `message` are **rendered by the server**, not stored — a
 * notification row keeps a rule key and a small context, and the wording is
 * produced per request in the reader's language. So these fixtures carry
 * finished prose exactly as a response would, and there is deliberately no
 * client-side template for a test to disagree with.
 */
export function notificationPayload(overrides: Record<string, unknown> = {}) {
  return {
    id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    category: "case",
    notification_type: "information",
    priority: "normal",

    title: "Nouveau dossier",
    message: "Le dossier CASE-2026-0001 a été créé.",
    language: "fr",

    event_type: "case.created",
    rule_key: "case.created",

    case_id: "22222222-2222-4222-8222-222222222222",
    actor: {
      id: "11111111-1111-4111-8111-111111111111",
      full_name: "Amina Benali",
      role: "administrator",
    },
    target: {
      target_type: "case",
      target_id: "22222222-2222-4222-8222-222222222222",
    },

    read_at: null,
    is_read: false,
    created_at: "2026-08-08T09:30:00Z",
    ...overrides,
  };
}

/** One page of the feed, with the badge state the panel draws beside it. */
export function notificationPagePayload(overrides: Record<string, unknown> = {}) {
  const items = (overrides.items as unknown[] | undefined) ?? [notificationPayload()];
  const unread = items.filter(
    (item) => !(item as { is_read?: boolean }).is_read,
  ).length;

  return {
    items,
    total_records: items.length,
    unread_count: unread,
    unread_count_capped: false,
    page: 1,
    page_size: 20,
    total_pages: 1,
    ...overrides,
  };
}

/** The bell's state, from `/notifications/summary`. */
export function notificationSummaryPayload(overrides: Record<string, unknown> = {}) {
  return {
    unread_count: 3,
    unread_count_capped: false,
    total_count: 7,
    unread_by_category: { case: 2, report: 1 },
    highest_unread_priority: "high",
    ...overrides,
  };
}

/**
 * Every preference, at its default.
 *
 * The complete set rather than only stored rows, exactly as the API sends it —
 * which is what lets a settings page render from one response and a preference
 * added later appear automatically.
 */
/**
 * A complete preferences response.
 *
 * `overrides` is keyed by preference and sets **every channel**, which is the
 * common case a test wants ("this one is switched off"). A test that needs the
 * channels to differ — the setting the outbound channels exist for — passes
 * `channelOverrides`, keyed by preference then by channel.
 *
 * **Every channel the API sends must appear here**, because
 * `lib/validation/notification.ts` parses the payload strictly: a fixture missing
 * one turns every preference test into "the query failed" rather than into a
 * useful assertion. `whatsapp` was the third channel to arrive and is the one
 * that proved the point.
 */
export function notificationPreferencesPayload(
  overrides: Record<string, boolean> = {},
  channelOverrides: Record<string, Partial<Record<"inApp" | "email" | "whatsapp", boolean>>> = {},
) {
  const keys = [
    "case_updates",
    "document_updates",
    "ocr_completion",
    "ai_report_completion",
    "hearing_updates",
    "account_activity",
    "system_announcements",
  ] as const;

  return {
    preferences: keys.map((key) => {
      const perChannel = channelOverrides[key] ?? {};
      const base = overrides[key] ?? true;

      return {
        preference_key: key,
        in_app: perChannel.inApp ?? base,
        email: perChannel.email ?? base,
        whatsapp: perChannel.whatsapp ?? base,
        is_default: !(key in overrides) && !(key in channelOverrides),
      };
    }),
  };
}

/** Platform-wide notification metrics in the API's wire format. */
export function notificationMetricsPayload(overrides: Record<string, unknown> = {}) {
  return {
    since: "2026-08-08T08:00:00Z",
    enabled: true,

    total_notifications: 42,
    unread_notifications: 9,
    read_notifications: 33,
    read_rate: 78.57,
    recipients: 6,

    created: 42,
    delivered: 41,
    failed: 1,
    suppressed_by_preference: 3,
    deduplicated: 2,
    dropped: 0,
    pending: 0,

    average_delivery_latency_ms: 12.4,

    notifications_by_category: { case: 20, document: 14, report: 8 },
    created_by_rule: { "case.created": 20, "document.uploaded": 14 },
    failures_by_reason: { delivery_failed: 1 },

    window_days: null,
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// Semantic Search fixtures
// --------------------------------------------------------------------------- //

/** One retrieved passage in the API's wire format. */
export function searchResultPayload(overrides: Record<string, unknown> = {}) {
  return {
    document_id: "33333333-3333-4333-8333-333333333333",
    document_version: 1,
    case_id: "22222222-2222-4222-8222-222222222222",
    page_number: 4,
    chunk_number: 2,
    score: 0.8421,
    text:
      "Article 4 : Loyer et charges. Le loyer mensuel est payable d'avance le premier " +
      "jour de chaque mois, au domicile du bailleur.",
    language: "fr",
    rank: 1,
    document: {
      id: "33333333-3333-4333-8333-333333333333",
      case_id: "22222222-2222-4222-8222-222222222222",
      original_filename: "bail-commercial.pdf",
      file_extension: "pdf",
      category: "contract",
    },
    ...overrides,
  };
}

/** A search response, as `POST /search` returns it. */
export function searchResponsePayload(
  results: Array<Record<string, unknown>> = [searchResultPayload()],
  overrides: Record<string, unknown> = {},
) {
  const scores = results.map((result) => Number(result.score ?? 0));

  return {
    query: "loyer payable d'avance",
    results,
    result_count: results.length,
    limit: 10,
    offset: 0,
    has_more: false,
    duration_ms: 42,
    top_score: scores.length ? Math.max(...scores) : null,
    average_score: scores.length
      ? Number((scores.reduce((total, score) => total + score, 0) / scores.length).toFixed(4))
      : null,
    is_empty: results.length === 0,
    ...overrides,
  };
}

/** Search metrics, as `GET /search/metrics` returns them. */
export function searchMetricsPayload(overrides: Record<string, unknown> = {}) {
  return {
    since: "2026-08-01T09:00:00Z",
    total_searches: 25,
    successful_searches: 23,
    failed_searches: 2,
    success_rate: 92.0,
    failure_rate: 8.0,
    average_latency_ms: 138.5,
    average_latency_seconds: 0.139,
    average_score: 0.7412,
    total_results: 187,
    average_results: 8.13,
    failures_by_code: { vector_store_unavailable: 2 },
    embedding_model: "BAAI/bge-m3",
    embedding_dimensions: 1024,
    embedding_available: true,
    vector_collection: "document_chunks",
    vector_store_available: true,
    ranker: "similarity",
    default_limit: 10,
    max_limit: 50,
    min_score: 0.0,
    enabled: true,
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// AI Legal Assistant fixtures
// --------------------------------------------------------------------------- //

/**
 * One citation in the API's wire format.
 *
 * Deliberately the *RAG pipeline's* shape, because that is exactly what the
 * assistant returns: the spec requires citations to be displayed without
 * modification, and a fixture with a different shape here would be testing a
 * payload the server never sends.
 */
export function assistantCitationPayload(overrides: Record<string, unknown> = {}) {
  return {
    marker: 1,
    document_id: "33333333-3333-4333-8333-333333333333",
    document_name: "bail-commercial.pdf",
    document_version: 1,
    page_number: 4,
    case_id: "22222222-2222-4222-8222-222222222222",
    score: 0.8421,
    excerpt:
      "Article 4 : Loyer et charges. Le loyer mensuel est payable d'avance le premier " +
      "jour de chaque mois, au domicile du bailleur.",
    excerpt_truncated: false,
    referenced: true,
    ...overrides,
  };
}

/** One message in the API's wire format. */
export function conversationMessagePayload(overrides: Record<string, unknown> = {}) {
  const role = (overrides.role as string | undefined) ?? "assistant";
  const isAssistant = role === "assistant";

  return {
    id: "77777777-7777-4777-8777-777777777777",
    conversation_id: "66666666-6666-4666-8666-666666666666",
    sequence: isAssistant ? 2 : 1,
    role,
    content: isAssistant
      ? "Le loyer mensuel est payable d'avance le premier jour de chaque mois [1]."
      : "Quand le loyer est-il payable ?",
    language: "fr",
    citations: isAssistant ? [assistantCitationPayload()] : [],
    suggestions: isAssistant ? ["Quelle est la durée du bail ?"] : [],
    citation_count: isAssistant ? 1 : 0,
    document_count: isAssistant ? 1 : 0,
    grounded: isAssistant ? true : null,
    insufficient_evidence: isAssistant ? false : null,
    truncated: false,
    provider: isAssistant ? "gemini" : null,
    model: isAssistant ? "gemini-2.5-flash" : null,
    prompt_name: isAssistant ? "rag/answer" : null,
    prompt_version: isAssistant ? 1 : null,
    duration_ms: isAssistant ? 2800 : null,
    retrieval_ms: isAssistant ? 210 : null,
    generation_ms: isAssistant ? 2400 : null,
    prompt_tokens: isAssistant ? 828 : null,
    completion_tokens: isAssistant ? 24 : null,
    total_tokens: isAssistant ? 852 : null,
    retrieved_count: isAssistant ? 3 : null,
    context_count: isAssistant ? 3 : null,
    context_turns: 0,
    top_score: isAssistant ? 0.8421 : null,
    edited_at: null,
    created_at: "2026-08-06T10:00:00Z",
    feedback: null,
    ...overrides,
  };
}

/** One conversation in the API's wire format. */
export function conversationPayload(overrides: Record<string, unknown> = {}) {
  return {
    id: "66666666-6666-4666-8666-666666666666",
    title: "Quand le loyer est-il payable ?",
    title_is_custom: false,
    status: "active",
    language: null,
    case_id: null,
    message_count: 2,
    last_message_at: "2026-08-06T10:00:00Z",
    last_message_preview: "Le loyer mensuel est payable d'avance…",
    created_at: "2026-08-06T09:59:00Z",
    updated_at: "2026-08-06T10:00:00Z",
    ...overrides,
  };
}

/** A conversation with its transcript, as `GET /assistant/conversations/{id}` returns it. */
export function conversationDetailPayload(
  messages: Array<Record<string, unknown>> = [
    conversationMessagePayload({ role: "user" }),
    conversationMessagePayload(),
  ],
  overrides: Record<string, unknown> = {},
) {
  return {
    ...conversationPayload({ message_count: messages.length }),
    messages,
    has_more_messages: false,
    ...overrides,
  };
}

/** A page of conversations, as `GET /assistant/conversations` returns it. */
export function conversationPagePayload(
  items: Array<Record<string, unknown>> = [conversationPayload()],
  overrides: Record<string, unknown> = {},
) {
  return {
    items,
    total_records: items.length,
    page: 1,
    page_size: 20,
    total_pages: 1,
    ...overrides,
  };
}

/** A page of messages, as `GET /assistant/conversations/{id}/messages` returns it. */
export function conversationMessagePagePayload(
  items: Array<Record<string, unknown>> = [conversationMessagePayload()],
  overrides: Record<string, unknown> = {},
) {
  return {
    items,
    total_records: items.length,
    page: 1,
    page_size: 50,
    total_pages: 1,
    ...overrides,
  };
}

/** One exchange, as `POST /assistant/conversations/{id}/messages` returns it. */
export function messageExchangePayload(overrides: Record<string, unknown> = {}) {
  return {
    conversation: conversationPayload(),
    user_message: conversationMessagePayload({ role: "user" }),
    assistant_message: conversationMessagePayload(),
    ...overrides,
  };
}

/** Assistant metrics, as `GET /assistant/metrics` returns them. */
export function assistantMetricsPayload(overrides: Record<string, unknown> = {}) {
  return {
    since: "2026-08-01T09:00:00Z",
    total_conversations: 12,
    active_conversations: 9,
    archived_conversations: 3,
    total_messages: 68,
    average_conversation_length: 5.67,
    total_requests: 34,
    successful_requests: 32,
    failed_requests: 2,
    success_rate: 94.12,
    failure_rate: 5.88,
    streamed_requests: 28,
    average_response_ms: 2840.5,
    average_response_seconds: 2.841,
    grounded_answers: 27,
    insufficient_evidence: 5,
    grounding_rate: 84.38,
    total_feedback: 11,
    helpful_feedback: 9,
    not_helpful_feedback: 2,
    helpful_rate: 81.82,
    rated_messages_rate: 34.38,
    failures_by_code: { llm_unavailable: 2 },
    suggestions_enabled: true,
    streaming_enabled: true,
    enabled: true,
    ...overrides,
  };
}

// --------------------------------------------------------------------------- //
// Timeline fixtures
// --------------------------------------------------------------------------- //

/**
 * A timeline event in the API's wire format, as `GET /cases/{id}/timeline`
 * returns it.
 *
 * Defaults to a document upload by the signed-in administrator, which is the
 * shape the spec's own example uses. `category` is included because the API
 * computes and serves it — a test that omitted it would be asserting against a
 * payload the server never sends.
 */
export function timelineEventPayload(overrides: Record<string, unknown> = {}) {
  return {
    id: "55555555-5555-4555-8555-555555555555",
    case_id: legalCasePayload().id,
    event_type: "document_uploaded",
    category: "document",
    title: "Document Uploaded",
    description: 'Amina Benali uploaded "contrat-de-bail.pdf".',
    actor_id: TEST_USER.id,
    actor_name: TEST_USER.full_name,
    actor_role: "administrator",
    metadata: { filename: "contrat-de-bail.pdf", version: 1 },
    created_at: "2026-07-20T14:32:00Z",
    ...overrides,
  };
}

/** A page of timeline events in the API's wire format. */
export function timelinePagePayload(
  items: Array<Record<string, unknown>> = [timelineEventPayload()],
  overrides: Record<string, unknown> = {},
) {
  return {
    items,
    total_records: items.length,
    page: 1,
    page_size: 20,
    total_pages: 1,
    ...overrides,
  };
}

/** A single scripted response for one endpoint. */
export interface RouteResponse {
  status?: number;
  body?: unknown;
  /** Throw a network-level failure instead of responding. */
  networkError?: boolean;
  /**
   * Answer with these raw bytes instead of JSON — for the download and preview
   * endpoints, which return a file rather than an envelope.
   */
  binary?: { content: string; contentType?: string; disposition?: string };
}

export interface RecordedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
  credentials?: RequestCredentials;
}

/**
 * Install a `fetch` double that answers based on URL substrings.
 *
 * A route value may be a single response or an array, in which case successive
 * calls to that endpoint consume it in order — that is what makes
 * refresh-then-retry sequences testable.
 */
export function mockFetch(routes: Record<string, RouteResponse | RouteResponse[]>) {
  const requests: RecordedRequest[] = [];
  const queues = new Map<string, RouteResponse[]>(
    Object.entries(routes).map(([key, value]) => [key, Array.isArray(value) ? [...value] : [value]]),
  );

  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const headers = (init?.headers ?? {}) as Record<string, string>;

    requests.push({
      url,
      method: init?.method ?? "GET",
      headers,
      body: readBody(init?.body),
      credentials: init?.credentials,
    });

    const key = [...queues.keys()].find((candidate) => url.includes(candidate));
    if (!key) throw new Error(`Unexpected request in test: ${url}`);

    const queue = queues.get(key)!;
    // The last entry repeats, so tests only script the calls they care about.
    const route = queue.length > 1 ? queue.shift()! : queue[0]!;

    if (route.networkError) throw new TypeError("Failed to fetch");

    const status = route.status ?? 200;

    if (route.binary) {
      return new Response(route.binary.content, {
        status,
        headers: {
          "Content-Type": route.binary.contentType ?? "application/octet-stream",
          ...(route.binary.disposition
            ? { "Content-Disposition": route.binary.disposition }
            : {}),
        },
      });
    }

    return new Response(route.body === undefined ? null : JSON.stringify(route.body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });

  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, requests };
}

/**
 * Record a request body without assuming it is JSON.
 *
 * Uploads send `FormData`, which `JSON.parse` chokes on. Flattening it to a plain
 * object is what lets an upload test assert on the fields it carried.
 */
function readBody(body: BodyInit | null | undefined): unknown {
  if (body === null || body === undefined) return undefined;
  if (typeof body === "string") {
    try {
      return JSON.parse(body);
    } catch {
      return body;
    }
  }
  if (body instanceof FormData) return formDataToObject(body);
  return body;
}

export function formDataToObject(form: FormData): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of form.entries()) {
    result[key] = value instanceof File ? { name: value.name, size: value.size } : value;
  }
  return result;
}

// --------------------------------------------------------------------------- //
// XMLHttpRequest double (multipart uploads)
//
// `lib/api/upload.ts` uses XHR rather than `fetch`, because `fetch` cannot report
// upload progress — and progress is a requirement for document uploads. That
// means the scripted `fetch` above cannot see those calls, so they get their own
// double with the same shape: script a response, inspect what was sent.
// --------------------------------------------------------------------------- //

export interface RecordedUpload {
  url: string;
  method: string;
  headers: Record<string, string>;
  fields: Record<string, unknown>;
  withCredentials: boolean;
}

export interface UploadResponse {
  status?: number;
  body?: unknown;
  /** Fire an `error` event instead of responding, i.e. the server is unreachable. */
  networkError?: boolean;
  /** Progress events to emit before the response, as `[loaded, total]` pairs. */
  progress?: Array<[number, number]>;
  /**
   * Emit the progress events but withhold the response until `release()` is
   * called. Without this a request completes on the next macrotask, which is
   * faster than any assertion can observe — so an in-flight state (a progress
   * bar, a disabled button) would appear to never render at all.
   */
  hold?: boolean;
}

/**
 * Install an `XMLHttpRequest` double that answers every multipart request.
 *
 * A queue, like {@link mockFetch}: successive uploads consume it in order and the
 * last entry repeats.
 */
export function mockUpload(responses: UploadResponse | UploadResponse[] = {}) {
  const uploads: RecordedUpload[] = [];
  const queue = Array.isArray(responses) ? [...responses] : [responses];
  const held: Array<() => void> = [];

  class FakeXhr {
    status = 0;
    responseText = "";
    withCredentials = false;
    upload = new EventTarget();

    private url = "";
    private method = "GET";
    private readonly headers: Record<string, string> = {};
    private readonly listeners = new Map<string, Array<() => void>>();
    private readonly responseHeaders: Record<string, string> = {
      "Content-Type": "application/json",
    };

    open(method: string, url: string): void {
      this.method = method;
      this.url = url;
    }

    setRequestHeader(name: string, value: string): void {
      this.headers[name] = value;
    }

    getResponseHeader(name: string): string | null {
      return this.responseHeaders[name] ?? null;
    }

    addEventListener(type: string, listener: () => void): void {
      this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
    }

    abort(): void {
      this.emit("abort");
    }

    send(form: FormData): void {
      uploads.push({
        url: this.url,
        method: this.method,
        headers: { ...this.headers },
        fields: formDataToObject(form),
        withCredentials: this.withCredentials,
      });

      const route = queue.length > 1 ? queue.shift()! : (queue[0] ?? {});

      // Asynchronous, like a real request: a synchronous resolution would let a
      // component skip its pending state entirely and hide a bug in it.
      setTimeout(() => {
        for (const [loaded, total] of route.progress ?? []) {
          this.upload.dispatchEvent(
            Object.assign(new Event("progress"), { lengthComputable: true, loaded, total }),
          );
        }

        const complete = () => {
          if (route.networkError) {
            this.emit("error");
            return;
          }

          this.status = route.status ?? 201;
          this.responseText = route.body === undefined ? "" : JSON.stringify(route.body);
          this.emit("load");
        };

        if (route.hold) {
          held.push(complete);
          return;
        }
        complete();
      }, 0);
    }

    private emit(type: string): void {
      for (const listener of this.listeners.get(type) ?? []) listener();
    }
  }

  vi.stubGlobal("XMLHttpRequest", FakeXhr);

  /** Let every held request finish. */
  function release(): void {
    for (const complete of held.splice(0)) complete();
  }

  return { uploads, release };
}

// --------------------------------------------------------------------------- //
// Settings fixtures
// --------------------------------------------------------------------------- //

/**
 * The user-settings registry, in the API's wire format.
 *
 * **Definitions travel with the values**, exactly as the API sends them: the
 * client renders a control from the definition rather than from a table of its
 * own, so a fixture that omitted them would exercise a code path production does
 * not have.
 */
const SETTING_DEFINITIONS = [
  {
    key: "theme",
    section: "appearance",
    value_type: "enum",
    choices: ["light", "dark", "system"],
    max_length: null,
    max_items: null,
  },
  {
    key: "language",
    section: "language",
    value_type: "enum",
    choices: ["fr", "ar", "en"],
    max_length: null,
    max_items: null,
  },
  {
    key: "timezone",
    section: "language",
    value_type: "timezone",
    choices: [],
    max_length: null,
    max_items: null,
  },
  {
    key: "date_format",
    section: "language",
    value_type: "enum",
    choices: ["day_month_year", "month_day_year", "year_month_day", "long"],
    max_length: null,
    max_items: null,
  },
  {
    key: "time_format",
    section: "language",
    value_type: "enum",
    choices: ["hour_24", "hour_12"],
    max_length: null,
    max_items: null,
  },
  {
    key: "ai_response_length",
    section: "ai",
    value_type: "enum",
    choices: ["concise", "balanced", "detailed"],
    max_length: null,
    max_items: null,
  },
  {
    key: "ai_streaming",
    section: "ai",
    value_type: "boolean",
    choices: [],
    max_length: null,
    max_items: null,
  },
  {
    key: "ai_citations",
    section: "ai",
    value_type: "enum",
    choices: ["inline", "list", "hidden"],
    max_length: null,
    max_items: null,
  },
  {
    key: "dashboard_range",
    section: "dashboard",
    value_type: "enum",
    choices: ["today", "last_7_days", "last_30_days"],
    max_length: null,
    max_items: null,
  },
  {
    key: "dashboard_widgets",
    section: "dashboard",
    value_type: "string_list",
    choices: ["my_cases", "recent_cases", "upcoming_hearings"],
    max_length: null,
    max_items: 3,
  },
] as const;

const SETTING_DEFAULTS: Record<string, boolean | string | string[]> = {
  theme: "dark",
  language: "fr",
  timezone: "UTC",
  date_format: "day_month_year",
  time_format: "hour_24",
  ai_response_length: "balanced",
  ai_streaming: true,
  ai_citations: "list",
  dashboard_range: "last_30_days",
  dashboard_widgets: [],
};

/**
 * A complete settings response.
 *
 * `overrides` is keyed by setting; anything absent comes back at its platform
 * default with `is_default: true`, which is what an account that has never opened
 * the page actually looks like.
 */
export function settingsCollectionPayload(
  overrides: Record<string, boolean | string | string[]> = {},
) {
  return {
    settings: SETTING_DEFINITIONS.map((definition) => ({
      key: definition.key,
      section: definition.section,
      value: overrides[definition.key] ?? SETTING_DEFAULTS[definition.key],
      is_default: !(definition.key in overrides),
    })),
    definitions: SETTING_DEFINITIONS.map((definition) => ({ ...definition })),
  };
}

/** A profile in the API's wire format. */
export function profilePayload(overrides: Record<string, unknown> = {}) {
  return {
    id: TEST_USER.id,
    email: TEST_USER.email,
    first_name: "Amina",
    last_name: "Benali",
    full_name: "Amina Benali",
    phone: "+212 612345678",
    profile_image: null,
    job_title: null,
    role: "administrator",
    status: "active",
    must_change_password: false,
    last_login_at: "2026-08-10T08:30:00Z",
    created_at: "2026-07-01T09:00:00Z",
    updated_at: "2026-08-10T08:30:00Z",
    ...overrides,
  };
}

/** The section catalog, as the API orders it. */
export function settingsSectionsPayload(includeAdministration = false) {
  const sections = [
    ["profile", "profile"],
    ["security", "account"],
    ["notifications", "notification_preferences"],
    ["communication", "notification_preferences"],
    ["ai", "user_settings"],
    ["dashboard", "user_settings"],
    ["appearance", "user_settings"],
    ["language", "user_settings"],
  ];
  if (includeAdministration) sections.push(["administration", "platform_settings"]);

  return sections.map(([section, storage]) => ({
    section,
    storage,
    editable: true,
    administrative: storage === "platform_settings",
  }));
}

/** Everything `GET /settings` returns. */
export function settingsOverviewPayload(overrides: Record<string, unknown> = {}) {
  return {
    sections: settingsSectionsPayload(),
    profile: profilePayload(),
    settings: settingsCollectionPayload(),
    maintenance: { maintenance_mode: false, message: null },
    ...overrides,
  };
}

/** A live sign-in in the API's wire format. */
export function sessionPayload(overrides: Record<string, unknown> = {}) {
  return {
    session_id: "session-1",
    is_current: true,
    created_at: "2026-08-11T08:00:00Z",
    last_seen_at: "2026-08-11T09:30:00Z",
    expires_at: "2026-08-18T08:00:00Z",
    ip_address: "10.0.0.1",
    user_agent: "Mozilla/5.0 (Windows NT 10.0)",
    ...overrides,
  };
}

/** The sessions list, with the availability flag the registry reports. */
export function sessionListPayload(
  sessions: Array<Record<string, unknown>> = [sessionPayload()],
  available = true,
) {
  return { sessions, available };
}
