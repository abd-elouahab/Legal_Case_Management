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
| AI Framework | LangGraph | Multi-agent orchestration. Introduced by the RAG Pipeline (`services/rag_graph.py`), which declares its workflow as a `StateGraph` whose nodes are calls onto `RagService` — so the graph owns the *order* and nothing else, and a future branch (conversation memory, tool calling, a planner) is an edge rather than a redesign. Note that `langgraph-sdk` pins `websockets<16`, which downgrades that package from 17.x; uvicorn's WebSocket support works on both |
| LLM Provider | `LLMProvider` protocol (`services/llm.py`), **Google Gemini** (`gemini-2.5-flash`) by default | The only module in the platform that imports a model SDK. Two backends ship — `GeminiProvider` over `google-genai`, and `LiteLLMProvider` over the gateway `ai-workflow-rules.md` requires models to stay replaceable through. `litellm` is deliberately **not** in `requirements.txt`: it is imported lazily and its absence is reported as `llm_available: false`, exactly as a missing Tesseract is. Every SDK failure is translated into a `RagFailureCode` at this boundary, and retries with exponential backoff live here rather than in the orchestration |
| Prompt Templates | Jinja2 behind the `PromptLibrary` protocol (`services/prompts.py`) | Versioned `.j2` files in `apps/api/prompts/`, **not strings in Python**, so a prompt change is reviewable as a diff of the text actually sent to the model. Versioning is in the filename (`answer.v1.system.j2`), so two versions coexist and every answer records which produced it. Rendered with `StrictUndefined` — a prompt that silently lost its context block would produce ungrounded answers that look entirely normal — and with autoescaping **off**, because the output is plain text for a model rather than markup for a browser |
| Embeddings | BAAI bge-m3 | Generate document embeddings |
| OCR | Tesseract OCR + pytesseract + pdf2image (Poppler) + Pillow | Text extraction from PDFs, scanned PDFs, and images. OCRmyPDF was not needed: pdf2image renders pages and pytesseract reads them, which is the same pipeline with one fewer dependency. Behind the `OcrEngine` protocol, so it is replaceable |
| Chunking | LangChain `RecursiveCharacterTextSplitter` (`langchain-text-splitters`) | Splits extracted text into passages, paragraph-first, with Arabic sentence punctuation added to the separator list. Behind the `Chunker` protocol (`services/chunking.py`), so it is replaceable |
| Vector persistence | `qdrant-client` behind the `VectorStore` protocol (`services/vector_store.py`) | The only module that speaks Qdrant's data model **on the write side**. Exposes write, delete, and count — **and deliberately no query**, so retrieval cannot be smuggled in through it |
| Vector retrieval | `qdrant-client` behind the `VectorSearcher` protocol (`services/vector_search.py`) | The read side, introduced by Semantic Search as its own module rather than as a method on the store. Exposes one `search` call — **and deliberately no write**, so the two halves stay separable in both directions |
| Result ranking | `Ranker` protocol (`services/search_ranking.py`), `SimilarityRanker` today | Orders retrieved passages. Exists as a seam from the start so a future cross-encoder reranker is one class rather than a redesign |
| Background Jobs | Two bounded thread pools in the API process — `services/ocr_queue.py` (OCR) and the generic `services/job_queue.py` (indexing); Trigger.dev or Celery + Redis when a second consumer arrives | OCR and indexing today; notifications and scheduled tasks later. Separate pools, because the two stages fail differently and are sized differently. The job's identity, state, and concurrency control live in PostgreSQL rather than in the queue, so the runner is one file to replace |
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
- `modules/timeline` — Activity timeline and audit trail. Implemented inside
  `apps/api` (`models/timeline.py`, `core/timeline.py`,
  `repositories/timeline.py`, `services/timeline.py`,
  `services/timeline_access.py`, `api/v1/timeline/`) and `apps/web`
  (`components/timeline/`, embedded in `app/(protected)/cases/[id]`), following
  the same layering as Cases and Documents. It is **generic by construction**:
  business modules publish to it, and it knows nothing about what their events
  mean.
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
  layering as Cases and Users rather than introducing a separate deployable.
  **OCR** attaches to the Document entity as its own layer (`models/ocr.py`,
  `core/ocr.py`, `repositories/ocr.py`, `services/ocr.py`,
  `services/ocr_engine.py`, `services/ocr_queue.py`, `services/ocr_worker.py`,
  `services/ocr_access.py`, `api/v1/ocr/`, plus `apps/web/components/ocr/`).
  **Document indexing** attaches to it the same way and consumes the text OCR
  persists (`models/indexing.py`, `core/indexing.py`,
  `repositories/indexing.py`, `services/indexing.py`, `services/chunking.py`,
  `services/embedding.py`, `services/vector_store.py`,
  `services/job_queue.py`, `services/indexing_worker.py`,
  `services/indexing_access.py`, `api/v1/indexing/`, plus
  `apps/web/components/indexing/`). **Semantic search** is the read side of the
  same vectors and attaches as its own module — not to the Document entity, since
  a search spans documents (`core/search.py`, `repositories/search.py`,
  `services/search.py`, `services/vector_search.py`,
  `services/search_ranking.py`, `services/search_metrics.py`,
  `services/search_access.py`, `schemas/search.py`, `api/v1/search/`, plus
  `apps/web/components/search/` and `app/(protected)/search/`). RAG and the
  assistant are later features that consume its results.
