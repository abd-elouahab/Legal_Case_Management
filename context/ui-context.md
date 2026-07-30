# UI Context

## Theme

The platform follows a modern enterprise SaaS design inspired by Linear, GitHub, Notion, and Microsoft Copilot. The interface is designed for productivity, clarity, and collaboration.

The application supports **both Dark Mode and Light Mode**, with Dark Mode as the default experience.

The interface is designed around three primary user roles:

- Administrator
- Lawyer
- Court Representative

The AI Assistant is integrated contextually throughout the application rather than existing as a separate page. Every workspace should feel collaborative, responsive, and focused on legal case management.

---

## Colors

All colors must be defined using CSS custom properties. Components must never use hardcoded color values.

| Role | CSS Variable | Value |
|------|--------------|-------|
| Page Background | `--bg-base` | `#0F172A` |
| Surface | `--bg-surface` | `#1E293B` |
| Card Background | `--bg-card` | `#1E293B` |
| Sidebar | `--bg-sidebar` | `#111827` |
| Primary Text | `--text-primary` | `#F8FAFC` |
| Secondary Text | `--text-secondary` | `#CBD5E1` |
| Muted Text | `--text-muted` | `#94A3B8` |
| Primary Accent | `--accent-primary` | `#3B82F6` |
| Secondary Accent | `--accent-secondary` | `#2563EB` |
| Border | `--border-default` | `#334155` |
| Success | `--state-success` | `#22C55E` |
| Warning | `--state-warning` | `#F59E0B` |
| Error | `--state-error` | `#EF4444` |
| Notification | `--state-info` | `#38BDF8` |

---

## Typography

| Role | Font | Variable |
|------|------|----------|
| UI Text | Geist Sans | `--font-sans` |
| Headings | Geist Sans | `--font-heading` |
| Code | Geist Mono | `--font-mono` |

Typography rules:

- Clear visual hierarchy.
- Large page titles.
- Medium section headings.
- Readable body text.
- Consistent spacing.
- Responsive typography.

---

## Border Radius

| Context | Class |
|---------|-------|
| Buttons | `rounded-md` |
| Inputs | `rounded-md` |
| Cards | `rounded-xl` |
| Panels | `rounded-xl` |
| Tables | `rounded-lg` |
| Dropdowns | `rounded-lg` |
| Dialogs | `rounded-2xl` |

---

## Component Library

The UI is built using:

- Tailwind CSS
- shadcn/ui
- Radix UI
- Lucide React

Reusable components are stored in:

```
components/ui/
```

Business-specific components are organized by feature:

```
components/auth/
components/cases/
components/documents/
components/reports/
components/notifications/
components/users/
components/ai/
```

---

## Layout Patterns

### Main Layout

```
┌─────────────────────────────────────────────┐
│ Top Navigation Bar                          │
├──────────────┬──────────────────────────────┤
│ Sidebar      │ Main Content                 │
│              │                              │
│              │                              │
│              │                              │
│              │                              │
├──────────────┴──────────────────────────────┤
│ AI Assistant (Contextual Panel / Drawer)    │
└─────────────────────────────────────────────┘
```

---

### Sidebar

The sidebar provides quick access to all platform modules.

Navigation:

- Dashboard
- Cases
- Documents
- Users
- Lawyers
- Court Updates
- Reports
- Notifications
- AI Assistant
- Settings

The active page is clearly highlighted.

**Users** is the administrator's account directory (User Management): it manages
accounts across all three roles — creating, editing, deactivating, and resetting
passwords. **Lawyers** is the case-facing view of lawyers and their assignments,
and belongs to Case Management. They are separate destinations because they
answer different questions ("who has an account?" versus "who is on this case?").

Each item declares its own permission requirement, so the sidebar shows only what
the current role may open. Items a user cannot reach are hidden, and a section
whose items are all hidden disappears with them.

---

### User Management

Administrator-only, gated on the `users:*` permissions.

The list page shows a searchable, filterable, sortable, paginated table:

- Avatar (initials when no profile image), full name, and phone
- Email
- Role and status, each as a labelled badge — never colour alone
- Last sign-in and created date
- A per-row actions menu: View, Edit, Reset Password, Activate / Deactivate

Search matches first name, last name, or email, case-insensitively. Role and
status filters combine with it. Columns sort in both directions.

Dialogs:

- **Add user** — personal details, contact, initial password, role, status, and
  an option to require a password change at first sign-in.
- **Edit user** — the same fields without the password; sends only what changed.
- **Deactivate** — a destructive confirmation that states plainly that the
  account is *kept, not deleted*, that the user is signed out everywhere, and
  that it can be reactivated.
- **Reset password** — confirms, then reveals the generated password once, with
  a copy control, because the server does not store it and cannot show it again.

States:

