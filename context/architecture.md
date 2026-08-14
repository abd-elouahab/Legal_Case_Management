# Architecture Context

## Stack

| Layer | Technology | Role |
| ------ | ---------- | ---- |
| Frontend | Next.js + TypeScript | Administrator, Lawyer, and Court web interfaces |
| UI | Tailwind CSS + shadcn/ui | Responsive and reusable user interface |
| State Management | TanStack Query | API communication and frontend caching |
| Internationalization | **next-intl** in the browser, `core/localization.py` on the server | English, French, and Arabic with RTL. The two halves answer different questions and are deliberately not one library: the API decides **which language a person is addressed in** (`resolve_language`, one candidate list, used by the notification renderer, both delivery channels, and both AI surfaces), and next-intl decides **what the interface says in it**. The catalogues are static JSON under `apps/web/messages/`, imported dynamically one locale at a time — deliberately **not** served by the API, because a page load would then wait on a database-backed process for text that changes when a release does, and the login screen's own copy would sit behind a login. There is no locale in the URL and no routing middleware: the language is a *setting*, so the shell resolves it after sign-in and writes `lang`/`dir` onto the document element |
| Forms | React Hook Form + Zod | Form handling and validation |
| Backend | FastAPI | REST APIs, business logic, authentication, AI orchestration, and real-time services |
| Real-Time Communication | FastAPI WebSockets behind a central **event dispatcher** (`services/events.py`) | Synchronize case updates instantly between users. Business modules publish typed **domain events** (`core/events.py`) to the dispatcher and know nothing about who consumes them; `websocket/manager.py` is its one subscriber today and routes each event to the connections authorized for its topic. The dispatcher is **in-process**: an event reaches only the clients connected to *this* API instance, which is the whole platform at one instance and is why `EventSubscriber` is a protocol — a `RedisEventBridge` is one class plus one line in `api/deps.py`, with no business module, connection, or client changing |
| ORM | SQLAlchemy + Alembic | Database interaction and migrations |
| Database | PostgreSQL | Store users, legal cases, lawyers, court information, reports, notifications, audit logs, and metadata |
| Vector Database | Qdrant | Semantic search and Retrieval-Augmented Generation (RAG) |
| Object Storage | MinIO | Store legal documents, generated reports, and future voice recordings |
| Cache & Messaging | Redis | Cache, background queues, and temporary data. **WebSocket Pub/Sub is not yet on Redis**: Real-Time Synchronization ships the in-process dispatcher described above, and the row is honest about that rather than describing an intent. Redis is what a multi-instance deployment adds behind the same protocol |
| AI Framework | LangGraph | Multi-agent orchestration. Introduced by the RAG Pipeline (`services/rag_graph.py`), which declares its workflow as a `StateGraph` whose nodes are calls onto `RagService` — so the graph owns the *order* and nothing else, and a future branch (conversation memory, tool calling, a planner) is an edge rather than a redesign. Note that `langgraph-sdk` pins `websockets<16`, which downgrades that package from 17.x; uvicorn's WebSocket support works on both |
| LLM Provider | `LLMProvider` protocol (`services/llm.py`), **Google Gemini** (`gemini-2.5-flash`) by default | The only module in the platform that imports a model SDK. Two backends ship — `GeminiProvider` over `google-genai`, and `LiteLLMProvider` over the gateway `ai-workflow-rules.md` requires models to stay replaceable through. `litellm` is deliberately **not** in `requirements.txt`: it is imported lazily and its absence is reported as `llm_available: false`, exactly as a missing Tesseract is. Every SDK failure is translated into a `RagFailureCode` at this boundary, and retries with exponential backoff live here rather than in the orchestration |
| Prompt Templates | Jinja2 behind the `PromptLibrary` protocol (`services/prompts.py`) | Versioned `.j2` files in `apps/api/prompts/`, **not strings in Python**, so a prompt change is reviewable as a diff of the text actually sent to the model. Versioning is in the filename (`answer.v1.system.j2`), so two versions coexist and every answer records which produced it. Rendered with `StrictUndefined` — a prompt that silently lost its context block would produce ungrounded answers that look entirely normal — and with autoescaping **off**, because the output is plain text for a model rather than markup for a browser |
| Embeddings | BAAI bge-m3 | Generate document embeddings |
| OCR | Tesseract OCR + pytesseract + pdf2image (Poppler) + Pillow | Text extraction from PDFs, scanned PDFs, and images. OCRmyPDF was not needed: pdf2image renders pages and pytesseract reads them, which is the same pipeline with one fewer dependency. Behind the `OcrEngine` protocol, so it is replaceable |
| Chunking | LangChain `RecursiveCharacterTextSplitter` (`langchain-text-splitters`) | Splits extracted text into passages, paragraph-first, with Arabic sentence punctuation added to the separator list. Behind the `Chunker` protocol (`services/chunking.py`), so it is replaceable |
| Vector persistence | `qdrant-client` behind the `VectorStore` protocol (`services/vector_store.py`) | The only module that speaks Qdrant's data model **on the write side**. Exposes write, delete, and count — **and deliberately no query**, so retrieval cannot be smuggled in through it |
| Vector retrieval | `qdrant-client` behind the `VectorSearcher` protocol (`services/vector_search.py`) | The read side, introduced by Semantic Search as its own module rather than as a method on the store. Exposes one `search` call — **and deliberately no write**, so the two halves stay separable in both directions |
| Result ranking | `Ranker` protocol (`services/search_ranking.py`), `SimilarityRanker` today | Orders retrieved passages. Exists as a seam from the start so a future cross-encoder reranker is one class rather than a redesign |
| Background Jobs | Bounded thread pools in the API process — `services/ocr_queue.py` (OCR), the generic `services/job_queue.py` (indexing, reports, email, WhatsApp), and `services/notification_events.py`'s own worker; plus **two timer threads**, `EmailRetrySweeper` and `WhatsAppRetrySweeper`; Trigger.dev or Celery + Redis when a second process arrives | OCR, indexing, report generation, notification creation, and the two outbound delivery channels. Separate pools, because the stages fail differently and are sized differently — and the two delivery channels are separate from *each other* for a reason worth stating: the Cloud API rate-limits per business phone number while a relay greylists per sender, so sharing would make a throttled WhatsApp number slow down password-reset mail and a greylisting relay occupy the threads that carry hearing updates. For OCR, indexing, and reports the job's identity, state, and concurrency control live in PostgreSQL rather than in the queue, so the runner is one file to replace. **Notifications is deliberately the exception**: its queue carries a *domain event*, which is not a persisted job and has nothing to resume from — so its worker drains on shutdown rather than re-queueing at startup, and a burst past `NOTIFICATION_QUEUE_SIZE` is dropped and counted rather than blocking the publisher that produced it. **Email is the first to need a *schedule* rather than only a queue**: a transient failure writes a `next_attempt_at` onto the delivery row and returns the worker thread, and `EmailRetrySweeper` — a timer thread that also runs once at startup — re-queues what has come due and reclaims anything a dead process left mid-send. A worker that slept out an hour-long backoff would hold one of two threads for an hour and lose the schedule on restart; a column survives both. It is deliberately not a general scheduler, and the reminder scheduling `16-notifications.md` left out of scope is the feature that should bring one — there are now **two** of these sweepers to replace rather than one, which is itself part of that argument |
| Email Service | `EmailProvider` protocol (`services/email_provider.py`), **SMTP** by default; Mailpit for local development | Email delivery of selected notifications. The only module in the platform that imports `smtplib` — and it adds **no dependency**, because the standard library speaks the protocol every relay does, which is the same outcome the report exporter reached for Markdown. Two backends ship: `SmtpEmailProvider` and a `NullEmailProvider` that accepts and discards for staging and tests. Every library failure is translated into an `EmailFailureCode` at this boundary, and the provider's own message never escapes it — an SMTP rejection quotes the envelope, so what leaves is a code and never an address. Resend, SendGrid, SES, and Mailgun are one class plus one registry entry each, with nothing above the boundary changing. **Off by default** (`EMAIL_ENABLED=false`), one of the two feature switches on the platform that are — the other being WhatsApp, and for the same reason: these are the platform's outward-facing side effects |
| Email Templates | Jinja2 behind the `EmailTemplateRenderer` protocol (`services/email_templates.py`) | Versioned `.j2` files in `apps/api/emails/`, three parts per version (subject, HTML, plain text), so a wording change is reviewable as a diff of the message that was delivered. **Two Jinja environments, and that is the difference from `services/prompts.py`**: autoescaping is **on** for the HTML part, because an administrator's announcement reaches `{message}` and that part is markup rendered in a mail client, and **off** for the subject and the plain-text part, because escaping there would put `&#39;` into somebody's screen reader. `StrictUndefined` in both, so a template that lost its `action_url` fails loudly rather than sending a correct-looking email nobody can act on |
| WhatsApp Integration | `WhatsAppProvider` protocol (`services/whatsapp_provider.py`), **Meta WhatsApp Cloud API** by default | WhatsApp delivery of selected notifications, and the third channel the same notifications travel on. The only module that knows Meta's URL shape, its JSON, or its error codes — and it adds **no dependency**, because the Cloud API is one JSON `POST` with a bearer token and `urllib.request` sends it, which is the same outcome the SMTP provider reached with `smtplib`. Two backends ship: `MetaWhatsAppProvider` and a `NullWhatsAppProvider` that accepts and discards, which matters more here than for email because a test cannot have a WhatsApp Business account. Every failure is translated into a `WhatsAppFailureCode` at this boundary, and the provider's own words never escape it — a Cloud API error body quotes the recipient's number, so what leaves is a numeric code and never a person. Twilio and Vonage are one class plus one registry entry each. **Off by default** (`WHATSAPP_ENABLED=false`), and it is now one of two switches that are: it is the outward-facing side effect that reaches a device in somebody's pocket, and it cannot work until templates are approved by Meta |
| WhatsApp Templates | Versioned `.j2` **descriptors** behind the `WhatsAppTemplateRenderer` protocol (`services/whatsapp_templates.py`), in `apps/api/whatsapp/` | **Not the same thing as an email template, and the difference is the design.** A WhatsApp template lives on the *provider's* side — submitted to Meta, reviewed, approved — and what the platform sends is its name, its language tag, and an ordered list of **parameters**. So a file here is a *descriptor*: it says, one per line, which values fill the approved template's slots, and its parameter count and order **are** the contract with the registered template. The sentences stay in `core/notifications.py`, where every channel gets them, and the approved template is a thin envelope around them — because a sentence held in a console is one no test can assert on, no reviewer can diff, and nothing keeps in step with the in-app feed. Autoescaping is **off**, and here that is the safe setting rather than the dangerous one: a parameter is text into a slot and never markup, so escaping would put `&#39;` on somebody's phone. `StrictUndefined`, because a descriptor that lost a variable sends a parameter list of the wrong length, which Meta refuses one message at a time |
| Authentication | JWT + OAuth2 | Secure authentication and authorization |
| Monitoring | **The platform's own**: `structlog` for logs, an in-process metric registry (`services/metrics_registry.py`), and a W3C Trace Context tracer (`core/tracing.py`, `services/tracer.py`) | The three pillars, and **no monitoring dependency was added to install them**. The exposition format a scraper wants is a hundred-line renderer over a snapshot (`services/metrics_export.py`), so `prometheus_client` would have bought a second global registry and a second opinion about what a metric *is*, beside the declarations in `core/observability.py` that already are one — the same reasoning `services/email_provider.py` gives for reaching for `smtplib`. Langfuse and OpenTelemetry remain the intended *destinations*: the wire format is already W3C, the spans already carry kinds and attributes, and an SDK dropped in front of this platform would find its own header propagated and correlated with every log line |
| Error Tracking | `services/error_tracker.py`; Sentry when a deployment wants retention | Failures **grouped** by type and location rather than listed, because the question an operator has is *"what is broken and is it getting worse?"* rather than *"what was the four hundredth exception?"*. Bounded, evicting by staleness, and holding no traceback — the traceback goes to the log beside the request id and trace id that lead back to the group. Sentry is one implementation of `ErrorTracker` plus one line in `api/deps.py` |
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
- `modules/reports` — AI-generated reports, exports, and report history.
  Implemented inside `apps/api` (`core/reports.py`, `models/report.py`,
  `repositories/report.py`, `services/report.py`, `services/report_graph.py`,
  `services/report_access.py`, `services/report_export.py`,
  `services/report_worker.py`, `schemas/report.py`, `api/v1/reports/router.py`)
  and `apps/web` (`components/reports/`, `app/(protected)/reports/`, embedded in
  `app/(protected)/cases/[id]`), following the same layering as Cases,
  Documents, Search, and the Assistant. It is the **second consumer of the RAG
  pipeline and the first non-conversational one**: every section of every report
  is a `RagService.answer` call, so the agent holds no search service, no
  embedder, no vector searcher, and no prompt library. Unlike the assistant it
  **persists a run with a lifecycle** — `pending` → `processing` → `completed` |
  `failed` — which is why it has a table, a worker pool, and SQL metrics where
  the assistant has counters. Legal summaries as a *separate* capability,
  compliance analysis, and translation remain unimplemented.
