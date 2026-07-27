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
- `modules/documents` — Document upload, OCR, indexing, versioning, and secure storage.
- `modules/reports` — AI-generated reports, legal summaries, exports, and report history.
- `modules/notifications` — Real-time notifications, email notifications, WhatsApp alerts, and reminder scheduling.
- `modules/users` — Administrator, lawyer, and court representative management.
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
- Accounts are provisioned by administrators; there is **no self-registration**.

Authorization

- Role-Based Access Control (RBAC). **Not yet implemented** — spec `03`
  establishes identity only. `User.role` is persisted and exposed by
  `GET /api/v1/auth/me`, but no endpoint is role-gated yet.
- Users only access resources permitted by their role.
- Lawyers can only access assigned cases.
- Court representatives access only authorized cases.
- Every request is validated before accessing business resources.

---

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