- `services/ai` — Retrieval-Augmented Generation, prompts, and LLM integration.
  Implemented as flat modules inside `apps/api` rather than as a nested package,
  following the convention `services/embedding.py`, `services/chunking.py`, and
  `services/vector_store.py` already established: `core/rag.py`,
  `services/rag.py`, `services/rag_graph.py`, `services/rag_metrics.py`,
  `services/prompts.py`, `services/llm.py`, `schemas/rag.py`,
  `api/v1/rag/router.py`, plus the templates in `apps/api/prompts/`.
  **It has no repository and no model**, because a question changes nothing and
  is not an entity. Its retrieval collaborator is `SearchService` and nothing
  else — see the RAG Pipeline section below for why that single fact is the
  whole of its authorization story. The report agent and the
  compliance/translation/summary agents are later features that consume it.
- `modules/assistant` — The AI Legal Assistant: conversations, messages,
  streaming, follow-up suggestions, and response feedback. Implemented inside
  `apps/api` (`core/assistant.py`, `models/conversation.py`,
  `repositories/conversation.py`, `services/assistant.py`,
  `services/assistant_metrics.py`, `services/suggestions.py`,
  `schemas/conversation.py`, `api/v1/assistant/router.py`, plus
  `apps/api/prompts/assistant/`) and `apps/web` (`components/ai/`,
  `app/(protected)/ai/`, embedded in `app/(protected)/cases/[id]`), following
  the same layering as Cases, Documents, and Search. It is the **conversational
  surface over the RAG pipeline** and generates nothing itself: every answer
  comes from `RagService.answer` or `RagService.stream`, and the assistant holds
  no search service, no embedder, no vector searcher, and no prompt library for
  answering. It is the first stage of the AI pipeline to **persist what a user
  said** — everything below it is derived from a document.
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
  The OCR and indexing workers are implemented inside `apps/api`
  (`services/ocr_queue.py` + `services/ocr_worker.py`, and
  `services/job_queue.py` + `services/indexing_worker.py`, all started and
  drained by `core/lifespan.py`), following the same "no separate deployable
  until one is needed" reasoning as Cases, Documents, and Timeline. Both are
  bounded thread pools today; each sits behind a protocol, and the job's
  identity, state, and concurrency control live in PostgreSQL, so promoting
  either to a standalone Celery or Trigger.dev worker replaces one file rather
  than the feature. `services/job_queue.py` is the **generic** form — typed by
  the job it carries — introduced by indexing so the machinery is written once;
  `services/ocr_queue.py` predates it and is the candidate to fold into it.
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
- OCR Results (one per document version)
- OCR Pages (extracted text, one row per page)
- Document Indexes (one indexing run per document version — the run's state and
  what it produced; the chunks and vectors themselves live in Qdrant)
- Lawyer Assignments
- Clients
- Hearings
- Court Decisions
- Reports
- Notifications
- Timeline Events
- Audit Logs
- AI Conversations (one thread per user, with its counters and its last-message
  preview denormalized for the list row)