- `modules/realtime` — The platform's event backbone, and the only module that is
  *infrastructure for other features* rather than a feature. Implemented inside
  `apps/api` (`core/events.py`, `core/realtime.py`, `services/events.py`,
  `services/event_metrics.py`, `services/realtime_access.py`,
  `websocket/protocol.py`, `websocket/connection.py`, `websocket/manager.py`,
  `schemas/events.py`, `api/v1/websocket/router.py`) and `apps/web`
  (`lib/realtime/`, `hooks/use-realtime.ts`, `components/realtime/`), following
  the same layering as every other module. It **owns no business rule and no
  entity**: it has no table, no migration, and no repository, because an event is
  not something anyone reads back — a client that missed one refetches, which is
  authoritative where a replay would only be a hint.
- `modules/notifications` — In-app notifications: a centralized Notification
  Service subscribed to the event dispatcher above, with persistence, categories,
  types, priorities, per-user preferences, read state, history, and real-time
  delivery. Implemented inside `apps/api` (`core/notifications.py`,
  `models/notification.py`, `repositories/notification.py`,
  `services/notification.py`, `services/notification_events.py`,
  `services/notification_recipients.py`, `services/notification_metrics.py`,
  `schemas/notification.py`, `api/v1/notifications/router.py`) and `apps/web`
  (`components/notifications/`, `app/(protected)/notifications/`, and the bell in
  `components/layout/notification-button.tsx`), following the same layering as
  every other module. It is the **first consumer of the dispatcher** rather than
  another producer on it, and it is where *persistence* of an event enters the
  platform: synchronization is ephemeral by design, a notification is not.
  **Push, SMS, and reminder scheduling remain unimplemented** and are what the
  preference model's remaining channel columns prepare for — a shape that has now
  been cashed twice, by email and by WhatsApp, at one boolean column each.
- `modules/notifications` — **email delivery**: the second channel the
  notifications above travel on, and deliberately a *consumer of notifications
  rather than of domain events*. Implemented inside `apps/api` (`core/email.py`,
  `models/email.py`, `repositories/email.py`, `services/email_delivery.py`,
  `services/email_provider.py`, `services/email_templates.py`,
  `services/email_metrics.py`, `services/email_worker.py`, `schemas/email.py`,
  the templates in `apps/api/emails/`, and one endpoint on the existing
  notifications router) and `apps/web` (a second column in
  `components/notifications/notification-preferences-form.tsx`), following the
  same layering as every other module. It is **not a module directory of its
  own**, and that is the point: it adds no business rule, no audience, and no
  wording — it attaches to the notification the way OCR attaches to a document,
  one stage further out. Its whole subscription list is `EMAIL_RULES`, and what
  is *absent* from it (every `document.*`, every `ocr.*`, every `indexing.*`, the
  assistant, the timeline) is the spec's *"Events That Must NOT Generate Emails"*
  enforced by a test rather than by review.
- `modules/notifications` — **WhatsApp delivery**: the third channel the same
  notifications travel on, and — like email — a *consumer of notifications rather
  than of domain events*. Implemented inside `apps/api` (`core/whatsapp.py`,
  `models/whatsapp.py`, `repositories/whatsapp.py`,
  `services/whatsapp_delivery.py`, `services/whatsapp_provider.py`,
  `services/whatsapp_templates.py`, `services/whatsapp_metrics.py`,
  `services/whatsapp_worker.py`, `schemas/whatsapp.py`, the descriptors in
  `apps/api/whatsapp/`, and one endpoint on the existing notifications router) and
  `apps/web` (a third column in
  `components/notifications/notification-preferences-form.tsx`). It is **not a
  module directory of its own** either, and the fact that adding it required no new
  abstraction is the interesting part: it implements the same one-method
  `NotificationDispatcher`, reuses the persisted delivery lifecycle, the
  conditional-`UPDATE` claim, the retry schedule with its sweeper, and the
  two-source metrics split, and is registered as one more entry in
  `api/deps.py`'s channel list. Its whole subscription list is `WHATSAPP_RULES`.
  **Three things are genuinely its own**, and each is a property of the medium
  rather than of the platform: the *provider* is HTTP rather than SMTP; the
  *template* lives on Meta's side and is approved rather than deployed, so what
  ships here is a **descriptor** of the parameters that fill it; and the
  *recipient* is `users.phone`, which is **optional**, so an account with no number
  is skipped forever and correctly — a skip reason the metrics report rather than a
  failure they count.
- `modules/dashboard` — The landing page: a widget system, role-aware layouts,
  operational analytics, quick actions, and one aggregated endpoint. Implemented
  inside `apps/api` (`core/dashboard.py`, `repositories/dashboard.py`,
  `services/dashboard.py`, `services/dashboard_access.py`,
  `services/dashboard_metrics.py`, `schemas/dashboard.py`, `api/v1/dashboard/`)
  and `apps/web` (`components/dashboard/`, `hooks/use-dashboard.ts`,
  `lib/api/dashboard.ts`, `app/(protected)/dashboard/`), following the same
  layering as every module above.

  **It is the only module with no model and no migration**, and that is its
  defining property rather than an omission: it owns no data. A dashboard is a
  *read* across Cases, Documents, OCR, Indexing, Timeline, Reports, the
  Assistant, Users, and Notifications, so its dependencies point at all of them
  and **none of theirs points back** — no business module knows the dashboard
  exists, exactly as none of them knows Notifications does. Removing it would
  remove a page and nothing else.

  Its authorization is delegated rather than owned (`dashboard_access.py` holds
  no rules; it asks `CaseAccessPolicy` and uses identity equality for private
  histories), and its live updates are **server-described**: each widget declares
  the domain events that make it stale and the API serves that list, which is why
  `apps/web` has no widget-to-event table. See the Dashboard & Analytics section
  below.
- `modules/settings` — The unified interface over the platform's configuration:
  profile, account security and active sessions, notification and communication
  preferences, AI and dashboard presentation, appearance, language and region,
  and the administrator's platform settings. Implemented inside `apps/api`
  (`core/settings.py`, `models/settings.py`, `repositories/settings.py`,
  `services/settings.py`, `services/settings_metrics.py`,
  `services/session_registry.py`, `schemas/settings.py`, `api/v1/settings/`) and
  `apps/web` (`components/settings/`, `hooks/use-settings.ts`,
  `hooks/use-date-format.ts`, `lib/api/settings.ts`,
  `app/(protected)/settings/`), following the same layering as every module
  above.

  **It is the first module that is mostly a *view over other features' writes*,**
  and its governing rule is `20-settings.md`'s own: *each feature owns its
  configuration; Settings only presents and manages it*. So four of its nine
  sections store nothing here — Profile writes the `users` row User Management
  owns, Account & Security delegates to `AuthService`, and Notifications and
  Communication are two projections of `notification_preferences`, read and
  written through `/notifications/preferences`. What it genuinely owns is the
  configuration **no feature had a home for**: Appearance, Language & Region, AI
  presentation, Dashboard preferences, and the platform's own settings. See the
  Settings section below.
- `modules/monitoring` — Monitoring & observability: structured logging context,
  metrics, distributed tracing, health and readiness, error tracking, security
  monitoring, background-job monitoring, and an operator's dashboard.
  Implemented inside `apps/api` (`core/observability.py`, `core/tracing.py`,
  `services/metrics_registry.py`, `services/metrics_export.py`,
  `services/tracer.py`, `services/error_tracker.py`,
  `services/security_monitor.py`, `services/system_metrics.py`,
  `services/database_metrics.py`, `services/monitoring.py`,
  `schemas/monitoring.py`, `api/v1/monitoring/`) and `apps/web`
  (`components/monitoring/`, `hooks/use-monitoring.ts`, `lib/api/monitoring.ts`,
  `app/(protected)/monitoring/`).

  **It is the first module that is entirely cross-cutting**, and that is its
  defining property rather than a description. Dashboard is a view over other
  modules' *rows*; Settings is a surface over their *configuration*; this is a
  view over their *instrumentation* — so it owns no table, no migration, no
  event, and no worker, which makes it the fourth module with no model after
  Dashboard, Real-Time Synchronization, and Localization, and for a fourth
  distinct reason: **a metric written to PostgreSQL would make the database a
  dependency of the thing that watches the database.**

  **No business module changed to add it.** Every observation is taken at an edge
  the platform already owned: the HTTP middleware (`core/middleware.py`) opens
  the root span, binds the log context, and records request metrics; the
  exception handlers (`core/exceptions.py`) classify *every* failed sign-in,
  invalid token, permission denial, and rate limit as a security event without
  `AuthService` or any access policy learning that monitoring exists; the
  SQLAlchemy engine listener times statements by **verb** and never by text; and
  the lifespan attaches all of it. The only two lines added to a router are the
  *successful* sign-in and the password change — the denominator without which a
  failure count cannot be read, and which are not exceptions.

  **It reads the eleven metrics recorders eleven previous features shipped with,
  and writes to none of them.** `services/monitoring.py` copies their numeric
  fields into one snapshot so an operator has a page rather than eleven, and
  bridges them into the registry as `feature_metric{feature,metric}` so a scraper
  has one endpoint rather than eleven — reflectively, so a figure added to
  `RagMetricsSnapshot` next month appears in both with no change to either. The
  one recorder it deliberately does **not** read is `services/event_metrics.py`:
  its snapshot needs the live connection count, which only `websocket/manager.py`
  holds, and `code-standards.md` says nothing outside that package, the lifespan,
  and the endpoint may import it. Real-time metrics stay on
  `GET /realtime/metrics`.

  **Its authorization is a permission and nothing else.** There is no
  `monitoring_access.py`, and the absence is structural rather than an omission:
  every figure here is platform-wide, so the question a per-resource policy would
  answer cannot be asked. See the Monitoring section below.
