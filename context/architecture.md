# Architecture Context

## Stack

| Layer | Technology | Role |
| ------ | ---------- | ---- |
| Frontend | Next.js + TypeScript | Administrator, Lawyer, and Court web interfaces |
| UI | Tailwind CSS + shadcn/ui | Responsive and reusable user interface |
| State Management | TanStack Query | API communication and frontend caching |
| Internationalization | next-intl | Arabic and French localization with RTL support |
| Forms | React Hook Form + Zod | Form handling and validation |
| Backend | FastAPI | REST APIs, business logic, authentication, AI orchestration, and real-time services |
| Real-Time Communication | FastAPI WebSockets | Synchronize case updates instantly between users |
| ORM | SQLAlchemy + Alembic | Database interaction and migrations |
| Database | PostgreSQL | Store users, legal cases, lawyers, court information, reports, notifications, audit logs, and metadata |
| Vector Database | Qdrant | Semantic search and Retrieval-Augmented Generation (RAG) |
| Object Storage | MinIO | Store legal documents, generated reports, and future voice recordings |
| Cache & Messaging | Redis | Cache, background queues, WebSocket Pub/Sub, and temporary data |
| AI Framework | LangGraph | Multi-agent orchestration |
| LLM Gateway | LiteLLM | Unified interface for OpenAI, Ollama, and future models |
| Embeddings | BAAI bge-m3 | Generate document embeddings |
| OCR | Tesseract OCR + OCRmyPDF | Text extraction from scanned documents |
| Background Jobs | Trigger.dev (or Celery + Redis) | OCR, indexing, notifications, and scheduled tasks |
| Email Service | SMTP / Mailpit | Email notifications |
| WhatsApp Integration | WhatsApp Business API | Real-time WhatsApp alerts |
| Authentication | JWT + OAuth2 | Secure authentication and authorization |
| Monitoring | Langfuse + OpenTelemetry | AI observability and tracing |
| Error Tracking | Sentry | Runtime monitoring |
| AI Evaluation | Ragas + DeepEval | AI quality evaluation |
| Containerization | Docker + Docker Compose | Local development and deployment |
| Reverse Proxy | Nginx | HTTPS termination and routing |
| CI/CD | GitHub Actions | Automated testing and deployment |
| Code Review | CodeRabbit | AI-assisted code review |
| Future Voice | Faster-Whisper + Piper TTS | Speech-to-Text and Text-to-Speech |

---

## System Boundaries

- `apps/web` — Collaborative web application used by administrators, lawyers, and court representatives.
- `apps/api` — Backend responsible for authentication, authorization, business logic, AI orchestration, notifications, and real-time communication.
- `modules/cases` — Case lifecycle management, lawyer assignment, hearings, court decisions, and case timeline.
  Implemented inside `apps/api` (`models/case.py`, `core/cases.py`,
  `repositories/case.py`, `services/case.py`, `services/case_access.py`,
  `api/v1/cases/`) and `apps/web` (`components/cases/`,
  `app/(protected)/cases/`), following the layering the backend already uses
  rather than introducing a separate deployable. Hearings, court decisions, and
  the case timeline are later features that attach to the Case entity.
- `modules/documents` — Document upload, OCR, indexing, versioning, and secure storage.
  Upload, versioning, preview, download, and archiving are implemented inside
  `apps/api` (`models/document.py`, `core/documents.py`,
  `repositories/document.py`, `services/document.py`,
  `services/document_storage.py`, `services/document_validation.py`,
  `services/document_access.py`, `api/v1/documents/`) and `apps/web`
  (`components/documents/`, `app/(protected)/documents/`), following the same
  layering as Cases and Users rather than introducing a separate deployable. OCR,
  indexing, and embeddings are later features that attach to the Document entity.
- `modules/reports` — AI-generated reports, legal summaries, exports, and report history.
- `modules/notifications` — Real-time notifications, email notifications, WhatsApp alerts, and reminder scheduling.
- `modules/users` — Administrator, lawyer, and court representative management.
  Implemented inside `apps/api` (`services/user.py`, `repositories/user.py`,
  `api/v1/users/`) and `apps/web` (`components/users/`, `app/(protected)/users/`),
  following the layering the backend already uses rather than introducing a
  separate deployable.