- AI Conversation Messages (one row per turn — the question verbatim, or the
  pipeline's answer with its citations, suggestions, and provenance as JSON)
- AI Message Feedback (one rating per assistant message, in its own table so
  that rating an answer cannot alter the transcript it is read from)
- Language Preferences

---

### Qdrant

Stores AI knowledge:

- Document embeddings
- Semantic search indexes
- Chunk metadata
- Retrieval information

Document chunks live in the `QDRANT_COLLECTION` collection (`document_chunks` by
default, created on the first indexing run at `EMBEDDING_DIMENSIONS` with
**cosine** distance, because the embedder returns unit-length vectors). Each
point's id is **derived** from `(document_id, document_version, page_number,
chunk_number)` as a UUID5 over a fixed namespace — never random — which is what
makes writing the same chunk twice an overwrite rather than a duplicate. Its
payload carries exactly what `10-document-indexing.md` lists (document, version,
case, page, chunk number, language, timestamps) plus the passage text and the
embedding model: the text so a future search result is readable without a second
round trip, the model because changing models requires re-indexing and a point
that does not say which model built it cannot be told apart from one that does
not need rebuilding. **Nothing is deleted except by version**: a rebuild removes
that version's points before writing its replacements, so a shorter rebuild
leaves no stale tail, while a replacement's index is built without destroying the
previous version's — which is still the right answer for anyone reading it.

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
  - `ocr:view` / `ocr:retry` / `ocr:monitor` follow the same shape. Reading a
    document's extracted text is scoped **per resource** by
    `services/ocr_access.py`, which delegates to `DocumentAccessPolicy`, which
    delegates to `CaseAccessPolicy` — so extracted text can never be more visible
    than the file it was read from. `ocr:retry` is narrower than `ocr:view`
    because a retry consumes real processing capacity: lawyers hold it, court
    representatives do not. `ocr:monitor` gates the platform-wide metrics view,
    which is administrative and deliberately not scoped to a case.
  - `indexing:view` / `indexing:reindex` / `indexing:monitor` extend the same
    shape one stage further. `services/indexing_access.py` owns no policy either;
    it delegates to `DocumentAccessPolicy`, so the chain is **index → document →
    case**, and an index can never be more visible than the extracted text it was
    built from. `indexing:reindex` is withheld from court representatives for the
    same reason as `ocr:retry`, only more strongly: a rebuild re-embeds every
    passage of the document, which is the most expensive operation the platform
    performs.
  - `search:query` / `search:monitor` extend the chain one stage further again,
    to **passage → document → case**. `services/search_access.py` owns no policy
    of its own either. The one thing that is genuinely different is *where* the
    scope is applied: every other module pushes it into the SQL query it was
    already running, and search cannot — its rows live in Qdrant, which cannot
    join against `cases`. So the scope crosses the boundary as a **set of case
    identifiers** (`SearchRepository.accessible_case_ids`) and becomes one more
    `must` condition on the vector query, which is exactly what Document Indexing
    put `case_id` in every payload for. Three properties make that safe and each
    is asserted rather than assumed: the scope is computed from the caller alone
    and can never be supplied by the request; it is **ANDed** with every user
    filter, so no combination of filters can widen it; and "assigned to no cases"
    is an **empty set that matches nothing**, never an absent filter that matches
    everything. `search:query` is granted to court representatives, unlike
    `ocr:retry` and `indexing:reindex`: those two *operate the pipeline*, while
    this one **reads**, and it reads strictly less than the `ocr:view` they
    already hold.
  - `ai:ask` / `ai:monitor` are the RAG pipeline's, and they are the first pair
    in this chain that adds **no per-resource policy module of its own**. There
    is no `rag_access.py`, deliberately: the pipeline retrieves only through
    `SearchService`, which already applies `search_access.py` → document → case,
    so a second policy here would be a second rule to keep in step with the
    first. `ai:ask` is **withheld from court representatives**, unlike
    `search:query` — and the difference is the one place this platform draws a
    line between reading and generating. Search returns the platform's own text
    verbatim; the pipeline returns a *generated interpretation* of a case file,
    produced on the platform's behalf. `project-overview.md` and this document
    give court representatives no AI capabilities, and `ai:chat` and
    `ai:generate-report` have been withheld from them since Authorization
    shipped; granting the pipeline underneath both would be the same access by
    another route. `ai:monitor` gates the platform-wide pipeline metrics, which
    are administrative and deliberately not case-scoped.
  - **The AI Legal Assistant adds no permission at all**, and it is the first
    feature in this chain not to. `ai:chat` was defined when Authorization
    shipped and is exactly this surface; `ai:ask` is the pipeline underneath it;
    `ai:monitor` is the operational view. **Sending a message requires both
    `ai:chat` and `ai:ask`**, because a message does both — a deployment that
    grants one and withholds the other must not reach the pipeline through this
    door — while *reading* a transcript requires only `ai:chat`, since it asks
    nothing new of the pipeline. There is also **no `assistant_access.py`**: the
    other modules need one because "may this caller reach this row" is a
    question about case assignments that several services ask, and here it is a
    single equality that every query in `repositories/conversation.py` asserts
    in its own `WHERE` clause. A conversation the caller does not own is
    **404, not 403** — the one place on the platform that conceals rather than
    refuses, because confirming that another user's private thread exists is
    itself the disclosure the spec forbids.
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

### OCR Processing

Implemented per `context/feature-specs/09-ocr-processing.md`. The first stage of
the AI pipeline: it ends at **persisted text**, and deliberately contains nothing
about embeddings, vectors, retrieval, or an LLM.

- **A run belongs to a document *version*, not to a document.** `ocr_results` is
  keyed `(document_id, document_version)` by a unique constraint, which is the
  whole of the spec's idempotency requirement: retrying re-uses the row, and a
  replacement gets its own run while the previous version keeps the text that was
  read from *it*.
- **Concurrency is a conditional `UPDATE`, not a lock.**
  `OcrRepository.claim` moves a run `pending → processing` with
  `WHERE status = 'pending'`, so exactly one worker updates a row and any other
  updates none. No Redis key, nothing to expire or leak — the row *is* the lock,
  held for exactly as long as its state says.
- **The extracted text is one row per page** (`ocr_pages`), not one blob. Page
  order and page boundaries are what a lawyer cites and what a later chunker will
  split on; a separator inside a single string is a convention every future reader
  would have to know. `OcrTextRead.full_text` joins them with U+000C FORM FEED and
  publishes the separator, so the convenience shape loses no boundary.
- **Recognition accuracy is the engine's, and the seam is the lever.** Measured
  against the live engine on a realistic Arabic filing: **94 % line recall, 100 %
  precision** — every line returned is character-exact at 91–93 % confidence.
  The residual loss is Tesseract's layout analysis discarding text on *sparse*
  pages (a single line alone on a blank page reads as nothing; the same line with
  neighbours reads perfectly), and it is invariant across language sets, page
  segmentation modes, spacing, fonts, and 150–600 DPI. No configuration improves
  it, so none was added. Raising it means changing engines, which is one class
  behind `OcrEngine`.
- **Two seams keep the feature replaceable.** `services/ocr_engine.py` is the only
  module that imports Tesseract, pytesseract, pdf2image, or Pillow, and every
  library failure is translated into an `OcrFailureCode` at that boundary;
  `services/ocr_queue.py` is the only module that knows how a job is scheduled.
  The job's *identity*, *state*, and *concurrency control* are all in PostgreSQL,
  so replacing the in-process thread pool with Celery or Trigger.dev replaces one
  file.
- **The upload never waits.** `DocumentService` publishes to an `OcrScheduler`
  after its commit; scheduling returns immediately and swallows its own failures,
  because the file is stored and the response is already earned.
- **A failure is a recorded state, not a failed request.** Every way extraction
  can go wrong becomes a `failed` run with a machine-readable `error_code`; the
  uploaded file, its metadata, and its version history are untouched, and the run
  stays retryable. The service writes only to `ocr_results` and `ocr_pages`, so
  that guarantee is structural rather than a matter of care.
- **Extracted text inherits document permissions**, which inherit case
  permissions — `services/ocr_access.py` owns no policy of its own, exactly as
  `document_access.py` and `timeline_access.py` do not.
- **The text never reaches a log.** A filename appears in a timeline description
  and nowhere in the application log; the extracted text appears in neither. The
  logs carry identifiers, statuses, page counts, and character counts only.
- Extraction requires two **system** binaries that pip does not install —
  **Tesseract** (the recogniser, with a language pack per language in
  `OCR_LANGUAGES`) and **Poppler** (`pdfinfo`/`pdftoppm`, which render PDF pages).
  They belong in the API's container image. Point `TESSERACT_CMD` and
  `POPPLER_PATH` at them when they are not on `PATH`, which is the default on
  Windows. Their absence is handled rather than fatal: the run fails with
  `engine_failure`, `GET /ocr/metrics` reports `engine_available: false`, the
  uploaded document is untouched, and every run can be retried once they are
  installed.

### Document Indexing

Implemented per `context/feature-specs/10-document-indexing.md`. The second stage
of the AI pipeline: it begins where OCR ends and **ends at persisted vectors**,
deliberately containing nothing about search, ranking, retrieval, RAG, or an LLM.

- **An index belongs to a document *version*, not to a document.**
  `document_indexes` is keyed `(document_id, document_version)` by a unique
  constraint, which is the whole of the spec's idempotency requirement: a rebuild
  re-uses the row, and a replacement gets its own index while the previous
  version keeps the one built from *its* text.
- **One table, not two.** OCR needed a run table and a text table because the
  text has nowhere else to live. Indexing needs one: the chunks and their
  embeddings live in Qdrant, and duplicating them into PostgreSQL would create
  two stores that can disagree about what is indexed. What PostgreSQL keeps is
  the *run* — the part a lawyer polls, an operator monitors, and a rebuild
  re-uses.
- **Concurrency is a conditional `UPDATE`, not a lock**, exactly as OCR's is:
  `IndexingRepository.claim` moves a run `pending → indexing` with
  `WHERE status = 'pending'`, so exactly one worker updates a row.
- **Re-indexing is idempotent through two mechanisms that cover different
  halves.** A point's id is *derived* from its position in the document, so
  writing the same chunk twice is an overwrite — that is "avoid duplicate
  vectors". And the version's previous points are deleted before the new ones are
  written, so a rebuild producing *fewer* chunks leaves no tail behind — that is
  "replace outdated vectors". Either alone is insufficient.
- **Three seams keep the feature replaceable**, in the shape `OcrEngine`
  established: `services/chunking.py` is the only module that imports a text
  splitter, `services/embedding.py` the only one that imports
  `sentence_transformers` or touches a model, and `services/vector_store.py` the
  only one that speaks Qdrant's data model. Every library failure is translated
  into an `IndexFailureCode` at its boundary, so the service records a *cause*
  without knowing what a `ValidationError` is — and so a library message, which
  can echo the text it was processing, never leaves the module.
- **Chunking preserves the page.** Pages are split one at a time rather than
  concatenated, because a chunk straddling two pages has no honest answer to
  "which page is this?" — and the page is what a future citation points at. A
  page that yields nothing contributes no chunk and does not renumber the pages
  after it, because the page number travels *on* the chunk.
- **The embedding model is loaded lazily, once, per process.** bge-m3 is roughly
  2 GB: loading it at import would make startup depend on a model download, and
  loading it per document would make indexing unusable. A deployment without it
  still starts and reports `embedding_available: false`.
- **A failure is a recorded state, not a failed request.** Every way indexing can
  go wrong becomes a `failed` run with a machine-readable `error_code`; the
  extracted text, the document, its metadata, and its version history are
  untouched, and the run stays retryable. The service writes to
  `document_indexes` only, so that guarantee is structural rather than a matter
  of care. Vectors already written by a failed attempt are deliberately **kept**:
  they are correct passages under derived ids, so a rebuild overwrites them, and
  a partial index is more useful than none while the failure is investigated.
- **Indexing inherits document permissions, which inherit case permissions** —
  `services/indexing_access.py` owns no policy of its own, exactly as
  `ocr_access.py`, `document_access.py`, and `timeline_access.py` do not. Every
  vector's payload carries its `case_id` and `document_id`, which is the metadata
  a future search will need to translate that same scope into a Qdrant filter.
- **No passage ever reaches a log.** A filename appears in a timeline description
  and nowhere in the application log; a chunk's text appears in neither. The logs
  carry identifiers, statuses, chunk counts, and character counts only.

### Semantic Search

Implemented per `context/feature-specs/11-semantic-search.md`. The third stage of
the AI pipeline and the RAG pipeline's retrieval half: it begins at the vectors
indexing wrote and **ends at retrieved passages**, deliberately containing
nothing about answer generation, summarization, prompts, conversations, or an
LLM.

- **It is a new module, not a method on the write side.** `10-document-indexing.md`
  made "indexing does not retrieve" structural by giving `VectorStore` no query
  method; honouring that means retrieval arrives as its own read-side protocol,
  `VectorSearcher` (`services/vector_search.py`), which in turn has **no write
  method**. The two halves say, in the type system, that indexing writes and
  search reads, and neither can do the other's job by accident.
- **The same embedding model embeds documents and queries**, because
  `ai-architecture.md` requires it — enforced by calling the same
  `services/embedding.py` module and injecting the same dependency, rather than
  by remembering to configure two settings identically.
- **No entity and no migration.** Search is a read that answers in milliseconds
  and has no lifecycle to poll, so unlike OCR and indexing it persists nothing.
  Its metrics accumulate **in the process** behind a `SearchMetricsRecorder`
  protocol (`services/search_metrics.py`); the limits of that are stated rather
  than hidden — counters reset on restart and each instance counts only its own
  traffic, which the endpoint reports as `since`. A Redis-backed recorder is one
  class plus one line in `api/deps.py`.
- **Every filter executes in the database, never in Python.** Filtering after
  retrieval would return short pages, leak match counts, and pull unauthorized
  text into the process. Category and file type are the exception that proves the
  rule: the vector payload carries neither (indexing stores what a *chunk* is,
  not what its document is), so they are resolved to a bounded set of document
  ids in PostgreSQL and pushed into the vector query as one condition — and a set
  that overflows `SEARCH_MAX_FILTER_DOCUMENTS` is **refused**, because silently
  truncating it would drop matching documents with nothing to indicate it.
- **`documents.deleted_at` is honoured at read time.** Deletion is logical, so a
  withdrawn document's vectors outlive it; the service drops results whose
  document no longer resolves. That is the point at which a search result stops
  being more visible than the document it came from.
- **A search that matches nothing is a success**, not a 404 and not a failure
  metric: the corpus holds nothing near the query, which is an answer. Only a
  dependency outage is a failure, and it answers **503 naming which dependency**
  — a missing embedding model and an unreachable Qdrant need different responses.
- **Search is a `POST`, and that is a privacy decision.** A query string is
  written to the reverse proxy's access log, the browser's history, and the
  `Referer` header of anything the page loads next — three logs the application
  does not control — and a legal query is at least as revealing as the passage it
  finds. It is still a read: nothing is created and it answers 200.
- **No query text reaches a log.** Every search is logged, correlated by a
  **salted, non-reversible fingerprint** of the query (`core/search.py`); the
  text itself appears only when a deployment sets `SEARCH_LOG_QUERIES`, which is
  the spec's "unless existing project logging policies explicitly allow it"
  clause made into a switch an operator sets. Filters are logged as a shape
  ("filtered: true"), never as values — a list of case identifiers is a list of
  the caller's matters.
- **Ranking is a seam from day one.** `SimilarityRanker` orders by the score
  Qdrant computed and breaks ties by position in the document, which is what makes
  the same query return the same page — Qdrant does not guarantee an order between
  equal scores. A future cross-encoder reranker is one class plus one setting.

### RAG Pipeline

Implemented per `context/feature-specs/12-rag-pipeline.md`. The fourth stage of
the AI pipeline: it begins at the passages Semantic Search returns and **ends at
a grounded answer with citations**. It is deliberately **not the chat
interface** — no conversation, no history, no persistent memory, no streaming,
and no UI — and not report generation; it is the reusable backend service the AI
Legal Assistant and the AI Report Agent will both consume.

- **Retrieval goes through `SearchService.search` and nowhere else, and that one
  fact is the whole of this feature's authorization story.** The spec forbids
  querying the vector database directly when a retrieval abstraction exists, and
  the boundary is **structural** rather than a matter of discipline:
  `RagService` holds no vector searcher, no embedder, no repository, and no
  database session, so there is no path from the pipeline to a passage that does
  not pass through the service that scopes it to the caller's cases. Everything
  the spec's "Authorization" section requires — the case scope inside the vector
  query, a filter naming an unreachable case refused with 403 rather than
  emptied, an unassigned caller retrieving nothing — is inherited rather than
  re-implemented. There is therefore **no `rag_access.py`**.
- **The workflow is a LangGraph `StateGraph`, and the graph owns only the
  order.** `services/rag_graph.py` declares seven nodes — validate, retrieve,
  assemble, generate, verify, format, and no-evidence — each of which is a call
  onto `RagService`. The two halves are independently testable: the graph with a
  recorder that only writes down what ran, the nodes with no graph at all.
- **The branch after retrieval is real, not decorative.** Nothing retrieved goes
  straight to the no-evidence node, **skipping the model entirely**. That is
  simultaneously "do not fabricate answers" (there is no model output to
  fabricate from), "avoid duplicate LLM calls" taken to its limit (the cheapest
  call is the one not made), and the proof that the branching the spec asks to be
  possible actually is.
- **No evidence is answered by the platform, not by the model.** The "could not
  find supporting information" sentence is written once per language in
  `core/rag.py`. Asking a model to explain that it found nothing is the tempting
  alternative and the wrong one: handed an empty context and a legal question, a
  model will sometimes explain the emptiness *and then answer anyway* from its
  training data, which is indistinguishable from a grounded answer downstream.
- **The model's own refusal is a typed outcome.** The prompt instructs it to
  reply with the exact token `INSUFFICIENT_EVIDENCE` when the passages do not
  support an answer; the pipeline recognises that and returns
  `insufficient_evidence: true` with the platform's sentence. A sentinel rather
  than a phrase, because a phrase would have to be matched in three languages and
  a paraphrase would read as a confident answer.
- **Prompts are files, versioned in their filenames.** `apps/api/prompts/rag/
  answer.v1.{system,user}.j2`, rendered by `services/prompts.py` and pinned by
  `RAG_PROMPT_TEMPLATE` / `RAG_PROMPT_VERSION`. Every answer records which
  template and version produced it, because configuration is *current* and an
  answer is *historical* — an evaluation run (Ragas, DeepEval) cannot compare two
  prompts otherwise.
- **Untrusted text is delimited, not escaped.** The question and the retrieved
  passages are fenced inside `CONTEXT`/`QUESTION` markers, and the system prompt
  states that everything inside them is data to be read rather than instructions
  to be followed. A prompt-level control, because no character-escaping scheme
  makes a sentence stop being a sentence.
- **The context budget is in characters and is enforced before the provider is
  called.** Same reasoning as `INDEX_CHUNK_SIZE`: counting tokens needs the
  provider's tokenizer, which is the coupling the provider abstraction exists to
  prevent. Enforcing it afterwards means discovering the overflow *after* the
  request was sent, billed, and waited for — or, on some providers, having it
  silently truncated with nothing to say which passages were lost. Passages are
  consumed in relevance order; the one that would overflow is clipped if a
  readable remainder is left and dropped otherwise, and once one is dropped the
  rest are too, because skipping ahead to a shorter passage would reorder the
  evidence by length.
- **Citations carry the four references the spec names** — document, version,
  page, case — plus the excerpt *as it was placed in the prompt*, which is the
  honest evidence for an answer. The chunk number, the point id, the embedding
  model, and the vector are all available at that layer and are all withheld.
  **Every source is returned, whether the model cited it or not**, each flagged
  `referenced`: a model that forgot a marker has not made the evidence disappear,
  and the flag keeps the list complete and honest at once. A marker pointing at
  no source is **removed from the prose** and counted — a dangling reference in a
  legal answer invites a reader to look for a source that does not exist.
- **The answer language is settled once, and detected `en` becomes French.**
  `detect_language` tells French from English by diacritics, and *"Quand le loyer
  est-il payable ?"* is impeccable French containing none — so on a short
  question the heuristic cannot do better than a coin flip. Since
  `project-overview.md` names Arabic and French as the interface and
  AI-interaction languages, French is the right fallback; an explicit `language`
  on the request always wins, and is what the localized frontend will send.
- **A failure is a 503 naming its cause**, never a 500: `retrieval_unavailable`,
  `llm_unavailable`, `timeout`, `llm_failure`, `malformed_response`,
  `context_overflow`, `unknown`. A rejected *request* (an unanswerable question,
  an inaccessible filter) keeps its own 4xx and is deliberately **not** counted
  as a pipeline failure — the failure rate is a health signal, and a badly-formed
  question says nothing about the pipeline's health. There is deliberately **no
  failure code for "no supporting evidence"**.
- **The whole-run deadline is checked between stages, not inside them**, for the
  reason indexing records: neither the search service nor a provider SDK accepts
  a deadline that can be moved mid-call, so the honest guarantee is "no new stage
  begins after the deadline". The provider is given the *smaller* of its own
  timeout and what remains of the run's.
- **Retries live in the provider, with exponential backoff**, and only transient
  failures are retried: a rejected credential retried three times is three
  refusals, slower and billed.
- **No question, passage, or answer reaches a log.** Every run is logged and
  correlated by the *same* salted fingerprint a search for that text produces, so
  an operator can trace a failing question across both surfaces while learning
  nothing about the matter. `RAG_LOG_QUESTIONS` adds the question beside the
  fingerprint; there is deliberately **no switch that logs the answer**.
- **No entity and no migration.** A question is not a persisted run with a
  lifecycle anyone polls, and a row per question would be write amplification
  derived from the user's question. Metrics accumulate in the process behind a
  `RagMetricsRecorder`, exactly as search's do; conversations *are* persisted, by
  the AI Assistant, which is a different feature.
- **Two endpoints only** (`POST /api/v1/rag/answer`, `GET /api/v1/rag/metrics`),
  and a test asserts there is no third: conversations, streaming, follow-up
  suggestions, and feedback are the assistant's.
- **Streaming lives here, not in the assistant.** `RagService.stream` is the
  same nodes in the same order with generation replaced by an incremental call,
  and it exists at this layer because the alternative — an assistant that
  streamed on its own — would have to retrieve, build a prompt, call a provider,
  verify the reply, and attach citations, which is this whole module written
  twice. It is not a LangGraph traversal, because `invoke` returns a *final
  state* and emitting fragments out of the middle of a node needs a generator;
  the two are kept in step by a test that asserts they visit the same nodes and
  take the same branch. A streamed answer carries **no token usage**, because a
  provider reports usage on a finished response and there is not one.

### AI Legal Assistant

Implemented per `context/feature-specs/13-ai-legal-assistant.md`. The fifth stage
of the AI pipeline and the conversational surface over the fourth: it begins at a
message a user typed and **ends at a persisted turn** — the question, the
pipeline's answer, its citations, and the questions worth asking next.

- **Every answer is the pipeline's, and that is structural rather than
  disciplinary.** `AssistantService` holds a `RagService` and nothing else that
  could produce one: no search service, no embedder, no vector searcher, no
  prompt library, and no document repository. So the spec's *"must not duplicate
  retrieval, prompt construction, or orchestration logic already implemented by
  the RAG Pipeline"* is inherited rather than promised, and the authorization
  chain — conversation → pipeline → search → document → case — holds by the shape
  of the dependency graph.
- **Three tables, and each earns its own.** `conversations` is the thread,
  `conversation_messages` is one row per turn, and `message_feedback` is a rating
  of one answer. The third is separate specifically so that *"feedback should not
  modify conversation history"* is structural: rating writes to a table the
  transcript is not read from, so it cannot alter one even by accident.
- **Ownership is the shape of every query, not a policy module.** Every read in
  `repositories/conversation.py` takes an `owner_id` and puts it in the `WHERE`
  clause; there is deliberately no method that resolves a conversation by
  identifier alone, so no call site can forget to scope one.
- **Deletion is logical, and it is the transcript that justifies it.** A
  conversation carries the citations of advice a lawyer may have acted on, so
  `DELETE` sets `deleted_at`; the row is excluded from every read and a future
  retention job reclaims it. Archiving is the reversible half — out of the
  working list, closed to new messages, still readable.
- **A follow-up is resolved against what came before it, deterministically.**
  `core/assistant.py` prefixes a short question with a labelled reference to the
  earlier question, bounded by both a turn count and a character budget. Prefixing
  rather than concatenating is forced by the pipeline: its `question` is *both*
  the retrieval query and the text the model is asked to answer, so raw history
  would make the model answer the previous question again. Only earlier **user
  questions** travel — an answer is a paragraph and would dominate both. The
  limits are stated rather than hidden: it broadens rather than rewrites, which is
  the safe direction, and a model-based rewriter substitutes for one function.
- **The title comes from the user's first question, never from a model.** A
  hallucinated title is the one hallucination nobody would ever check, it would
  double the model calls the first message of every conversation costs, and the
  user's own words are by construction the most faithful description. It is
  editable, and a title someone chose is never overwritten.
- **Follow-up suggestions are a second, versioned prompt** (`assistant/followups`)
  through the *same* prompt library and the same provider — a new prompt for a
  purpose the pipeline does not serve, not a duplicate of one it does. They are
  never produced for an ungrounded answer (there is nothing to ground a follow-up
  in, and no call is made), and **every failure returns an empty list**: an answer
  the user is already waiting for must not be lost to the convenience after it.
  A reply the provider reports as **truncated loses its last line**, because a
  cut-off reply ends mid-line and its final entry is half a question — short,
  unique, and indistinguishable from a real one by every length rule.
- **An output ceiling has to cover a reasoning model's thinking, not just its
  answer.** `gemini-2.5-flash` charges its internal deliberation against
  `max_output_tokens`, so `ASSISTANT_SUGGESTION_MAX_OUTPUT_TOKENS` is sized for
  the model rather than for three short questions — 256 left nine visible tokens
  and produced a suggestion cut off mid-word. Found by a live run; unreachable
  from a hermetic one, where the double returns whatever string the test wrote.
- **Streaming is Server-Sent Events, and the stream is primed before the status
  line is sent.** The route pulls the first event — emitted once retrieval has
  run — so every request rejection (403, 404, 409, 422, 503) keeps its own HTTP
  status instead of being smuggled into an event. The refusal sentinel is
  **withheld while the accumulated text could still be it**, so a reader never
  sees an internal token flash by; the `final` event is authoritative, because a
  dangling citation marker has been removed from it.
- **A streamed exchange persists the question before the answer exists**, unlike
  the blocking path, which writes both in one transaction. A browser that closes
  mid-stream would otherwise lose a question it had already sent and seen echoed.
- **`ASSISTANT_STREAMING_ENABLED=false` changes what the server does**, not what
  a client is asked to do: the streaming endpoint is served from the blocking
  pipeline and emits the same three-event sequence, so a client needs no branch
  for it. An operator turning streaming off because a proxy buffers responses
  needs the API to actually stop streaming.
- **Metrics come from two places on purpose.** Conversation counts, conversation
  length, and feedback statistics are **queried** — they are properties of
  persisted rows, and counting them in a process would reset on restart *and* be
  wrong. Request counts, latency, and failures accumulate **in the process**
  behind `AssistantMetricsRecorder`, exactly as search's and RAG's do, with
  `since` reporting the window.
- **No question, answer, title, or citation reaches a log**, and the logs carry
  the same salted fingerprint a search or a pipeline run for that text produces.

### Timeline & Audit Trail

Implemented per `context/feature-specs/08-timeline.md`:

- **The timeline module holds no business logic.** It records what it is told.
  The services that own a rule — `services/case.py`, `services/document.py` —
  publish to `TimelineService.record(...)` *after* their change is committed;
  the timeline owns storage, presentation, and authorization of those events and
  nothing else. `TimelineRecorder` is the narrow protocol a publisher depends on,
  so a publishing module cannot reach the read or authorization side.
- **Append-only.** `timeline_events` has no `updated_at` and no soft-delete
  column, and `TimelineRepository` exposes no update and no delete — a repository
  that cannot express "change this event" cannot be talked into it. The API is
  read-only for the same reason: an audit trail a client can edit is not one.
- **The actor is snapshotted, not joined.** `actor_name` and `actor_role` are
  copied onto the row when the event happens. A join would render the actor as
  they are *today*, so renaming a user would silently rewrite history.
  `actor_id` is kept alongside for correlation.
- **`event_type` and `actor_role` are `VARCHAR`, not database enums** — the only
  place on the platform that departs from `case_status` / `document_category` /
  `user_role`. The spec requires that future modules publish without modifying
  the timeline, and an `ALTER TYPE` per new event type is exactly that
  modification; and a *snapshot* column must tolerate its vocabulary moving on.
  `TimelineEventType` (`core`/`models`) remains the central registry publishers
  use, and every read path is tolerant of an identifier it does not recognise.
- **`metadata` is JSONB**, `NOT NULL DEFAULT '{}'`, normalised by
  `core/timeline.py` to JSON-safe values and capped at 8 KB. It is the extension
  point: a later module attaches its own specifics with no schema change.
- **A failure to record never fails the operation that caused it.** The business
  change is already committed by then, so raising would answer a successful
  request with a 500 and invite a duplicating retry. The failure is logged; the
  structured application log for the underlying operation is emitted
  independently, so the operational record survives.
- **`created_at` is stamped by the service, monotonically per instance.** One
  request can publish several events, and the timeline is ordered by this column
  — but PostgreSQL's `now()` is the transaction's start time and the platform
  clock is only so fine-grained, so ties would order history arbitrarily against
  a random-UUID tiebreaker. A service instance is per request, which is exactly
  the scope where ordering is guaranteed and needed.
- **Access follows the case, exactly.** `services/timeline_access.py` owns no
  policy; it delegates to `CaseAccessPolicy`, as `document_access.py` does. A
  caller not party to a case is refused **403**, never handed an empty page — an
  empty timeline and an inaccessible one must not be confused.
- **The timeline is not the application log, and neither is derived from the
  other.** Timeline events are business facts a lawyer reads; the structured logs
  are for an operator. That is why a **filename appears in a timeline
  description and never in a log line**: the timeline is served only to users
  already entitled to the case.

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