- Skeleton loader matching the table's column layout while the first page loads.
- Distinct empty states for "no users yet" (offering *Add user*) and "no results"
  (offering *Clear filters*).
- An error state with a retry.

An administrator is never offered actions on their own account that the API
refuses — deactivating themselves, or changing their own role or status.

Business-specific components for this area live in:

```
components/users/
```

---

### Case Management

Gated on the `cases:*` permissions. Every role reaches the pages; **which cases
they see is decided per case by the API**, so a lawyer's list contains only the
matters they are assigned to and the pagination totals count only those.

The list page shows a searchable, filterable, sortable, paginated table:

- Case number, title (linking to the case), and category
- Status and priority, each as a labelled badge — never colour alone
- Court, assigned lawyer, assigned court representative, filing date, next
  hearing, and last updated
- A per-row actions menu: View, Edit, Manage assignments, Archive / Restore

Search matches case number, title, description, or court name,
case-insensitively. Status, priority, assignee, court, and the filing- and
hearing-date ranges all combine with it. Case number, priority, filing date,
hearing date, and last updated sort in both directions. The two assignee filters
appear only for callers who may read the user directory.

Columns hide progressively on smaller screens — the assignees below `xl`, court
and dates below `lg` — so a phone still shows what identifies a case and whether
it needs attention.

The details page groups the record the way someone working a case reads it:
**General information**, **Assignment**, **Court information**, and **Audit
information**, followed by the case's **Documents** (the real list, scoped to
this case) and then dashed placeholder cards reserving the layout for Timeline,
Notes, AI Assistant, and Reports. Those cards say plainly that the module is not
built yet, so an empty card is never mistaken for a loading failure. Documents no
longer has a placeholder — the module shipped, and a placeholder beside a working
feature reads as a bug.

Dialogs:

- **New case** — details, category, status, priority, court, dates, and the two
  assignments. The case number field may be left empty, which asks the API to
  generate the next one in the series.
- **Edit case** — the same fields without the case number, which is immutable
  once filed; sends only what changed. The Status select offers only the moves
  the case can legally make, taken from the server's `allowed_transitions`.
- **Manage assignments** — assign, change, or remove the lawyer and the court
  representative. Its own dialog because assignment is a separate capability
  from editing the case.
- **Archive** — a destructive confirmation that states plainly that the case is
  *kept, not deleted*, stays searchable, and can be restored.

States:

- Skeleton loader matching the table's column layout while the first page loads.
- Distinct empty states for "no cases yet" (offering *New case*) and "no results"
  (offering *Clear filters*).
- An error state with a retry.

A user is never offered an action the API would refuse: the assignment fields are
hidden from a caller without `cases:assign`, and Archive and Restore each name
the permission its own request needs.

Business-specific components for this area live in:

```
components/cases/
```

---

### Document Management

Gated on the `documents:*` permissions. Every role reaches the page; **which
documents they see is decided per case by the API**, so a lawyer's list contains
only files on the matters they are assigned to and the pagination totals count
only those.

The list page shows a searchable, filterable, sortable, paginated table:

- File name (with a file-type icon and the description beneath it), the case it
  is filed under, category as a labelled badge — never colour alone — file size,
  version, uploader, and upload date
- A per-row actions menu: View details, Preview, Download, Replace, Delete

Search matches the original filename, the description, or a category name,
case-insensitively. Category, file type, uploader, and the upload-date range all
combine with it. File name, category, size, version, and upload date sort in both
directions. The uploader filter appears only for callers who may read the user
directory.

Columns hide progressively on smaller screens — the uploader and date below `xl`,
the case and version below `lg` — so a phone still shows what identifies a file.

**Preview is offered only when the server says the type can be rendered**
(`isPreviewable`), so the menu never contains an action the API answers with 415.
Files are fetched as blobs through an authenticated request and displayed from an
object URL: the access token is a header, not a cookie, so pointing an `<iframe>`
at the API would arrive anonymous. The preview frame is fully sandboxed.

Dialogs:

- **Upload** — file, case, category, and an optional description, with a real
  transmitted-byte progress bar. The file picker's `accept` mirrors the server's
  accepted types; a rejection the browser cannot foresee (a corrupted or renamed
  file) comes back from the server against the same field.
- **Document details** — metadata, the case, and the complete version history with
  a download for every version, current or not. Editing the category and
  description happens here, gated on `documents:update`; the binary is never
  touched.
- **Replace** — a new file. The copy states plainly that the previous version is
  *kept* and stays downloadable, because "replace" usually implies otherwise.
- **Delete** — a destructive confirmation that states plainly that the document is
  *kept, not destroyed*, along with every stored version.

States:

- Skeleton loader matching the table's column layout while the first page loads.
- Distinct empty states for "no documents yet" (offering *Upload*) and "no
  results" (offering *Clear filters*).
- An error state with a retry, an upload progress indicator, and per-action
  downloading states.