- `modules/localization` — Arabic and French translations, language switching, and RTL support.
- `services/ai` — Retrieval-Augmented Generation (RAG), semantic search, summarization, information extraction, report generation, compliance checking, and multilingual AI services.
- `services/workers` — Background workers responsible for OCR, embeddings, indexing, notifications, and scheduled tasks.
- `packages/shared` — Shared DTOs, schemas, utilities, constants, and API contracts.
- `infrastructure` — Docker Compose, Nginx, monitoring, deployment scripts, CI/CD, and environment configuration.

---

## Storage Model

### PostgreSQL

Stores structured business data:

- Users
- Roles
- Permissions
- Administrators
- Lawyers
- Court Representatives
- Legal Cases
- Documents (metadata only)
- Document Versions
- Lawyer Assignments
- Clients
- Hearings
- Court Decisions
- Reports
- Notifications
- Timeline Events
- Audit Logs
- AI Conversations
- Language Preferences

---

### Qdrant

Stores AI knowledge:

- Document embeddings
- Semantic search indexes
- Chunk metadata
- Retrieval information

---

### MinIO

Stores files:

- PDF documents
- DOCX documents
- Images
- Scanned files
- Generated reports
- OCR outputs
- Exported documents
- Future voice recordings

Case documents live in the `MINIO_DOCUMENTS_BUCKET` bucket (`legal-documents` by
default, created on first upload), keyed
`cases/{case_id}/documents/{document_id}/v{version}/{stored_filename}`. Because
the key carries the version, a replacement cannot overwrite its predecessor —
"preserve previous versions" is a property of the layout rather than a rule the
service must remember. `stored_filename` is a generated UUID, never derived from
the uploaded name, so a crafted filename cannot influence the layout. **Nothing
is ever physically deleted**: `services/document_storage.py` deliberately
exposes no destructive operation, and deletion is a metadata change
(`documents.deleted_at`), leaving a future cleanup job to reclaim storage.

---

### Redis

Stores temporary data:

