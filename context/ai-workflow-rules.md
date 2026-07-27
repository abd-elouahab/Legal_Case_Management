# AI Workflow Rules

## Approach

Develop the platform incrementally using a **spec-driven and modular workflow**. Every implementation must align with the project documentation (`project-overview.md`, `architecture.md`, `ui-context.md`, and `code-standards.md`).

The project is a **collaborative AI-powered Legal Case Management Platform**, not simply an AI assistant. AI features must integrate seamlessly into the business workflows of administrators, lawyers, and court representatives.

Each implementation should deliver a complete, testable business capability while maintaining separation between business logic, AI services, notifications, and real-time collaboration.

---

## Scoping Rules

- Implement **one business workflow** at a time.
- Prefer small, verifiable increments over large implementations.
- Complete one module before moving to another.
- Keep frontend, backend, AI, notification, and infrastructure work independent whenever possible.
- Every completed feature must be functional from the user interface to the database.
- Every feature must preserve real-time synchronization between authorized users.
- Every user-facing feature must support both Arabic and French.

---

## Preferred Development Order

Implement the platform in the following order:

1. Authentication & Role-Based Access Control (Administrator, Lawyer, Court Representative)
2. User Management
3. Case Management
4. Lawyer Assignment
5. Document Management
6. OCR & Document Processing
7. AI Document Indexing
8. RAG Pipeline
9. AI Assistant
10. Reports
11. Real-Time Synchronization
12. Notifications (In-App)
13. Email Notifications
14. WhatsApp Notifications
15. Localization (Arabic & French)
16. Monitoring & Observability

Each step should be independently deployable and testable.

---

## Business Workflow Units

Examples of valid implementation units:

- User Authentication
- User Management
- Create Legal Case
- Assign Lawyer
- Upload Documents
- OCR Processing
- Document Indexing
- Court Update
- Hearing Management
- AI Question Answering
- AI Summarization
- AI Report Generation
- Notification Delivery
- WhatsApp Integration
- Email Integration
- Case Timeline
- Audit Logging
- Language Switching

Avoid implementing multiple unrelated workflows in the same iteration.

---

## When to Split Work

Split implementation whenever it combines:

- Frontend and unrelated backend features.
- Business logic and infrastructure changes.
- AI services and notification services.
- Multiple unrelated API endpoints.
- Different business domains (Cases, Users, Reports, Notifications, etc.).
- Features that cannot be tested independently.
- Large UI redesigns with backend changes.
- Real-time communication and AI implementation.

If a feature cannot be verified end-to-end quickly, divide it into smaller implementation units.

---

## Handling Missing Requirements

- Never invent business behavior.
- Always follow the project context files.
- Resolve ambiguities before implementation.
- Record unresolved questions in `progress-tracker.md`.
- If new business requirements appear, update the documentation before writing code.
- AI behavior must always remain consistent with documented business rules.

---

## AI Development Rules

- AI features are assistants, not decision-makers.
- AI responses involving legal documents must always use Retrieval-Augmented Generation (RAG).
- Never allow the LLM to answer without document retrieval when company knowledge is required.
- AI agents must remain independent and reusable.
- Every AI response should reference the source documents.
- AI services must support Arabic and French.
- Prompt templates must be version-controlled.
- AI models must remain replaceable through LiteLLM.

---

## Real-Time Collaboration Rules

- Every authorized participant must immediately see important case updates.
- WebSocket events should be event-driven.
- Synchronization should not depend on page refreshes.
- Every business event should update the shared case timeline.
- Real-time collaboration must remain independent from AI processing.

---

## Notification Rules

Every important business event should trigger notifications.

Supported notification channels:

- In-App
- Email
- WhatsApp

Notification events include:

- Case Assignment
- New Court Update
- Hearing Scheduled
- Hearing Reminder
- Document Uploaded
- AI Report Ready
- Deadline Reminder
- Case Closed

Notifications should execute asynchronously and remain persistent.

---

## Localization Rules

Every feature must support:

- French
- Arabic

Requirements:

- No hardcoded UI strings.
- Translation keys only.
- RTL support for Arabic.
- AI answers in the selected language.
- Locale-aware formatting for dates, numbers, and time.

Localization must be verified before considering a feature complete.

---

## Protected Files

Do not modify the following unless explicitly instructed:

- `components/ui/*` (generated shadcn/ui components)
- Third-party libraries
- Auto-generated migration files (except when required)
- Generated API clients

---

## Keeping Documentation in Sync

Whenever implementation changes occur, update the appropriate documentation:

- `project-overview.md`
  - Feature scope
  - Business workflows
  - User roles

- `architecture.md`
  - System architecture
  - Storage
  - Services
  - Real-time communication

- `ui-context.md`
  - Navigation
  - Dashboard
  - User experience
  - Localization

- `code-standards.md`
  - Coding conventions
  - Security
  - Notification rules
  - Localization rules

- `progress-tracker.md`
  - Current implementation status
  - Completed modules
  - Open questions

Documentation should evolve alongside the implementation.

---

## Before Moving to the Next Unit

Before starting a new implementation unit, verify that:

1. The current workflow functions end-to-end.
2. All architecture invariants remain satisfied.
3. Unit and integration tests pass.
4. Real-time synchronization works correctly.
5. Notifications are delivered successfully.
6. Role-Based Access Control (RBAC) is enforced.
7. AI functionality is validated (if applicable).
8. Arabic and French support has been verified.
9. Documentation has been updated.
10. `progress-tracker.md` reflects the latest implementation status.
11. The application builds successfully.
12. No critical issues remain unresolved.

Only after these conditions are met should the next implementation unit begin.

---

## Definition of Done

A feature is considered complete only when:

- Business logic is implemented.
- UI is complete.
- API is complete.
- Database changes are migrated.
- AI integration (if applicable) is functional.
- Real-time synchronization works.
- Notifications are delivered correctly.
- Arabic and French translations are complete.
- Unit and integration tests pass.
- Documentation is updated.
- The feature is validated end-to-end.