The same list is embedded in the case workspace, pinned to that case: the case
column disappears, the upload dialog pre-selects the case, and *Clear filters*
does not widen the view to the whole platform.

Business-specific components for this area live in:

```
components/documents/
```

---

### Authentication

Public authentication pages use a standalone centered layout — no sidebar or app
header — so they stay isolated from the protected shell.

Sign-in screen:

- Card containing the sign-in form.
- Email and password fields, with a show/hide password toggle.
- Inline field-level validation messages.
- A single alert region for sign-in failures (never revealing whether the email
  or the password was wrong).
- Submit button with an in-progress state that disables the form.

After too many consecutive failed attempts the account is temporarily locked. The
alert then shows the server's lockout message, which states how long to wait; the
client never invents its own countdown, since only the server knows the remaining
time.

Changing a password signs the user out of every other device. The confirmation
message must say so, so the user understands why their other sessions ended. The
device performing the change stays signed in.

Route behaviour:

- Unauthenticated users visiting a protected route are redirected to `/login`,
  and returned to their original destination after signing in.
- Authenticated users never see the login page; they are forwarded to the
  dashboard.
- `/` resolves to the dashboard when signed in and to `/login` otherwise.
- While a session is being restored after a page refresh, protected routes show a
  pending state rather than briefly redirecting.

Business-specific components for this area live in:

```
components/auth/
```

---

### Dashboard

The dashboard presents key information at a glance.

Widgets include:

- Active Cases
- Upcoming Hearings
- Assigned Lawyers
- Recent Court Updates
- Pending Notifications
- AI Activity
- Recently Modified Cases
- Case Status Overview

Charts:

- Cases by Status
- Monthly Case Activity
- Hearing Schedule
- Notification Statistics

---

### Case Workspace

Each case serves as a collaborative workspace containing:

- Case Information
- Assigned Lawyers
- Court Information
- Timeline
- Documents
- Reports
- Hearings
- Notifications
- Activity History
- AI Assistant

The AI Assistant can summarize documents, answer questions, and generate reports directly within the case workspace.

---

### Document Viewer

The document viewer supports:

- PDF preview — **implemented** (plus PNG, JPEG, and plain text; other types fall
  back to download)
- Document metadata — **implemented**
- Version history — **implemented**
- OCR text display — deferred to OCR & Document Processing
- AI-generated summaries — deferred to the AI Assistant
- Semantic search highlights — deferred to the RAG pipeline
- Source references — deferred to the AI Assistant

---

### Notifications Center

Notifications are displayed in real time.

Categories:

- Case Updates
- Court Decisions
- Hearing Reminders
- Document Uploads
- AI Report Completion
- System Alerts

Users can mark notifications as read or filter them by type.

---

### AI Assistant

The AI Assistant is available throughout the platform.

Capabilities include:

- Legal document Q&A
- Semantic search
- Summarization
- Information extraction
- Report generation
- Case timeline explanations

Every AI response includes references to the source documents.

---

### Collaboration

Case collaboration displays:

- Assigned lawyers
- Recent edits
- Court updates
- Shared documents
- Activity timeline

Real-time updates appear instantly without requiring page refreshes.

---

## Localization

The platform fully supports:

- French
- Arabic

Features:

- Instant language switching
- Right-to-Left (RTL) layout for Arabic
- Left-to-Right (LTR) layout for French
- Localized dates and times
- Localized numbers
- AI responses in the selected language

Language switching is available from the top navigation bar.

---

## Icons

Use **Lucide React** exclusively.

Guidelines:

- Stroke icons only.
- Consistent icon sizes.

| Context | Size |
|---------|------|
| Inline | `h-4 w-4` |
| Buttons | `h-5 w-5` |
| Navigation | `h-5 w-5` |
| Dashboard Cards | `h-6 w-6` |
| Empty States | `h-10 w-10` |

---

## Responsive Design

The platform is desktop-first while remaining fully responsive.

Breakpoints:

- Mobile
- Tablet
- Laptop
- Desktop
- Large Desktop

On smaller screens:

- Sidebar collapses into a drawer.
- AI Assistant becomes a slide-over panel.
- Tables become responsive cards.
- Dashboard widgets stack vertically.

---

## Accessibility

The interface should comply with WCAG AA guidelines.

Requirements:

- Keyboard navigation.
- Focus indicators.
- Screen reader compatibility.
- High contrast support.
- Accessible forms.
- Accessible tables.
- ARIA labels where appropriate.

---

## User Experience Principles

- Keep workflows simple and intuitive.
- Minimize clicks for common actions.
- Surface the most important case information first.
- Make collaboration effortless.
- Show real-time updates immediately.
- Ensure multilingual consistency.
- Integrate AI naturally into existing workflows rather than making it a separate experience.
- Prioritize readability, speed, and accessibility.