- API cache
- Session cache
- Revoked JWT identifiers (logout / refresh-token rotation denylist, keyed by
  `jti` with a TTL equal to the token's remaining lifetime)
- Failed-login counters and lockouts (per account and per client IP, keyed by
  scope with a TTL equal to the failure window / lockout duration)
- WebSocket Pub/Sub messages
- Background job queues
- Rate limiting
- Notification queues

---

## Auth and Access Model

The platform supports three main user roles:

### Administrator

- Create and manage legal cases.
- Assign lawyers.
- Upload documents.
- Generate reports.
- Manage users.
- Monitor platform activity.

### Lawyer

- Access only assigned legal cases.
- View and upload documents.
- Receive court updates.
- Generate AI summaries and reports.
- Collaborate with administrators.

### Court Representative

- Update hearings and court decisions.
- Upload official documents.
- View authorized case information.
- Trigger case status updates.

---

Authentication

- JWT authentication.
- OAuth2 support (future).
- Password hashing using Argon2 or bcrypt.

Implemented token strategy (see `context/feature-specs/03-authentication.md`):

- Short-lived **access token** (15 min), sent as an `Authorization: Bearer`
  credential and held in browser memory only — never in `localStorage`,
  `sessionStorage`, or a script-readable cookie.
- Long-lived **refresh token** (7 days), delivered to browsers as an
  `httpOnly; SameSite=strict` cookie so script cannot read it. Because normal API
  calls authenticate by header rather than cookie, this combination is CSRF-safe.
- Refresh tokens are **single-use and rotated**; the consumed token is revoked, so
  a replayed refresh token is rejected.
- Logout and rotation revoke tokens by `jti` through the Redis denylist above,
  which is what makes sign-out effective despite JWTs being stateless. The
  denylist **fails closed** — if Redis is unreachable, the request is rejected.
- **Bulk session revocation** uses a per-user `session_generation` counter stored
  in PostgreSQL and embedded in every token as the `sgen` claim. A token whose
  generation is behind the user's is rejected, so incrementing the counter
  invalidates every session for that user in one write. A **password change**
  increments it: all other devices must authenticate again, while the device making
  the change receives a replacement token pair and stays signed in.
- **Brute-force protection:** consecutive failed sign-ins are counted in Redis per
  account and per client IP. Crossing the threshold refuses further attempts with
  **HTTP 429** and a `Retry-After` header until the lockout expires. Checked before
  credentials are verified, and a successful sign-in clears the counters. The
  per-IP counter derives the address from `X-Forwarded-For` only when the app is
  configured to trust a reverse proxy.
- Accounts are provisioned by administrators through the User Management API
  (`POST /api/v1/users`); there is **no self-registration**.
  `scripts/create_user.py` remains the **bootstrap** path only — it creates the
  first administrator, before any account exists to authorize that call.
- **Account status, not a boolean, decides who may sign in.** `users.status` is
  `active` | `inactive` | `suspended`; only `active` authenticates.
  `User.is_active` is a derived property over it, so authentication has a single
  question to ask and the two can never disagree.
- **Administrator-initiated password reset** (`POST /users/{id}/reset-password`)
  generates a temporary password with a cryptographic RNG, stores only its hash,
  returns it once in the response, flags the account `must_change_password`, and
  revokes every session for that user via the same `session_generation` counter.
  Deactivating an account revokes its sessions the same way, so a disabled user
  loses access immediately rather than when their access token expires.

Authorization

Role-Based Access Control, implemented per
`context/feature-specs/04-authorization-rbac.md`:

- **Permissions are the unit of access**, not roles. Every capability is named
  once in `core/permissions.py` as a `group:action` identifier (`cases:view`,
  `ai:generate-report`). Enforcement code names a permission; it never branches
  on a role.
- **Roles map to permission sets** in `core/roles.py`. `UserRole` (persisted on
  the user record) is the canonical role definition; `ROLE_PERMISSIONS` is the
  only place that decides what each role may do, so policy can be refined by
  later features without touching a single call site. Administrators hold every
  permission by reference, so new permissions reach them automatically.
- **A permission grants a capability, not a row.** "Lawyers can only access
  assigned cases" and "court representatives access only authorized cases" are
  per-resource rules layered on top of `cases:view`. Case Management implements
  them in `services/case_access.py`, expressed as capabilities rather than role
  checks:
  - `cases:view-all` lifts the row restriction. A caller who holds it reads
    every case; everyone else is scoped, **in the SQL query**, to the cases they
    are assigned to as lawyer or court representative — so page totals count
    only what the caller may access.
  - `cases:update-hearing` is the narrow half of `cases:update`, covering the
    court-facing fields only (court name, filing date, next hearing date, and
    the status change that follows). Court representatives hold it instead of
    the full `cases:update`, which matches their role description exactly.
  - Write access is decided **per field**: `cases:update` covers the whole case,
    `cases:update-hearing` the court fields, `cases:assign` the two assignment
    fields. A request touching a field the caller cannot reach is refused *in
    full* — never applied in part.
- **`AuthorizationService`** (`services/authorization.py`) evaluates every
  access decision — require role / permission / any / all — in both a boolean
  (`has_*`) and a raising (`require_*`) form. It is stateless and pure.
- **Endpoints are guarded by reusable dependencies** (`api/authorization.py`):
  `require_role`, `require_permission`, `require_any_permission`,
  `require_all_permissions`. Authentication resolves first, so an anonymous
  caller gets **401** and only an authenticated-but-unauthorized one gets
  **403**. A 403 body never names the required role or permission; the specifics
  go to the log (`authorization_denied`, with user id and role only).
- **Permissions ride on the authentication context.** Every user payload
  (`login`, `refresh`, `GET /auth/me`, `change-password`) carries the role's
  effective `permissions`, computed rather than stored, so the client cannot
  drift from server policy. `GET /api/v1/authorization/me` returns the caller's
  own grants; `GET /api/v1/authorization/roles` serves the full catalog and
  requires `users:view`.
- **The frontend gates presentation only.** `usePermissions` / `useRole`,
  `<Protected>`, `<ProtectedRoute>`, and the sidebar filter all evaluate the
  same `AccessRule` shape against the server-supplied permission list. Route
  requirements are declared once in `config/navigation.ts`, so the sidebar can
  never offer a destination the route guard would block. None of this is a
  security boundary — every request is authorized independently by the API.

---

### Case Lifecycle

Implemented per `context/feature-specs/06-case-management.md`:

- A case is the platform's central entity; every later module (documents,
  timeline, hearings, reports, AI conversations, notifications) attaches to it.
