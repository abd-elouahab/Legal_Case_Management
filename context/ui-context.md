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
- Lawyers
- Court Updates
- Reports
- Notifications
- AI Assistant
- Settings

The active page is clearly highlighted.

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

- PDF preview
- OCR text display
- AI-generated summaries
- Semantic search highlights
- Source references
- Version history
- Document metadata

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