- `modules/users` — Administrator, lawyer, and court representative management.
  Implemented inside `apps/api` (`services/user.py`, `repositories/user.py`,
  `api/v1/users/`) and `apps/web` (`components/users/`, `app/(protected)/users/`),
  following the layering the backend already uses rather than introducing a
  separate deployable.
- `modules/localization` — English, French, and Arabic: the language vocabulary,
  per-account language resolution, translation catalogues, language switching,
  and RTL. Implemented inside `apps/api` (`core/localization.py`,
  `repositories/localization.py`, `services/localization.py`,
  `services/localization_metrics.py`, `schemas/localization.py`,
  `api/v1/localization/`) and `apps/web` (`lib/i18n/`, `messages/*.json`,
  `components/i18n/locale-provider.tsx`,
  `components/layout/language-switcher.tsx`, `hooks/use-number-format.ts`,
  and the date half of `hooks/use-date-format.ts`).

  **It owns no table and no migration**, which makes it the third such module
  after Dashboard and Real-Time Synchronization — and for a third distinct
  reason: a language preference is a *setting*, so it lives in `user_settings`
  where Settings put it, and this module reads it. There is deliberately **no
  write path here**: `PATCH`ing a language goes through `/settings/preferences`
  like every other preference, because a second endpoint serving one stored value
  is how two answers to one question start to disagree.

  **It adds one permission and it is a `*:monitor`.** Reading the interface in
  Arabic is not a capability anybody needs a grant for; choosing a language is
  `settings:update`. `localization:monitor` gates the metrics view alone. See the
  Localization section below.
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
  `infrastructure/docker/api.Dockerfile` is the **API image**, and it is where
  everything the platform needs but pip cannot install actually gets installed:
  **Tesseract** with its `fra`/`ara` language packs, **Poppler**, and a font with
  **Arabic** coverage (`fonts-noto-core`) for PDF report export. Two decisions in
  it are worth knowing before reading it: **torch is resolved from the CPU
  index**, because the default Linux wheel bundles ~2.5 GB of CUDA runtime a
  CPU-only deployment never executes; and the bge-m3 cache lives on a **named
  volume** (`HF_HOME=/models`), because a 2.3 GB model re-downloaded on every
  container replacement would make a restart take minutes. The compose service
  sits behind an **`api` profile**, so `docker compose up -d` keeps meaning "the
  four backing services" for local development and
  `docker compose --profile api up -d --build` runs the application too.
  Migrations are a deploy step rather than a container start step, so replicas
  cannot race one another on scale-up.

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
- Notifications (one row per thing one person was told — its rule, a bounded
  context, its category, type, priority, target, actor, read state, and the
  identity of the event it came from. **No title and no message**: the wording is
  rendered per request in the reader's language, so a history is Arabic for an
  Arabic reader rather than frozen per row)