- **Statuses:** `draft` → `open` → `in_progress` ↔ `waiting_for_hearing` →
  `closed`, with `archived` reachable from anywhere. The legal moves are
  declared once in `core/cases.py` (`STATUS_TRANSITIONS`) and served to clients
  on every case as `allowed_transitions`, so the UI cannot offer a transition
  the API would refuse. Nothing returns *to* `draft`; an archived case is
  restored to `open`.
- **Archiving is the soft delete.** `DELETE /cases/{id}` sets
  `status = archived`; the row is never removed, because documents, timeline
  entries, and audit records reference it. Archived cases stay listed and
  searchable, exactly as the spec requires.
- **Priority** (`low` / `medium` / `high` / `urgent`) is ordered by
  `PRIORITY_RANK`, not by its stored value — sorting alphabetically would place
  `urgent` below `low`. The repository builds its `ORDER BY` from that rank.
- **Case numbers** are unique (enforced by a database index, not only by the
  service) and generated as `CASE-YYYY-NNNN` when a request omits one. A
  client-supplied registry number is uppercased and stored verbatim, and does
  not disturb the generated series.
- **Assignments** are validated against the assignee's role and status: the
  lawyer position accepts only an active account holding the lawyer role, the
  representative position only an active account holding the court role. An
  unchanged assignment is not re-validated, so a case whose lawyer was later
  deactivated stays editable.

### Document Lifecycle

Implemented per `context/feature-specs/07-document-management.md`:

- A document belongs to exactly one case, and **its access follows its case's**.
  `services/document_access.py` owns no policy of its own; it delegates to
  `CaseAccessPolicy`, so document visibility cannot drift from case visibility.
  The list scope is applied in SQL (via the shared `assigned_case_scope` clause
  that `repositories/case.py` exports) so page totals count only what the caller
  may reach.
- **Two tables, because a document has two lifetimes.** `documents` is the
  current state — the file downloaded today, plus the category and description
  that survive a replacement. `document_versions` is one immutable row per
  uploaded file, including the current one. The document row's binary columns
  *mirror* its current version; that denormalization has a single writer and is
  what lets the list search filenames, filter by type, and sort by size against
  one table with no join and no "latest version" window function.
- **Categories** (`contract`, `evidence`, `court_decision`, `pleading`,
  `correspondence`, `invoice`, `identity_document`, `other`) are declared once as
  a PostgreSQL enum; `CATEGORY_RANK` derives the display and sort order from the
  declaration order, so "sort by category" keeps `other` last instead of
  alphabetically in the middle.
- **A document's MIME type comes from its extension, never from the client's
  `Content-Type`** — the browser-supplied value is attacker-controlled and is what
  would decide how an inline preview renders.
- **Uploads are validated in one place** (`services/document_validation.py`):
  missing, empty, oversized, unsupported type, and *corrupted* (the leading bytes
  must match the declared format). The filename is sanitised — directory
  components discarded, control and header-special characters replaced — because
  it reaches a `Content-Disposition` header.
- **Preview and download stream from object storage** with
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none';
  sandbox`, and `Cache-Control: private, no-store`. A type no browser renders
  answers **415**, and the document's computed `is_previewable` says so in
  advance, so a client never offers a preview the API will refuse.
- **Deletion is logical.** `DELETE /documents/{id}` sets `deleted_at`; the row and
  every stored file are kept, and the operation is idempotent.

## Invariants

1. Every legal case must have one administrator and may have one or more assigned lawyers.

2. Every case update must be synchronized immediately across all authorized users through real-time communication.

3. Every important event (case update, document upload, hearing update, report generation, etc.) must generate a persistent notification.

4. Notifications may be delivered through the platform, email, and WhatsApp depending on user preferences and event type.

5. Every AI response must be grounded using Retrieval-Augmented Generation (RAG) and company legal documents.

6. Original legal documents are immutable after upload. New versions create version history while preserving previous versions.

7. Long-running operations (OCR, indexing, embeddings, AI report generation, notification delivery) must execute asynchronously.

8. Every AI-generated response must include references to the source documents used.

9. Every sensitive operation must be authenticated, authorized, and recorded in audit logs.

10. Business logic, AI services, notification services, and real-time communication must remain independent modules.

11. The platform must support both Arabic and French without changing the underlying business logic or stored data.

12. Every user interface must support localization, and Arabic pages must correctly support Right-to-Left (RTL) rendering.

13. All services must be containerized using Docker and deployable using Docker Compose while remaining cloud-ready for future deployment.