- Notification Preferences (one row per `(user, preference)` the user has
  actually expressed an opinion about — an untouched account has none and follows
  the platform defaults — with **one boolean column per delivery channel**:
  `in_app`, `email` since the email channel shipped, and `whatsapp` since the
  WhatsApp one did. Two channels added at one column each, with no new table, no
  new key, and no backfill either time, is the evidence that "a row per
  `(user, key)`" was the right shape rather than the claim that it was)
- Email Deliveries (one row per notification the platform tried to deliver by
  email — its envelope, its lifecycle, its attempt count, and the machine-readable
  reason it stopped. **No subject and no body**: the message is rendered per
  attempt from the same module the in-app feed renders from, and a column holding
  the contents of an email is exactly what the spec's Logging section forbids
  putting in a log. A unique index on `notification_id` is the whole of "avoid
  duplicate emails")
- WhatsApp Deliveries (one row per notification the platform tried to deliver over
  WhatsApp — the same shape as an email delivery, for the same reasons, with two
  deliberate differences. `recipient_phone` is stored in **normalized E.164
  digits** rather than as typed, because `users.phone` is a free-text display
  field and two rows for the same person have to be comparable; and
  `provider_message_id` keeps the `wamid` Meta returns, which is the only handle
  that correlates a row with anything on the provider's side — a support case, a
  Business Manager log, and the delivery-receipt webhook a later feature would
  consume. **No message text**, for the reason there is no email body: the wording
  is rendered per attempt from `core/notifications.py`. A unique index on
  `notification_id` is the whole of "avoid duplicate messages", and it matters more
  here than one channel over — two phone alerts about the same hearing leave a
  reader unable to tell which is current)
- User Settings (one row per `(user, setting)` the person has actually expressed
  an opinion about — an untouched account has none and follows the platform
  defaults. The value is **JSON**, because the registry is open by design: a
  tenth setting is an entry in `core/settings.py` with no migration, which is
  what `20-settings.md`'s *"support future sections without redesign"* asks for.
  The type discipline moves from the column to
  `core.settings.validate_setting`, applied to the whole batch **before**
  anything is written — which is what makes *"invalid configuration should never
  corrupt stored preferences"* a property of the ordering rather than of care.
  The shape is `notification_preferences`' own, reused rather than reinvented)
- Platform Settings (one row per setting an administrator has configured, with
  **no `user_id` at all** — that absence *is* the spec's *"administrator settings
  should remain isolated from regular user settings"*: there is no column to
  scope by, so no query can serve a platform value as somebody's preference.
  `updated_by` is on the row rather than only in the log, because *"who turned
  maintenance mode on?"* is asked days later from a database. Every `default_*`
  key here is the fallback an account with no stored row follows, so changing one
  reaches every such account with no backfill)
- Timeline Events
- Audit Logs
- AI Conversations (one thread per user, with its counters and its last-message
  preview denormalized for the list row)
- AI Conversation Messages (one row per turn — the question verbatim, or the
  pipeline's answer with its citations, suggestions, and provenance as JSON)
- AI Message Feedback (one rating per assistant message, in its own table so
  that rating an answer cannot alter the transcript it is read from)
- Reports (one row per generated report — its lifecycle, its progress counters,
  its sections and citations as JSONB, and the provenance an evaluation needs.
  **One table rather than two**: a section is never read, filtered, paged, or
  referenced on its own, so a `report_sections` table would buy a join on every
  read and a second place for the section order to live)
- Language Preferences — **no table of their own**. A person's language is one
  row in `user_settings` (`key = 'language'`), the deployment's is
  `platform_settings.default_language`, and the application's is the
  `DEFAULT_LANGUAGE` environment variable. That is three layers and **zero
  migrations**, which is exactly what `20-settings.md`'s "one row per
  `(owner, key)`" shape was chosen to buy — and the fourth time it has been
  cashed, after two notification channels and the Settings registry itself. The
  listing kept the name because `architecture.md` had promised it since the
  beginning; what it describes is now a *read*, not a store

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
- OCR outputs
- Future voice recordings

**Generated reports and their exports are deliberately *not* here**, and the
listing above no longer claims otherwise. A report's content lives in PostgreSQL
(`reports.sections`), and an export is a **deterministic projection** of that
row rendered per request by `services/report_export.py`. Storing the rendered
bytes would create a second copy that goes stale the moment a report is
regenerated, and would need a lifecycle, a cleanup job, and an authorization
story of its own — whereas rendering per request makes
`14-ai-report-agent.md`'s *"exported reports inherit the same permissions as
their source case"* **structural**: there is no object anyone can be handed a
URL to, and every byte is produced inside a request that has already resolved
the report through an owner-scoped query. The trade is a few milliseconds of CPU
per download against minutes of generation, and it is revisitable — a future
scheduled-delivery feature that emails a report would be the first genuine
reason to persist one.

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
- Active session records (one hash per user, one field per **sign-in**, keyed by
  the `sid` claim rather than by `jti` — a `jti` rotates every fifteen minutes,
  so a registry keyed by one would show a lawyer twenty "devices" for one laptop
  by lunchtime. Added by Settings, whose Account & Security section needs to
  *name* a session where the platform could previously only revoke every one.
  **A view, never a boundary**: nothing here is consulted when a request is
  authorized, so unlike the denylist it fails **soft** — an unreachable Redis
  costs the *list*, never a session that should have ended and did not, because
  what ends one is `users.session_generation` in PostgreSQL)
- Failed-login counters and lockouts (per account and per client IP, keyed by
  scope with a TTL equal to the failure window / lockout duration)
- WebSocket Pub/Sub messages (**reserved, not yet used** — see the stack table)
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
  `DELETE /settings/sessions` ("sign out everywhere else") is the **same
  mechanism with the password left alone**, deliberately rather than a second
  one: no enumeration is involved, so a device the session registry never heard
  of is signed out exactly like one it did.
- **Every token carries a `sid` claim**, minted once at sign-in and preserved
  across every refresh rotation. It is the identity of a *session* where `jti` is
  the identity of a *credential*, and it **grants nothing** — authorization is
  still the signature, the denylist, and `sgen`. It exists so
  `GET /settings/sessions` can list sign-ins rather than tokens, and a token
  minted before the claim existed is accepted without one, so introducing it
  signed nobody out.
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
  - **`reports:view` / `reports:generate` / `reports:monitor`** close the chain,
    and the first of them is the only permission on the platform that scopes to
    a **user** rather than to a case. A report is its author's private work
    product (`14-ai-report-agent.md`: history *"must remain user-specific"*), so
    every read in `repositories/report.py` is keyed by `requested_by` and there
    is deliberately **no `reports:view-all`** — an administrator holds
    `cases:view-all` and still cannot read somebody else's report, because that
    permission lifts a *row* restriction rather than an ownership one. The
    consequence is the platform's one deliberate asymmetry in refusals: an
    inaccessible **case** is a 403 (a lawyer needs to know it exists and to ask
    for assignment), while another user's **report** is a 404 (confirming it
    exists is itself the disclosure). Generating requires **both**
    `reports:generate` and `ai:generate-report`, in the shape the assistant
    established for `ai:chat` + `ai:ask`; `reports:monitor` is administrative and
    is withheld from lawyers like every other `*:monitor`.
  - **`realtime:connect` / `realtime:monitor`** are the last pair, and the first
    of them is the narrowest permission on the platform: **it grants access to
    nothing.** Every event travels on a *topic*, and a topic is authorized per
    resource by `services/realtime_access.py`, which — like `ocr_access`,
    `indexing_access`, `search_access`, and `document_access` before it — owns no
    policy of its own and delegates: **topic to case / document / report, then to
    the module that already decides**. So the socket a court representative opens
    with it carries exactly the updates for the cases they could already open,
    and nothing else, which is why it sits in `BASE_PERMISSIONS` beside
    `notifications:view`: a role without it would watch stale screens while every
    other role's updated, and that is a defect rather than a policy.
    Two consequences of the chain are worth stating, because they are the only
    places this feature could have leaked. A **document** event fans into its
    case's topic (`CASE_FANOUT_SCOPES`), which is safe precisely because document
    access *is* case access one hop out — so a case follower learns nothing that
    following the case did not already disclose. A **report** event deliberately
    does **not**: it is about a case and carries its identifier, but it belongs to
    the user who generated it, so the fan-in rule is a set of *scopes* rather than
    a rule about `case_id`. The case's participants learn from the timeline that a
    report exists; only its author watches it being written.
    `realtime:monitor` gates the connection metrics and the presence roster, and
    is administrative like every other `*:monitor` — presence in particular,
    because who is online is a product decision `15-real-time-synchronization.md`
    puts out of scope.
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
  - **`settings:view` / `settings:update` / `settings:manage` /
    `settings:monitor`** are the last four, and the first two are the only pair
    on the platform where *both* halves sit in `BASE_PERMISSIONS`. They are the
    caller's **own** preferences: a role that could read the theme it was stuck
    with and not change it is not a policy anybody would write. There is
    deliberately **no `settings:view-all`**, **no `settings:update-any`**, and —
    the load-bearing part — **no `settings_access.py`**. *"Users may modify only
    their own settings"* is enforced by the **absence of a parameter**: no method
    on `SettingsService` and no route in `api/v1/settings/` takes a user
    identifier, so a caller cannot ask for somebody else's settings because there
    is nowhere to put the request. That is the shape
    `repositories/conversation.py` and `repositories/notification.py` use, one
    step further — they scope a query, this has no query to scope.
    `settings:manage` is **not a wider form of `settings:update`**: it acts on a
    different table through different routes, and holding every user-settings
    permission grants none of it. `settings:monitor` is administrative like every
    other `*:monitor`.
  - **`monitoring:view` / `monitoring:export`** are the last two, and **neither
    is a `*:monitor`** — which is the substance of them rather than a naming
    quirk. Eleven features each added a `<feature>:monitor` gating *their own*
    operational view; this one **is** the operational view, so its permissions are
    named for what they grant. Both are administrator-only, which is
    `22-monitoring.md`'s *"regular users must never access monitoring endpoints
    or operational metrics"* — and there is deliberately **no
    `monitoring_access.py`**, because every figure behind them is a property of
    the *platform* rather than of anybody's rows. An uptime, a queue depth, and a
    p95 have nothing to scope to, so the question a per-resource policy would
    answer cannot be asked and there is no narrower version of this to grant
    anybody. That is the same *"scope by the absence of a parameter"* shape
    Settings uses, arrived at from the opposite direction: there, no route takes
    a user identifier; here, no figure has an owner.
    `monitoring:export` is separate from `monitoring:view` for a practical reason
    rather than a theoretical one: the account holding it is a **scraper**, not a
    person, and a deployment should be able to give Prometheus a credential that
    reads counters and cannot read the security feed, the error list, or the
    trace buffer — each of which says considerably more about the platform than a
    counter does.
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

### AI Report Generation

Implemented per `context/feature-specs/14-ai-report-agent.md`. The sixth stage of
the AI pipeline and the **second consumer of the fourth**: it begins at a report
type chosen for a case and **ends at a persisted, structured, cited document**
that can be exported. It is deliberately not summarization-as-a-feature, not
compliance analysis, and not translation.

- **A report section *is* a pipeline answer, and that one fact is the whole
  design.** The spec forbids duplicating retrieval, prompt construction, or LLM
  interaction, so the agent calls `RagService.answer` once per section and does
  none of the three. `ReportService` holds no vector searcher, no embedder, no
  search service, and no prompt library — so *"it must never query Qdrant
  directly"* and *"generated reports must never contain unauthorized
  information"* are inherited from the pipeline rather than restated, exactly as
  the assistant inherits them.
- **The agent is its own LangGraph graph** (`services/report_graph.py`), which is
  the shape `services/rag_graph.py` reserved for it. Five nodes — plan, write
  section, assemble, validate, finalize — and ``write_section`` is a
  **self-looping** node whose conditional edge sends control back to itself until
  the template is exhausted. That loop is the spec's "Large Cases" requirement
  made structural: a case larger than any context window costs more *iterations*
  rather than a bigger prompt.
- **Section instructions are domain data, not prompts.** `14-ai-report-agent.md`
  lists prompt construction under *Do NOT implement*, and it is obeyed literally:
  the strings in `core/reports.py` are the *questions the platform asks about a
  case*, which the pipeline then fences inside its own versioned `rag/answer`
  template. They are versioned as a set by `REPORT_TEMPLATE_VERSION`, recorded on
  every report, so an evaluation can group by them the way it groups by a prompt
  version.
- **Markers are renumbered, which is not the same as modified.** The pipeline
  numbers each answer's sources `[1]`…`[n]`, and a report is one document made of
  a dozen answers — so `CitationLedger` assigns one global numbering,
  de-duplicates on (document, version, page), and rewrites each section's prose
  against its own mapping **in one pass**. A source beyond
  `REPORT_MAX_CITATIONS` gets no marker and its reference is *removed* from the
  prose, which is the spec's *"reports should never invent citations"* applied to
  the one place this feature could have invented one.
- **A section the case file does not cover says so, in the platform's own
  words**; a report in which *nothing* could be grounded is a **failure**
  (`insufficient_context`) rather than a document of empty headings. The first is
  a finding, the second would be a several-hundred-token way of saying "this case
  has no indexed documents" that a lawyer would read as a considered answer.
- **A failed section fails the whole run**, which is the opposite of indexing's
  choice about partial vectors and deliberate: a partial index is a smaller index
  and still correct, while a partial *report* is a legal document missing sections
  with nothing on its face to say so. Half a report is not a smaller report.
- **A report is a persisted run, so its metrics are SQL.** Every figure the spec
  names — generated reports, average generation time, export count, failed
  generations, average report size, token usage — is an aggregate over `reports`,
  which is why `GET /reports/metrics` carries no `since` caveat while search,
  RAG, and the assistant all do. Those three persist nothing and must count in
  the process; this one does not.
- **Its own worker pool**, separate from OCR's and indexing's, and sized by an
  API quota rather than by a core count: a report is a burst of calls to a
  metered language model, so `REPORT_WORKER_CONCURRENCY` defaults to **1** and a
  second worker would only double the rate at which a key is spent.
- **Two authorization questions with two different answers.** *May this caller
  generate a report about this case* is a question about the **case**, delegated
  to `CaseAccessPolicy` by `services/report_access.py` and refused with **403**.
  *May this caller read this report* is a question about **ownership**, answered
  by the repository — every read is keyed by `requested_by` — and refused with
  **404**, because confirming that another user's private work product exists is
  itself the disclosure the spec forbids. There is deliberately no
  `reports:view-all`, and an administrator does not read other people's reports.
- **`reports:monitor` is the one new permission**, following `ocr:monitor`,
  `indexing:monitor`, `search:monitor`, and `ai:monitor`. Generating requires
  **both** `reports:generate` and `ai:generate-report`; `ai:ask` is deliberately
  *not* additionally required, because it gates the ad-hoc question endpoint and a
  deployment that wants reports without ad-hoc questioning is a coherent policy.
- **Exports are rendered per request, never stored** — see the MinIO note above.
  `services/report_export.py` is the seam: `MarkdownReportRenderer` is **always
  available** (no library behind it, which is what every "try Markdown instead"
  message rests on) and `PdfReportRenderer` is ReportLab, imported lazily and
  reported as unavailable rather than fatal.
- **Arabic PDF export works unconfigured, and getting there took three things.**
  `project-overview.md` names Arabic as one of the platform's two languages, so
  an export that needed manual setup would be half the intended users locked out.
  ReportLab's built-in Type 1 fonts are Latin-only, so: a font is **discovered**
  from `ARABIC_FONT_CANDIDATES` when `REPORT_PDF_FONT_PATH` is unset (Amiri,
  DejaVu, FreeSerif, and the macOS/Windows system faces); every candidate is
  **verified against the font's own character map** rather than trusted by name;
  and the text is **shaped and reordered** by `arabic-reshaper` + `python-bidi`,
  which are **required** dependencies — unlike `litellm`, which is an alternative
  to something that already works, those two are the difference between correct
  Arabic and mangled Arabic.
- **What the font check verifies is not "Arabic", and the difference is the whole
  reason it exists.** `REQUIRED_CODEPOINTS` demands four things: an Arabic letter,
  an Arabic **presentation form** (the shaper converts to those *before* drawing,
  so a font with the base block and not these renders nothing), a Latin capital,
  and an **em dash**. The last two are what a first attempt got wrong:
  `NotoNaskhArabic` is the obvious package by name, renders Arabic beautifully,
  and carries **no Latin and no em dash** — so every case number
  (`CASE-2026-0001`), filename, page reference, and citation line
  (`[1] bail.pdf — p. 7 (v1)`) in an Arabic report would have come out as boxes.
  A bilingual legal document needs one font covering both scripts, which Amiri,
  DejaVu, FreeSerif, Arial, and Tahoma all do. A configured path that is missing
  or fails the check falls back to the search rather than failing. Only when
  nothing is found anywhere is an Arabic PDF **refused**, with a message naming
  Markdown — a legal report that exports as a page of empty boxes is worse than
  one that does not export.
- **A reasoning model's thinking is charged against the output ceiling, and a
  section is not a chat reply.** A live run at `LLM_MAX_OUTPUT_TOKENS` (1024)
  returned **41 visible tokens** — a 151-character section cut off mid-sentence —
  because roughly 983 went to deliberation first. `REPORT_SECTION_MAX_OUTPUT_TOKENS`
  is therefore sized for the model rather than for a paragraph, and it is passed
  through `RagService.answer(..., max_output_tokens=...)` as a **method argument
  rather than a field on `RagRequest`**: a token ceiling is a provider concept,
  and the pipeline keeps its wire budgets in characters precisely so the contract
  does not acquire one. A section that still hits the ceiling is reported as
  `truncated` rather than presented as complete.
- **No section, title, or citation reaches a log.** The logs carry identifiers,
  statuses, section *keys*, counts, and character counts only — a generated
  interpretation of a case file is at least as sensitive as the passages it was
  built from, which OCR, indexing, search, RAG, and the assistant each already
  refuse to log.
- **The case timeline records that a report exists, never what it says.**
  Requested, generated, failed, and exported are published to the people already
  party to the case, which is the collaboration invariants 3 and 9 ask for — and
  it grants nothing: the report itself stays readable only by its author. This is
  the opposite of the assistant's choice not to publish at all, and the
  difference is that a conversation is one lawyer's private research while a
  report is case work product.

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

### Real-Time Events & Synchronization

Implemented per `context/feature-specs/15-real-time-synchronization.md`. Not a
stage of the AI pipeline and not a feature in the usual sense: it is the
**transport every other feature's updates travel on**, and the reusable
infrastructure Notifications, Email, WhatsApp, Dashboard Analytics, and
Monitoring are all specified to build on.

- **One dispatcher, two narrow protocols, and nothing that sees both except the
  dispatcher itself.** `EventPublisher` is what a business module depends on —
  one method, taking a type, a topic, and a payload. `EventSubscriber` is what a
  consumer implements. The case, document, OCR, indexing, report, and timeline
  services take the *publisher* protocol and never the dispatcher, so none of
  them can register a consumer, enumerate who is listening, or reach a socket.
  The spec's *"business modules should publish events but should never know who
  consumes them"* is therefore a property of the dependency graph rather than a
  convention. **The two halves are joined in exactly one place** —
  `core/lifespan.py` — which is also where a second consumer will be added.
- **An event is not persisted, and that is the design rather than an omission.**
  Synchronization is ephemeral: a client that missed something **refetches**,
  which is authoritative where a replayed event is only a hint. So there is no
  table, no migration, and no repository — the first module since Semantic Search
  to have none. What *is* durable is the timeline entry beside each event, and
  Notifications is where persistence of an event properly enters the platform.
- **Publishing never fails a caller, and never waits on one.** An event is a side
  effect of a change that has already committed, so a subscriber that raises is
  logged and skipped and a subscriber that is slow hands the event to a queue.
  `EventDispatcher.publish` is O(subscribers), not O(clients).
- **Confidential material cannot travel, by construction.** `normalize_payload`
  bounds a payload to twenty flat, scalar keys and **strips** the ones that name
  document contents (`text`, `full_text`, `sections`, `citations`, `answer`,
  `question`, and the rest), in the dispatcher, once — so a publisher that
  forgets is a log line rather than a disclosure. The consequence is visible in
  every payload: a case event carries the case *number* and never its title, a
  document event carries identifiers and the file's shape and never its filename,
  and a report event carries counters and never a section. The client resolves
  the rest through the authorized read it makes next, which is the whole point.
- **Authorization is applied before every delivery, and the bound on that is
  stated rather than hidden.** A grant carries the moment it was authorized; past
  `REALTIME_AUTHORIZATION_TTL_SECONDS` (30) it is re-resolved against the
  database — **including the account**, so a deactivated user's open socket goes
  quiet — and a refusal *revokes* the subscription rather than skipping one event.
  Re-resolving on literally every delivery is a supported configuration (`0`) and
  is one query per event per connection, which is what the TTL exists to avoid.
  The residual window applies to *notifications that something changed*, never to
  the changed data: every REST read behind them is authorized afresh.
- **The socket authenticates with its first frame, never its URL.** A browser
  cannot set an `Authorization` header on a WebSocket, and the two alternatives
  are both worse than a round trip: a query parameter writes a bearer token into
  the reverse proxy's access log and the browser's history — the three logs
  `11-semantic-search.md` made search a POST to stay out of — and a cookie would
  make the socket CSRF-reachable, undoing the header scheme `03-authentication.md`
  chose. An accepted socket may send exactly one kind of frame and is closed after
  `REALTIME_AUTH_TIMEOUT_SECONDS`.
- **The channel is read-only.** There are five client frame types and none of them
  mutates anything; a socket that could would be a second, thinner door onto the
  same business logic. Its HTTP surface is one status probe and two
  administrative reads.
- **Three threads' worth of concerns are kept apart.** Publishers run on request
  and worker threads; sockets live on the event loop. The manager owns a dispatch
  thread between them, so `handle()` is a queue put, no database work ever runs on
  the loop, and every queue in the path is bounded — a burst the platform cannot
  deliver is dropped and *counted* rather than accumulated in memory.
- **A slow consumer is closed rather than buffered.** Dropping events silently
  would desynchronize a client that believes it is live; an unbounded queue makes
  one client that stopped reading into the process's memory problem. Closing says
  "reconnect and refetch", which is the only outcome that leaves the client
  correct.
- **Duplicates are avoided at both ends, because a reconnect legitimately
  re-offers events.** Every event carries a stable id and a monotonic sequence:
  the server suppresses within a bounded per-connection window, the client keeps
  its own, and a *gap* in the sequence is what tells a client it must refetch.
- **Presence is tracked and deliberately not shown.** The roster counts
  connections per account and is gated on `realtime:monitor`; it never reports
  what anyone is subscribed to, which would be a live index of who is working on
  which matter. Online indicators, active viewers, and collaborative editing are
  the features this makes possible and are each a product decision.
- **No topic, payload, case, or filename reaches a log or a metric.** Connection
  logs carry a connection id, a user id, and a role; subscription logs carry
  *counts*; the metrics view reports throughput by event *type* — "eleven
  documents were uploaded" — where a per-topic breakdown would be a statement
  about a client's matter.
- **Nothing depends on it.** Every list, pipeline, and report still polls, so a
  deployment with `REALTIME_ENABLED=false`, a failed connection, and a browser
  that blocks WebSockets all leave the application exactly as it was before this
  feature. The channel makes those polls feel immediate; it is never the reason
  something is correct.

### Notifications (In-App)

Implemented per `context/feature-specs/16-notifications.md`. Not a stage of the
AI pipeline and not a producer on the event channel: it is the **first consumer**
of the dispatcher Real-Time Synchronization built, and the point at which
*persistence of an event* enters the platform.

- **Adding the first consumer took one class and one line**, exactly as
  `services/events.py` predicted. `NotificationEventSubscriber` implements
  `EventSubscriber`; `core/lifespan.py` subscribes it beside the WebSocket
  manager. **No business module changed** — they hold `EventPublisher`, which has
  one method and no way to ask who is listening — and no business module imports
  the notification service, so `code-standards.md`'s *"all notifications must be
  generated by the Notification Service"* is true because there is no function a
  business module could call, not because nobody has called one.
- **Delivery goes back onto the same channel, and the service never touches a
  socket.** It persists a notification and then *publishes*
  `notification.created` on the recipient's own `user:<id>` topic, which the
  connection manager already routes and already authorizes — a user topic is
  identity equality, so a notification cannot reach anyone but its recipient even
  if the service were wrong about who it was for. The payload carries
  identifiers, a category, a type, and a priority, and **no wording at all**.
- **No prose is stored, and that is the load-bearing decision.** A row keeps a
  `rule_key` and a small screened `context`; the title and message are rendered
  per request by `core/notifications.py` in the language the reader asks for.
  Three things follow: an Arabic reader's **whole history** is Arabic rather than
  frozen per row, which is what `ai-workflow-rules.md`'s localization rules
  actually require of a persisted feed; *"never log confidential notification
  contents"* is trivially true because there is nothing to log; and a future
  email or WhatsApp sender renders from the same module rather than restating the
  wording. The cost is stated rather than hidden — a withdrawn rule falls back to
  its category's generic wording instead of raising, because a vague notification
  is much better than a history page that will not load.
- **`EVENT_RULES` is the whole subscription list**, and what is *absent* from it
  is documented as a decision rather than left as a gap: `document.updated`,
  `ocr.started`, every `indexing.*`, `report.started`, `report.progress`,
  `timeline.updated` (which would notify everything twice, since it is derived
  from the same changes), `presence.changed`, and `notification.*` — whose
  absence is what makes a feedback loop impossible rather than merely unlikely.
  `user.deactivated` is the instructive one: the rule was written and then
  removed, because a disabled account cannot sign in to read it.
- **Three refinements read a payload, and each fails soft.** A case assignment
  becomes *assigned* or *unassigned*; a `case.updated` whose changed-field labels
  include the court fields becomes *hearing* news; a status change **into**
  `waiting_for_hearing` becomes *hearing awaited*. Each derives from wording
  `services/case.py` publishes without knowing notifications exist, so a renamed
  label degrades to ordinary case news — less specific, never missing.
- **Authorization is two passes, and the second is asked last and per person.**
  The rule's audience says who the platform *intends* to tell;
  `services/notification_recipients.py` then re-checks each resolved recipient
  against the policy that owns the resource. It owns no policy of its own —
  case → `CaseAccessPolicy`, document → its case (which *is* the document check,
  since `document_access.py` owns no policy either), report → its author, user →
  identity — so an audience that widened by accident would still be narrowed.
  A disabled account is dropped here as well.
- **A case audience is its participants, deliberately not every administrator.**
  An administrator holds `cases:view-all` and *could* open any case; notifying
  all of them about every event on the platform would be authorized and would
  also be noise. The **actor is excluded** for the same reason: the confirmation
  somebody needs is the response to the request they made.
- **Duplicates are prevented by two mechanisms covering different halves**, the
  shape `10-document-indexing.md` established. A unique index on
  `(recipient_id, event_id)` makes "one dispatched event, one notification per
  person" an invariant that cannot suppress a genuine repeat, because an event's
  identity is assigned once and never reused. A hashed `dedupe_key` matched
  inside `NOTIFICATION_DEDUPE_WINDOW_SECONDS` catches a retried worker or a
  double-click — a *window* rather than a constraint, because a case genuinely
  updated twice in a week is two notifications.
- **Preferences are one row per `(user, key)`, not a column per preference**, and
  that shape is what "prepare for future delivery channels" means concretely: an
  eighth preference is a row with no migration, and a delivery channel is one
  boolean beside `in_app` — a claim that has since been cashed **twice**, by
  `email` and by `whatsapp`. All seven default to **on**, and a row is
  written only when somebody changes something — so `architecture.md` invariant 3
  holds for an account that has never opened the settings page, and a future
  change to the defaults reaches every untouched account without a backfill.
- **Failures are isolated by a thread, not by discipline.** `handle()` is a queue
  put and a return, so resolving recipients and inserting a batch never runs on
  the request that published the event — there is no longer a call stack
  connecting them, which is *"notification failures should never affect business
  operations"* made structural. Two failure paths deliberately **admit** rather
  than exclude: a preference lookup that could not run is not evidence somebody
  asked not to be told, and a duplicate is a smaller harm than a missed hearing.
- **System announcements are the one path a person creates a notification on**,
  and the reason is structural: `core/events.py` defines no broadcast scope,
  because every event the platform carries is about something somebody owns and a
  scope with no owner is a scope with no authorization rule. So an announcement
  enters through the Notification Service's own API (`notifications:manage`) —
  the service creating a notification, with no business module involved — and
  travels the ordinary path from there.
- **Two authorization questions with two different answers**, the shape Reports
  established. *May this caller use notifications at all* is `notifications:view`,
  in `BASE_PERMISSIONS` because a role without it would be told nothing while
  every other role was. *Whose notification is this* is answered by the
  repository — every read is keyed by recipient — and refused with **404**,
  because confirming that another person's notification exists is itself the
  disclosure. There is deliberately no `notifications:view-all` and no
  `notification_access.py`. `notifications:monitor` is the one new permission and
  is administrative like every other `*:monitor`.
- **Metrics come from two places on purpose**, exactly as the assistant's do.
  Row counts — stored, unread, recipients, by category — are **SQL aggregates**,
  because counting them in a process would reset on restart *and* be wrong across
  instances. Created, delivered, failed, suppressed, deduplicated, dropped, and
  latency accumulate **in the process** behind `NotificationMetricsRecorder`,
  with `since` reporting the window. *Delivered* means "published onto the
  channel", never "arrived in a browser" — the second would measure a client's
  network.
- **Nothing depends on it, in either direction.** Every business module works
  with `NOTIFICATIONS_ENABLED=false`, because none of them knows the feature
  exists; and the badge polls on a slow interval regardless of the WebSocket
  channel, so a deployment with `REALTIME_ENABLED=false` still tells a lawyer
  they were assigned a case.
- **No notification content, recipient name, or case number reaches a log or a
  metric.** The logs carry rule keys, categories, priorities, counts, and
  identifiers only; the metrics are counted **by rule**, which is a throughput
  figure, where counting by recipient would be a live index of who is being told
  what.

### Email Delivery Channel

Implemented per `context/feature-specs/17-email-delivery-channel.md`. Not a stage
of the AI pipeline, not a consumer of the event dispatcher, and not a feature that
decides anything about the platform's behaviour: it is the **second delivery
channel** for notifications that already exist, and the first thing to arrive
since Notifications shipped that adds *no* business rule at all.

- **It consumes notifications, never events, and that is structural.** The spec
  is explicit — *"the Email Delivery Channel should never receive domain events
  directly"* — and the boundary is a dependency rather than a discipline:
  `EmailDeliveryService` implements the one-method `NotificationDispatcher`
  protocol and is handed rows that have **already been created, authorized,
  de-duplicated, and persisted** by the Notification Service. It holds no event
  publisher, no dispatcher, and no business service, so there is no path from it
  to a business event. Everything it does from there only *narrows*: an email
  goes to a subset of the people the platform already decided to tell, about a
  subset of the things it already decided to say. **It therefore cannot widen
  visibility even if it were wrong**, which is what the spec's *"trust the
  Notification Service and never attempt to broaden notification visibility"*
  asks for.
- **Adding the channel took one protocol and one list**, in the shape
  `services/events.py` predicted for a second consumer and
  `models/notification.py` predicted for a second channel. No business module
  changed, no event was defined, and the preference model grew **one column** —
  no new table, no new key, no backfill, and an account that has never opened the
  settings page still has no row and still follows the platform defaults.
- **`EMAIL_RULES` is the whole of "marked for email delivery"**, keyed by
  *notification rule* rather than by event, because an event is not something
  this module can name. Its seven entries are exactly the spec's "Supported Email
  Types"; everything on the *"must not generate emails"* list is absent, and its
  absence is asserted by a test rather than left to review. Adding an email type
  is one entry — which is what *"support future email types without redesign"*
  means concretely. **"Password Changed" is the one supported type with nothing
  behind it**: `AuthService.change_password` publishes no event, so there is no
  notification to deliver, and creating one would be *notification policy*, which
  this spec's Out of Scope forbids this feature from touching.
- **The wording is not restated.** The subject is the notification's rendered
  title and the body's lead is its rendered message, both from
  `core/notifications.render_notification` in the recipient's language — which is
  exactly what `models/notification.py` chose to store no prose *for*, and what
  makes `code-standards.md`'s *"notification logic must never be duplicated"* hold
  across a channel that did not exist when that rule was written. The chrome
  around it (greeting, button label, footer) lives in `core/email.py` beside the
  rest of the platform's text, so the `.j2` files carry no sentence of their own
  and no `{% if language %}` branch.
- **The delivery is a persisted run**, so this feature has a table where
  Real-Time Synchronization has none: `pending` → `sending` → `sent` | `failed`,
  with the claim a **conditional `UPDATE`** rather than a lock — the mechanism
  `ocr_results` and `document_indexes` use, and the reason two workers handed the
  same job cannot both send the message. A unique index on `notification_id` makes
  *one notification, one email* an invariant rather than a heuristic, so retrying
  is re-using the row.
- **Retry is a schedule on the row, not a sleep on a thread.** A transient
  failure writes `next_attempt_at` and returns the worker immediately;
  `EmailRetrySweeper` re-queues what is due and reclaims anything stranded in
  `sending`. The transient/permanent split is a partition of a closed vocabulary
  (`TRANSIENT_FAILURE_CODES`) rather than a judgement at a call site: a timeout, a
  dropped connection, and an SMTP **4xx** are retried; a rejected credential, an
  unknown mailbox, an oversized message, and a broken template are not — retrying
  a 5xx is how a platform gets a relay to stop accepting its mail, and retrying a
  rejected password is how an account gets locked.
- **A deployment with no relay queues nothing**, rather than accumulating
  `pending` rows whose only outcome is a burst of very old mail the day somebody
  configures SMTP — including "your case was assigned" for a case that closed
  weeks ago. `provider_available: false` says so on the metrics endpoint, the
  same posture a missing Tesseract and a missing `LLM_API_KEY` take.
- **Off by default**, and it was the first feature switch on the platform to be
  (WhatsApp is the second). The others default to on because the worst case of an
  unconfigured one is a recorded failure nobody outside the platform sees; email
  is an *outward-facing* side effect, and a deployment that has not chosen a
  relay, a from-address, and a base URL should not be mailing real people the
  first time somebody is assigned a case.
- **Preferences are per key *and* per channel**, which is what the spec's User
  Preferences section actually asks for: a lawyer silences *email for hearing
  updates* without emptying their in-app feed. A change carries only the channels
  it is changing, so a client written before this channel existed cannot switch it
  off by omission — and the same protection is inherited by whichever channel
  arrives next.
- **The one place on the platform that builds a URL from a notification target.**
  `16-notifications.md` requires navigation to stay independent of frontend
  routing, and the in-app feed honours that literally by naming a resource; an
  email has **no client** to resolve one, so `TARGET_PATHS` turns the pair into an
  address. With no base URL configured the mail is **linkless but correct**,
  rather than carrying a broken path with no host.
- **Two Jinja environments, and the reason is a real attack surface.**
  `POST /notifications/announcements` puts a human's words into `{message}`, which
  reaches both bodies — so the HTML part autoescapes and the plain-text part does
  not. A header value carrying a line break is **refused rather than stripped**,
  because a name silently rewritten is a name that was attacked and nobody
  noticed.
- **No email content, and no address, reaches a log.** This module holds itself
  to a stricter line than any other, because it handles the one thing none of the
  others does: a personal address. The logs carry delivery identifiers, rule keys,
  statuses, failure codes, attempt counts, and durations — never a subject, never
  a body, never a case number, and never an address, not even at debug and not
  even hashed, since an address hashed unsalted is reversible by anyone holding a
  user list. The provider's own message never leaves `services/email_provider.py`
  for the same reason: an SMTP rejection quotes the envelope.
- **Metrics come from two places on purpose**, exactly as Notifications' do.
  Queued, sending, sent, failed, recipients, and attempts are **SQL aggregates**,
  because "how many emails are stuck?" is the first question after a restart and a
  process-local count would answer it wrongly. Retries, latency, and skips
  accumulate in the process with a `since`. *Sent* means "a provider accepted it",
  never "it arrived" and certainly not "it was read" — SMTP hands off to a relay,
  and a column called `delivered_at` would be a claim the platform cannot support.
- **No new permission, and no endpoint that lists deliveries.** Email is a
  delivery channel for notifications rather than a feature of its own, so its
  monitoring view is gated on the existing `notifications:monitor` — a separate
  `email:monitor` would be a second grant meaning the same thing. And a *list* of
  deliveries names a person, a rule, a moment, and an address, which is precisely
  the live index `services/notification_metrics.py` refuses to build; the history
  lives in the table for an operator to query under the database's own controls.
- **Nothing depends on it, in either direction.** Every notification is created,
  delivered in-app, and readable with `EMAIL_ENABLED=false`, because the
  Notification Service holds a one-method protocol and cannot ask a channel
  anything — not even whether it succeeded.

### WhatsApp Delivery Channel

Implemented per `context/feature-specs/18-whatsapp-delivery-channel.md`. The
**third delivery channel**, and the first feature on this platform whose most
interesting property is how *little* of it is new: it is the email channel's shape
applied to a different medium, and everything that is genuinely different is a
property of the medium rather than a decision this feature got to make.

**What it reuses unchanged**, and therefore does not re-argue here — read the
Email Delivery Channel section above for the reasoning, all of which applies:

- it **consumes notifications, never events**, by implementing the same
  one-method `NotificationDispatcher` and holding no event publisher and no
  business service, so it can only ever *narrow*. That is also the whole of the
  spec's Authorization section, inherited rather than re-implemented — which is
  why there is no `whatsapp_access.py`;
- `WHATSAPP_RULES` is the whole of "marked for WhatsApp delivery", keyed by
  *notification rule* rather than by event, with the *"must not generate"* list
  absent and its absence asserted by a test;
- the wording comes from `core/notifications.render_notification`, so three
  channels cannot drift;
- the delivery is a **persisted run** with a conditional-`UPDATE` claim, a unique
  index on `notification_id`, a retry schedule on the row swept by a timer thread,
  and a transient/permanent split that is a partition of a closed vocabulary;
- preferences are per key *and* per channel, and adding this one cost **one
  boolean column**;
- a deployment with no provider queues nothing, the channel is **off by default**,
  metrics come from two places, no new permission was added, and there is no
  endpoint that lists deliveries.

**What is genuinely its own**, all four of them forced by the medium:

- **The provider is HTTP, so the protocol is a different one.** `EmailProvider`
  and `WhatsAppProvider` are separate protocols rather than one generalized
  interface, deliberately: an SMTP send takes an addressed document and a Cloud
  API send takes a template name, a language tag, and an ordered parameter list.
  A common supertype would have to be the union of the two, which is a type that
  describes neither. `services/whatsapp_provider.py` adds **no dependency** —
  `urllib.request` sends the one JSON `POST` involved — and translates every
  failure into a `WhatsAppFailureCode`, with Meta's **numeric error code** checked
  before the HTTP status because a `400` carrying `132001` (unapproved template)
  and a `400` carrying `131009` (bad parameter) send an operator to two different
  consoles.
- **The template lives on Meta's side, so what ships here is a descriptor.** A
  WhatsApp template is submitted, reviewed, and approved in a console this
  repository cannot read, and the platform supplies its *parameters*. The
  tempting design — put the sentences in the approved template and send a case
  number — puts the platform's wording somewhere no test asserts on and nothing
  keeps in step with the in-app feed. So the approved template is a **thin
  envelope** (a greeting slot, a heading slot, a body slot), the sentences stay in
  `core/notifications.py`, and `apps/api/whatsapp/*.params.j2` says which values
  fill which slot. **A descriptor's parameter count and order are the contract
  with the registered template**, which is why they are a reviewable file rather
  than a list built in Python — and why `template_rejected` is its own failure
  code: it is the one failure fixed in Business Manager rather than in this
  repository.
- **The recipient is optional, and that changes what "skipped" means.**
  `users.phone` is nullable and most accounts are created without one, so
  `no_phone_number` is the *expected* outcome for much of the platform rather than
  a rare fault — a deployment watching it sit at the size of its user base is being
  told to collect phone numbers, not that something is broken.
  `normalize_phone` refuses what it cannot be sure about rather than guessing: a
  nationally-formatted number is messaged only when `WHATSAPP_DEFAULT_COUNTRY_CODE`
  is set, because one message not sent is a failure this channel is allowed to
  have and a legal notification delivered to a stranger is not. No phone-number
  library was added; that is stated as the trade it is.
- **"Delivered" is the spec's word and the platform is honest about it.** The
  status means *the provider accepted the message and issued a `wamid`* — the same
  claim `sent` makes one channel over. WhatsApp does publish real sent/delivered/read
  receipts, on an **inbound webhook** that is a public endpoint, a signature scheme,
  and an inbound message surface the spec does not ask for. `provider_message_id`
  is recorded on every row so that feature has something to correlate on.

Two smaller notes worth keeping. **Rate limits are prepared for rather than
modelled**: WhatsApp limits per business number and per recipient pair, and the
preparation is a small bounded pool, five of Meta's codes plus HTTP `429`
classified as `throttled`, and an exponential backoff whose ceiling is *half* the
email channel's — because this is the urgent channel, and a hearing update that
arrives after the hearing is worse than one that never arrives, since the reader
will act on it. And **`sanitize_parameter` collapses whitespace rather than
refusing it**, which is the opposite of what `sanitize_header_value` does with a
line break: a newline in a mail header is an injection, while a newline in a
template parameter is a formatting rule of Meta's, and an administrator who
pressed Enter in an announcement has not attacked anything.

### Dashboard & Analytics

Implemented per `context/feature-specs/19-dashboard-analytics.md`. The platform's
landing page, and the **first feature that reads across every module rather than
owning one** — which is what makes almost every decision in it a decision about
boundaries rather than about charts.

**It stores nothing.** There is no dashboard table, no saved layout, no
materialized metric, and no migration. A dashboard is a *read*, assembled per
request from rows the other modules own, and the entire feature is
`core/dashboard.py`, `repositories/dashboard.py`, `services/dashboard*.py`,
`schemas/dashboard.py`, and `api/v1/dashboard/`. It also took **no new event, no
new worker, no new queue, no new provider, and exactly one new permission**
(`dashboard:monitor`, for its own metrics view, like every other `*:monitor`).
There is deliberately **no `dashboard:view`**: every authenticated role has a
dashboard, so a permission on the page would be one every role holds.

**A widget is a function of a context, registered by key**, and the spec's
requirements follow from that shape rather than being implemented on top of it:

```text
WidgetKey ──▶ loader(WidgetContext) ──▶ WidgetPayload
                    │
                    └── case scope + owner id + window + limits,
                        resolved ONCE per request by DashboardAccessPolicy
```

- **Independence** — a loader is handed a context and returns a payload. It
  cannot reach another widget's result because it is never given one, and it
  cannot know it is on a page.
- **Authorization** — the context carries the *answers* the access policy already
  computed; a loader has no `User`, no permission set, and no way to widen a
  scope. The permission check happens **before** the loader runs, so an
  unauthorized widget is not computed and filtered — it is never computed.
- **Independent failure** — each loader runs inside its own `try`. A failure marks
  that widget `unavailable` with a code, is counted, is logged with a traceback
  server-side, and the page continues. *"One failing widget must not prevent the
  dashboard from loading"* is therefore the only behaviour the loop can have.
- **Timeouts** — widgets are loaded against a wall-clock budget checked between
  them. Once it is spent the rest come back `budget_exhausted` rather than being
  attempted, so a slow dashboard degrades into a partial one. Deliberately a
  budget rather than a per-query timeout: cancelling a statement differs between
  PostgreSQL and SQLite, and a rule enforced identically everywhere is worth more.
- **Independent refresh** — `GET /dashboard/widgets/{key}` calls the *same*
  loader as `GET /dashboard`, so a refreshed tile cannot drift from the one
  rendered beside it.

**Adding a widget is one entry in `WIDGETS` and one loader.** Nothing in the
router, the schemas, the access policy, or `apps/web` changes — and that last
part is the design's most load-bearing consequence. A widget declares **the domain
events that make it stale**, and the API serves that list to the client, so the
browser has no widget table of its own: it refreshes exactly what an event
touched. A widget added on the server starts updating live in a browser nobody
redeployed. This is the one place `lib/realtime/sync.ts` is deliberately *not*
the single source of staleness, and the reason is recorded in that file.

**Authorization is delegated, never re-derived**, and this is the feature's
sharpest risk: a dashboard shows a *number*, so a wrong scope here would be
invisible in a way a wrong document read never is. `services/dashboard_access.py`
therefore owns **no rules at all**. It asks `CaseAccessPolicy.visibility_scope`
for the case scope — the same call the case list, the document list, the
timeline, and semantic search make — and uses identity equality for the private
histories (reports, conversations, notifications), which is the rule those
repositories already enforce in every `WHERE` clause. Every query in
`repositories/dashboard.py` takes `visible_to` with **no default**, and the
predicate is `repositories.case.assigned_case_scope` rather than a second copy.
The notifications widget goes further and reads through
`NotificationRepository` itself, because every read there is keyed by recipient
and a second query would be a second place to get that wrong.

Three consequences worth stating, because each was a decision:

- **An aggregate widget requires *all* its capabilities, not any.**
  `document_analytics` reports uploads, extraction, and indexing; offered to
  somebody holding one of the three it would report the other two as zero — and a
  zero is information, which is what *"aggregated metrics must never leak
  unauthorized information"* forbids.
- **"My cases" means assigned to me, even for an administrator.** `cases:view-all`
  decides what `recent_cases` counts; it must not decide what *"what requires my
  attention?"* answers, or an administrator's dashboard becomes the whole caseload.
- **The platform-wide cache is safe by construction rather than by care.** Only
  widgets declared `platform_wide` are eligible, because those are the only ones
  whose answer does not depend on who asked. Nothing user-scoped has a cache key,
  and no setting can give it one.

**Analytics are descriptive and real.** Every figure is a `COUNT`, a `SUM`, or a
bounded `SELECT` over rows that exist. Where there is no data the answer is a
measured zero; where an average has no observations it is `null`, which the client
renders as an em dash rather than as `0`. There are no trends, no projections, no
smoothed series, and nowhere for one to come from — the spec's "Analytics Data
Integrity" section made structural. Queue depths come from **persisted lifecycle
rows** rather than from the in-process thread pools, for the same reason: a pool's
depth is one API instance's opinion and resets on deploy, while a `pending` row is
the platform's own record of work it owes.

**Nine payload shapes serve nineteen widgets**, discriminated on `kind`, so the
frontend has a renderer per shape rather than per widget — which is what makes the
twentieth widget free in the browser as well as on the server. Labels are **never**
in a response: a widget, a metric, and a bucket each carry a stable `key`, and the
words live in `components/dashboard/labels.ts`, because an API response is a place
a translation cannot live.

### Settings

Implemented per `context/feature-specs/20-settings.md`. The platform's
configuration in one place, and the first feature whose defining property is how
much of it deliberately **belongs to somebody else**.

**Its governing rule is the spec's own**: *"each feature should own its
configuration; the Settings module simply presents and manages those
configurations through a unified interface."* Taken literally, that decides
almost everything about the module:

- **Profile** writes the `users` row through `UserRepository` — the same
  repository User Management uses, and deliberately **not**
  `UserService.update_user`. That method is an administrator editing *somebody
  else's* account: it takes a user id, checks `users:update`, can change a role,
  a status, and an email, and publishes an event about a third party. None of
  that is a person editing their own name, and reusing it would have meant either
  widening its authorization or handing this service a capability it must never
  have. What is reused is the layer below, where reuse is safe. One new column,
  `users.job_title` — the spec names it and nothing had it.
- **Account & Security** is delegated to `AuthService` whole. The Settings
  service verifies no password, mints no token, and touches `session_generation`
  never; it calls one method and records that it happened. The spec's Password
  Change Policy — clear `must_change_password`, invalidate every other session,
  keep this device signed in — has held since Authentication shipped and is not
  re-implemented.
- **Notifications** and **Communication** are **two projections of one stored
  thing**, `notification_preferences`, read and written through
  `/notifications/preferences`. The split is a UI axis — *what you are told
  about* (the keys) versus *how it reaches you* (the channels) — and the Settings
  module stores nothing for either. There is no `/settings/notifications`, and a
  test asserts there is no route under `/settings` naming a notification: a
  second endpoint serving one stored thing is how two answers to one question
  start to disagree.
- **Appearance, Language & Region, AI, and Dashboard** are what no feature owned,
  so this module owns them — and that is the whole of what it owns.

**The storage shape is `notification_preferences`' own, reused rather than
reinvented**: one row per `(user, key)` with an open registry in
`core/settings.py`, so a tenth setting is one entry and **no migration**, and an
account that has never opened the page has **no rows** and follows the platform
defaults. That is what makes the spec's *"support future sections without
redesign"* concrete rather than aspirational, and it is why the migration seeds
nothing — a seeded default row says exactly what its absence already says, and
would then need a data migration every time a default changed.

**Administrator settings are isolated three times over**, which is the spec's
*"administrator settings should remain isolated from regular user settings"* made
structural: a separate table with **no `user_id` column at all**, a separate
registry sharing no key with the user one (asserted by a test), and
`settings:manage`, which is not a wider form of `settings:update`. Every
`default_*` platform setting is the fallback an account with no stored row
follows, which is what makes them *do* something rather than merely be stored:
changing one reaches every such account at once, with no backfill, because there
is nothing stored to back-fill.

**Validation happens to the whole batch before anything is written.** A save with
one bad time zone leaves every other field exactly as it was — enforced by
ordering rather than by a transaction alone, since a rejected entry means no
statement was issued for any of them. The same request also reports *every*
offending key rather than the first, so a form can mark all its bad fields at
once. And a value equal to what is already stored produces **no write at all**,
which is the spec's *"minimize unnecessary updates"* and keeps `updated_at`
meaning *when this setting last changed* rather than *when somebody last opened
the page*.

**Active sessions are the one genuinely new capability**, and they required a
token claim. JWTs are stateless here, so the platform could *revoke* every
session and could not *name* one. A `sid` claim — minted at sign-in, preserved
across every refresh rotation, granting nothing — plus a Redis-backed
`SessionRegistry` makes the list real. Three properties are worth stating because
each was a decision: it is keyed by **sign-in** rather than by credential (a
`jti` rotates every fifteen minutes, so a `jti`-keyed list would show one laptop
as dozens of devices by evening); it is a **view, never a boundary**, so it fails
**soft** where `TokenRevocationStore` fails closed — an unreachable Redis costs
the list and never a session that should have ended; and the API distinguishes
*"you have one session"* from *"the registry is unavailable"*, because an empty
array would conflate two states that deserve different sentences on screen.

**Maintenance mode announces; it does not close the platform.** The switch is
administrative and the *announcement* is readable by every authenticated caller —
a maintenance notice only administrators can see is a notice nobody needed, which
is the same shape a system announcement has. Refusing traffic would be a
platform-wide behaviour change this spec does not describe, and it is recorded as
an open question in `progress-tracker.md` rather than assumed. A message typed
with the mode still off is a **draft** and is not served.

**The page is server-described, in the shape the dashboard's widget catalog
established.** The API returns an ordered list of section descriptors *and* a
definition per setting — its value type, its permitted identifiers, its bounds —
so the browser renders a control for a setting it has never heard of and a tenth
section reaches a client nobody redeployed. Labels are **never** in a response:
every section, setting, and permitted value carries a stable key, and the words
live in `components/settings/labels.ts`, for the reason an API response is a
place a translation cannot live. An **administrative** section is *omitted*
rather than served disabled, because showing it would tell every lawyer which
platform settings exist and that somebody else controls them.

**Appearance is where the design system finally grew its second half.**
`00-design-system.md` shipped the platform dark-only while `ui-context.md` had
always said it supports both modes with dark as the default; this feature is what
closed the gap, so `globals.css` now carries a light palette beside the dark one
and the root layout no longer forces a theme. The two palettes are declared as
**platform tokens only** — the shadcn/ui mapping is written once in terms of
them, because `.dark` lands on the same element `:root` matches — and one new
token, `--text-on-accent`, is what a two-theme palette needs and a dark-only one
could do without.

**Nothing depends on it, in either direction.** No business module imports the
settings service, it holds no event publisher (changing your own theme is not
news anybody else is entitled to), and every feature works exactly as before for
an account that never opens the page — because "no stored row" is the platform's
own default everywhere.

### Localization

Implemented per `context/feature-specs/21-localization.md`. Not a stage of the AI
pipeline, not a delivery channel, and not a view over other modules: it is the
**one feature whose entire output is words**, and its defining property is how
much of it turned out to be already built.

**Almost every surface was already language-parameterized, and none of them had a
source.** A notification has stored **no prose** since Notifications shipped and
renders per request; `core/email.py` has carried chrome per language since the
email channel; `apps/api/whatsapp/*.params.j2` carries a descriptor per language;
`core/reports.py` has held section titles per language; and the RAG pipeline, the
assistant, and the report agent have all taken a `language` on every request. What
none of them had was an answer to *whose* language — `EMAIL_DEFAULT_LANGUAGE` was
the whole of it for outbound mail, recorded as an open question in
`progress-tracker.md` since that channel shipped. **This feature is mostly that
one seam**, plus the interface catalogues that never existed.

- **One vocabulary, one resolver.** `core/localization.py` holds the languages,
  their directions, their formatting locales, and `resolve_language(*candidates)`
  — a candidate list walked in priority order, falling back to the application
  default. Every resolver on the platform is now a call to it
  (`resolve_notification_language`, `resolve_email_language`,
  `resolve_whatsapp_language`, `resolve_report_language`, and the last clause of
  `resolve_answer_language`), so *"failures should gracefully fall back to the
  default language"* is one function rather than six agreements that happen to
  hold today.
- **English is the shipped default, and it is configurable.**
  `21-localization.md` names English as the default where the platform had
  defaulted to French everywhere; `DEFAULT_LANGUAGE` is therefore a deployment
  setting rather than a literal, and `user_settings.language` /
  `platform_settings.default_language` both take *its* value as their own
  default. One consequence is recorded rather than hidden: the platform's
  last-resort language changed, which moved a handful of assertions and is
  visible to any deployment that had been relying on the French fallback.
- **`LanguageDirectory` is the seam, and it is one method wide.** A delivery
  channel is handed *"which language does this account read in?"* and cannot read
  a theme, a dashboard preference, or an AI setting — and cannot **write**,
  because the repository underneath it has no write method. That is the
  deliberate answer to the open question the email channel recorded: a worker
  gets a directory, never a settings repository.
- **`language_for` and `chosen_language_for` are different questions, and
  conflating them would have made language *detection* dead code.** A delivery
  channel must write in *some* language, so it asks for the resolved one. An AI
  surface has something better to fall back on than a deployment default — the
  question itself — so it asks what the person actually **chose**, and an account
  that has chosen nothing still gets an Arabic answer to an Arabic question. The
  distinction is asserted by a test, because nothing else would have noticed.
- **The language is snapshotted onto a delivery row before it is queued**, for the
  reason the address and the phone number are: a preference changed between a
  hearing update being queued and a relay accepting it would otherwise rewrite
  history, and on WhatsApp it would ask Meta for a template that was never
  submitted in that language.
- **The interface catalogues are static assets, one file per locale**, imported
  dynamically and cached per locale — so switching to Arabic downloads one chunk
  and switching back downloads nothing. They carry **1,449 keys each**, covering
  every feature module rather than only the shell: the `*_LABELS` maps that used
  to hold enum wording in `types/*.ts` are gone, every `*ErrorMessage` helper is a
  hook resolving a code to a key, and a Zod schema emits `vm(key, values)` rather
  than a sentence, because a module-level constant cannot read a setting. A test
  fails the build if one language gains a key another lacks. A non-default catalogue is **deep-merged
  onto English before it reaches the provider**, so a key translated in one
  language and not another resolves to a sentence rather than to a branch
  somebody has to remember to write. A key present in *no* catalogue renders a
  humanized form of its last segment, which is *"the application should never
  expose translation keys to users"* applied to the one place a key could have
  reached a screen.
- **RTL is one attribute and a rule about class names.** `LocaleProvider` writes
  `dir` and `lang` onto the document element; every component uses Tailwind's
  *logical* utilities (`ms-`/`me-`, `ps-`/`pe-`, `start-`/`end-`,
  `text-start`/`text-end`, `border-s`/`border-e`), which resolve against it.
  There is **no mirrored stylesheet**, because a second layout is a second thing
  to keep in step — and a test asserts on the shipped source that no component
  outside the generated `components/ui/` primitives names a physical edge.
  `globals.css` carries only what a logical property cannot express: an Arabic
  font stack, looser leading for a taller script, and the two things that are
  genuinely transforms (a chevron that means *forward*, and a progress bar's
  fill).
- **The browser's language is step 2 of the chain and is adopted *once*.** The
  spec qualifies it *"first login only"*, and `isDefault` on the served setting is
  what makes "first" precise — an account with no stored row has expressed no
  opinion. Adopting it **writes** it, so from the next visit it is an ordinary
  preference; without the write, somebody who deliberately chose the platform
  default would have it silently overridden by their browser on every load.
- **The language is stored twice, exactly as the theme is.** `localStorage` is
  what lets the *login screen* — which has no session and therefore no settings —
  render in the language somebody has been using for a year; the Settings API is
  the durable copy that follows them to a new laptop. The reconciliation is
  one-way: the server's answer wins once it arrives.
- **Two of the four monitoring figures are only observable in a browser**, which
  is the one genuinely new thing here. A catalogue is fetched there and a key is
  missed while React renders, so `POST /localization/report` accepts them — and
  what it accepts is the whole privacy story: **keys and catalogue names only**,
  with anything carrying whitespace discarded on both sides of the network,
  because the *text* a key renders to may name a case, a court, or a person.
  Nothing is stored; it is counted, exactly as a RAG run's latency is.
- **Metrics come from two places on purpose**, the shape Notifications, Email, and
  Settings established. Active languages, load failures, missing translations, and
  unsupported-locale requests accumulate in the process with a `since`; the
  language distribution and the count of accounts following the default are **SQL
  aggregates**. The distribution reports every supported language *including the
  ones nobody chose*, because a breakdown that omitted Arabic until somebody
  switched would hide the figure a deployment deciding whether to invest in
  Arabic actually needs.
- **Nothing here can affect a decision, and that is structural.**
  `core/localization.py` has no permission, no scope, no identifier, and no
  query; `services/localization.py` performs no authorization because the caller
  has already decided that this person is being told something. A language is
  chosen *after* every access decision has been made, so *"localization must
  never affect authorization, RBAC, routing, database schema, business rules, or
  workflow execution"* is a property of the dependency graph — asserted by an
  integration test that switches a lawyer to Arabic and checks that their case
  list is the same list.
- **Uploaded documents are never touched.** The spec is explicit, and it is true
  by omission: no module in this feature imports a document repository, an OCR
  result, a chunk, or a vector. Document translation, OCR translation, and
  machine translation remain out of scope and have nowhere in this feature to
  live.

### Monitoring & Observability

Implemented per `context/feature-specs/22-monitoring.md`. The three pillars —
logs, metrics, traces — plus health, readiness, error tracking, security
monitoring, background-job monitoring, and one operator's page. It is the
platform's **last** feature on `ai-workflow-rules.md`'s list and the only one
that is entirely cross-cutting.

- **The whole design follows from one sentence in the spec**: *"business modules
  should not contain monitoring-specific logic beyond emitting logs, metrics, and
  traces."* So every observation is taken at an edge the platform already had, and
  the count of business modules changed by this feature is **zero**:
  - the **HTTP middleware** (`core/middleware.py`) is the one HTTP observation
    point — correlation id, trace, log context, request counts, and latency,
    labelled by the **route template** rather than the path, because
    `/cases/9f2c…` would be one time series per case and a slow leak of which
    matters are being worked on;
  - the **exception handlers** (`core/exceptions.py`) are where *security
    monitoring lives*. Failed logins, invalid tokens, permission denials, and rate
    limits are already exceptions this module handles, so classifying them here is
    a table lookup over exceptions that were being raised anyway —
    `AuthService`, `AuthorizationService`, and every access policy are untouched;
  - the **SQLAlchemy engine** (`services/database_metrics.py`) times every
    statement, including the lazy loads, the flushes, and the ones background
    workers issue on their own threads — none of which a repository decorator
    would have reached. It records the **verb** and never the text;
  - the **lifespan** attaches all of it and logs *configuration loaded* as a list
    of which switches are on, never of what any of them is set to.
- **Nothing here may become a dependency of the platform**, which
  ``22-monitoring.md`` states twice. Every recorder is a protocol with a null
  implementation selected in `api/deps.py`, every observation site is inside a
  `try`, and the failure mode is therefore always *"an observation was not
  taken"* and never *"a request was not served"*. An integration test signs in and
  reads `/health` with `MONITORING_ENABLED=false` to keep it that way.
- **The Logging Policy is enforced by the pipeline, not by discipline.**
  `core.observability.redact_mapping` runs as the last structlog processor over
  **every** entry from every module — including the ones libraries emit — and
  replaces the value of any field whose *name* suggests a credential or document
  content. Name-based rather than value-based, deliberately: a value scrubber has
  to recognise a token, while a name scrubber refuses `password` before ever
  seeing what was in it. Passwords, tokens, API secrets, prompts, extracted text,
  and report bodies therefore cannot be logged by accident.
- **Security monitoring names nobody.** A client address is folded into a salted
  digest whose salt is random per process and never leaves it, and the only thing
  readable from the result is its **cardinality** — *"forty-one failures from
  three sources"* is available and *"forty-one failures from 203.0.113.7"* is
  not. That is the mechanism `services/dashboard_metrics.py` introduced for
  counting active users, applied where the privacy argument is stronger. There is
  no parameter for an account anywhere in `SecurityMonitor`.
- **Errors are grouped, and the grouping deliberately ignores the message.** A
  fingerprint is built from the exception's type and where it was raised, because
  a message usually carries the identifier of whatever was being worked on —
  fingerprinting on one would produce a group per request and answer none of the
  questions an operator has. No traceback is kept: it goes to the log, beside the
  request id and trace id that lead back to the group.
- **Tracing is W3C Trace Context, and that choice is the whole of "prepare for
  OpenTelemetry".** A trace id is 16 random bytes, a span id is 8, and they travel
  in a `traceparent` header that is *validated* before it reaches a log — an
  all-zero id, a wrong length, or a non-hex character starts a fresh trace instead
  of being echoed into an aggregator. An inbound header is trusted for correlation
  and for nothing else: it grants nothing and is never used to look anything up.
- **Health has two audiences and two shapes.** `/health` and `/ready` stay
  unauthenticated at the application root, because an orchestrator has no
  credentials; `GET /monitoring/health` is the operator's view of the same
  platform and carries the detail a public probe must not — which dependency
  failed and why, and which setting is missing where one is. **External services
  are probed from configuration alone**: a readiness endpoint that called a
  metered language model would spend a deployment's token budget on Kubernetes'
  health checking, and one that opened an SMTP connection every few seconds is how
  a relay starts greylisting the platform. Whether the relay is *up* is already
  answered by the delivery rows every send writes.
- **Required and optional dependencies are told apart, and it matters.** Qdrant
  being unreachable makes the platform **degraded**, not unhealthy: semantic
  search, the assistant, and report generation refuse while cases, documents,
  users, notifications, and the timeline are untouched. Marking it required would
  make a platform that is 80 % working report itself as down, and an orchestrator
  would take it out of rotation — the one response that helps nobody.
- **Queue depth is counted from persisted rows and liveness from the process**,
  which is `code-standards.md`'s *"count persisted state, not process state"* plus
  the one exception that proves it: *"is the OCR pool running?"* has no answer in
  the database, because a stopped pool and a busy one look identical there — and a
  queue with nothing draining it is the failure a depth chart takes hours to
  reveal.
- **Alerts are declared, evaluated, and delivered nowhere.** The spec puts
  delivery out of scope and asks only that the infrastructure be prepared;
  `ALERT_RULES` is that preparation, with thresholds in `core/config.py` rather
  than beside the rules because a five-percent error rate is alarming for one
  deployment and a quiet afternoon for another. A *rate* alert needs a floor of
  observations before it may fire: three requests of which one failed is a 33 %
  error rate and means nothing.
- **`GET /monitoring/export` renders Prometheus's text format**, and the exporter
  is a renderer over a snapshot rather than a client library — so a second
  exposition format is a second function and no change anywhere else. It converts
  milliseconds to seconds at that boundary, name and all, because every stock
  dashboard and alert expression assumes base units and exporting
  `..._milliseconds` produces charts wrong by three orders of magnitude in a way
  nobody notices until an alert does not fire.

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