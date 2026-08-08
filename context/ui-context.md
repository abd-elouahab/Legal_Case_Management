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
components/realtime/
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
- Search
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
this case), its **Timeline** (the real activity history), its **Search** (pinned
to this case), its **AI Assistant** (also pinned), its **Reports** (also pinned,
and showing only the reports the reader generated), and then a dashed placeholder
card reserving the layout for Notes. That card says plainly that the module is
not built yet, so an empty card is never mistaken for a loading failure.
Documents, Timeline, the AI Assistant, and Reports no longer have placeholders —
all four modules shipped, and a placeholder beside a working feature reads as a
bug.

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

### Semantic Search

Gated on `search:query`, which every role holds. **Which passages a user sees is
decided per case by the API, inside the vector query itself**, so a lawyer
searches only the matters they are assigned to — and a caller assigned to nothing
receives an empty result set rather than the platform's corpus.

It has both a destination and an embedded form. `/search` searches everything the
caller can reach; the case workspace renders the same component pinned to one
case, where the case filter disappears and *Clear filters* does not widen the
search back to the platform — the same rule the embedded document list follows.

The screen is a query box, the metadata filters, and the ranked passages:

- **The search runs on submit, never as you type.** Every request costs a query
  embedding on the server, and a legal search is a question someone finished
  writing rather than a prefix they are still typing.
- **Filters** are case, category, file type, and language — the subset a person
  actually narrows by. Document, version, and the indexing-date range are
  supported by the API and reachable from the case-scoped panel and the client,
  but are given no control here: nobody searches "passages indexed between two
  dates", and a filter row nobody uses costs every user the time to read past it.
  The case select appears only for callers who may read the case list, since
  nobody else could resolve a name from an identifier.
- **A result is a passage, shown in full.** It is the evidence: truncating it
  would leave a lawyer unable to tell whether the clause they need is inside, and
  send them to open the document to find out — which is the work this feature
  exists to save. Beside it sits the complete citation (file name, page, passage
  number, version), the category and language as labelled badges, and relevance
  as a **percentage with a label, never colour alone**. `dir="auto"` lets an
  Arabic passage render right-to-left beside a French one.
- A result links to its **case**, not to a document viewer: the case is the one
  destination every result's reader is certainly entitled to open.
- **Paging re-runs the submitted query**, not whatever is currently in the box.
  Page numbers only, with no total — a similarity search has no cheap exact count,
  and the API reports `hasMore` rather than a figure it would have to guess at.

States:

- Skeleton cards while a search is in flight.
- Three distinct empty states, and none of them is an error: **"nothing searched
  yet"** (explaining what a result will look like), **"no matching passages"**
  (the corpus holds nothing near the query — an answer, which the API returns as
  a 200), and a **dependency outage** that names *which* dependency, because "the
  search index is unreachable" sends a user to an administrator while "search is
  unavailable" sends them to retry forever.
- A failed search is **not retried automatically**: a 503 means a dependency is
  down and retrying costs another query embedding to fail the same way.

`SearchMetricsPanel` sits on `/documents` beside the OCR and indexing panels,
gated on `search:monitor` — the third stage of the same pipeline, and the one
that says whether the first two are paying off.

Business-specific components for this area live in:

```
components/search/
```

---

### Timeline & Audit Trail

Gated on `timeline:view`, which every role holds. **Which timelines a user sees is
decided per case by the API**, so a lawyer reads the history of the matters they
are assigned to and is refused the rest with a 403 — never with an empty list,
which would make an inaccessible case indistinguishable from a quiet one.

It has no page of its own. A timeline belongs to a case, so it is rendered inside
the case workspace, in reverse chronological order. Each entry shows:

- An **event icon**, chosen by the event's category — folder for case events, file
  for documents, user for assignments, refresh for status, flag for priority. The
  category is computed by the API, so the icon cannot disagree with what happened.
- The **event title** ("Document Uploaded"), the **description** (the sentence the
  publishing module wrote, e.g. `Amina Benali uploaded "Contract.pdf"`), and a meta
  line carrying the **user**, their **role**, and the **timestamp**.

Timestamps read the way an activity feed does — *Today • 14:32*, *Yesterday •
09:15*, *24 July • 14:32*, and the year once it stops being obvious — with the
exact instant on the element's `title`, because precision is what matters in a
legal audit trail.

Search matches the event title or description, case-insensitively. Activity type,
actor, and the date range all combine with it, and the order flips between newest-
and oldest-first. The actor filter appears only for callers who may read the user
directory, since nobody else could resolve a name from an identifier.

The structured `metadata` an event carries is **never rendered raw**. It exists so
a future module can attach specifics without a schema change; everything a reader
needs is already in the description.

States:

- Skeleton loader matching the entry layout while the first page loads, and a
  spinner in the pagination controls while a later page is in flight — the
  previous page stays on screen, so without it pressing Next looks inert.
- Distinct empty states for "no activity yet" (explaining that entries appear
  automatically) and "no results" (offering *Clear filters*).
- An error state with a retry, which also carries the refusal message when the
  caller is not party to the case.

Business-specific components for this area live in:

```
components/timeline/
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
- AI-generated summaries — deferred to a later AI feature. The AI Assistant
  answers questions about a document from inside the case workspace; summarizing
  one *in the viewer* still needs a viewer-level action, and
  `13-ai-legal-assistant.md` puts summarization out of its scope
- Semantic search highlights — **partially delivered**: Semantic Search returns
  the matching passage verbatim with its page number, which is the citation a
  lawyer needs. Highlighting that passage *inside the rendered document* is still
  deferred, because it needs the viewer to map a chunk back to a position in the
  file rather than to a page.
- Source references — **delivered**, but by the AI Assistant rather than by the
  viewer: every answer carries its citations with the document, version, and
  page, and each links to the case. Rendering a reference *inside* the viewer
  waits on the same chunk-to-position mapping the highlight does

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

Gated on `ai:chat`, which administrators and lawyers hold and **court
representatives do not** — the one place this platform draws a line between
reading the case file and generating an interpretation of it. **Sending a
message additionally needs `ai:ask`**, because a message puts a question to the
RAG pipeline; a caller holding only `ai:chat` reads their history and is told
plainly that they cannot ask new questions, rather than meeting a 403 on submit.

It has both a destination and an embedded form. `/ai` holds conversations about
everything the caller can reach; the case workspace renders the same workspace
pinned to one case, where every answer is built only from that case's documents
and the conversation list shows only that matter's threads — the same rule the
embedded document list and case search follow.

The screen is the conversation list beside the open thread:

- **Which conversation is open is component state, not a route.** A conversation
  identifier in the URL would be written to the browser's history and the
  `Referer` header of anything the page loads next — the same three logs the API
  refuses to put a question into by making search and messaging POSTs.
- **The list shows the caller's own threads only**, most recently active first,
  with active and archived as two states rather than a filter bar. Search matches
  the title, because that is what the API searches; a box that appeared to search
  message contents and quietly did not would be worse than none.
- **A message appears the instant it is sent**, before the server has confirmed
  anything, and the pending turn is discarded the moment the stored transcript
  arrives — so an answer is never drawn twice.
- **The answer streams when the platform allows it**, and falls back to arriving
  whole when the provider cannot. Three states are shown while it is in flight,
  and they are the three the API actually reports: *searching your documents*,
  *read N passages*, and the text itself. A client renders the **final** event
  rather than the accumulated fragments, because a dangling citation marker has
  been removed from it and a refusal replaced by the platform's own sentence.
- **An answer with no supporting evidence says so prominently**, and a truncated
  one says that it stops early. A reader must never mistake "I found nothing" for
  an answer that happens to be short, and an answer cut off at the model's output
  ceiling is the one way this screen could actively mislead.
- **Citations are shown exactly as the pipeline produced them** — file name,
  version, page, and the marker the prose cites — with the excerpt collapsed
  rather than omitted, and relevance as a **percentage with a label, never colour
  alone**. A source the answer did not cite is listed and *marked*, because a
  model that forgot a marker has not made the evidence disappear. Each links to
  its **case**, the one destination its reader is certainly entitled to open.
- **Suggested follow-ups appear under the last answer only**, and choosing one
  fills the box rather than sending it: a suggestion is a starting point someone
  may want to narrow, and one click that silently spends a model call is not a
  shortcut anyone asked for.
- **Every answer can be rated helpful or not helpful, and copied.** Pressing the
  rating already given withdraws it. Rating never alters the answer — feedback is
  stored separately server-side, which is what the spec requires and what keeps
  the transcript usable as evaluation evidence.
- **The composer sends on Enter and breaks the line on Shift+Enter**, and is
  never disabled while an answer is in flight — only the send button is. Someone
  who thought of their next question while reading must be able to type it.
- **`dir="auto"` throughout**, so an Arabic answer renders right-to-left beside a
  French question without the client detecting script.
- **The answer is rendered as written, not as Markdown.** Interpreting generated
  text as markup would mean deciding what to do with a `[1]` citation marker, a
  `#` from a statute reference, or an underscore in a filename — and rendering
  generated text as HTML in a legal platform is a much larger decision than it
  looks.

States:

- Skeletons while a transcript loads, and a typing indicator while an answer is
  being produced.
- Distinct empty states for "no conversation open", "ask your first question",
  and "no conversations yet" — none of them an error.
- A failure keeps the question on screen with a **retry**, and is never retried
  automatically: a 503 means retrieval or the model is down and an immediate
  retry fails the same way, while a request that *did* reach the model would
  append a second answer.

`AssistantMetricsPanel` sits on `/ai`, gated on `ai:monitor` — the fifth stage of
the same pipeline, and the one that says whether the four below it are being used.

Business-specific components for this area live in:

```
components/ai/
```

---

### Reports

Gated on `reports:view`, which administrators and lawyers hold and **court
representatives do not** — the same line every other AI capability draws.
**Generating additionally needs `reports:generate` *and* `ai:generate-report`**,
so a caller holding only `reports:view` reads and exports the reports they
already have and is never offered a Generate control they would be refused.

It has both a destination and an embedded panel. `/reports` lists everything the
caller has generated; the case workspace renders the same list pinned to one
matter, where the case column disappears, *Generate* pre-selects the case, and
*Clear filters* does not widen the list back to the whole platform — the same
rule the embedded document list, case search, and the case assistant follow.

**The list is the caller's own, and that is not a filter.** The API scopes it by
requester, so a lawyer's history contains the reports *they* generated and the
totals count only those. There is deliberately no "generated by" control: a
filter naming a user would either be redundant or be a request the API must
refuse, and offering it would suggest the second is possible.

The screen is the filters, the history table, and the report dialog:

- **A row is what a report is and where its run got to**: title and type, status,
  progress, how many of its sections were grounded, and when it was requested.
  The **progress bar lives in the status column** rather than in one of its own —
  a permanently empty column for the ninety percent of rows that have finished
  would be a column of blanks.
- **A failed row says why in the table**, not only inside the report. A history
  where three rows read "Failed" and nothing more makes a user open three reports
  to learn they all failed for the same reason.
- **Progress counts sections, never seconds.** "Writing section 3 of 7" is a
  statement about the work; a time estimate would be a guess about a language
  model's latency, and a wrong one is worse than none. The denominator is
  published by the server when the run is queued, so the bar has a true scale
  from the first poll rather than being an indeterminate stripe — and a *queued*
  run says "waiting for a worker" rather than showing 0%, because zero on a bar
  reads as "started and got nowhere".
- **Which report is open is component state, not a route.** A report identifier
  in the URL would be written to the browser's history and to the `Referer`
  header of anything the page loads next — the same three logs the API refuses to
  put a question into by making search and messaging POSTs, and a report is a
  generated interpretation of a client's file.
- **The report reads as a document**: sections in template order, each with its
  heading and its prose, then the reference list, then the disclaimer. **Rendered
  as written, not as Markdown**, for the reason the assistant's answers are —
  interpreting generated text as markup would mean deciding what to do with a
  `[1]` citation marker, a `#` from a statute reference, and an underscore in a
  filename. Line breaks *are* preserved, because a model that wrote a chronology
  one item per line meant it.
- **A section the case file does not cover is shown and marked**, never hidden:
  hiding it would leave a reader to conclude the report forgot to mention the
  parties. A section cut off at the model's output ceiling is marked too — it is
  the one way this screen could actively mislead, because it reads as a complete
  finding that happens to be short.
- **Citations are the pipeline's, rendered by the assistant's own component.**
  File name, version, page, and the marker the prose cites, with the excerpt
  collapsed rather than omitted, each linking to its **case** — the one
  destination its reader is certainly entitled to open. Reusing `CitationList`
  rather than writing a second one is what keeps a citation looking identical
  wherever the platform shows one.
- **The disclaimer is the server's and travels with the report**, into every
  surface and every export: a document that looks like a lawyer's work product
  and is not must say so on its face.
- **Export offers only what this deployment can produce.** PDF and Markdown ship,
  and both work for Arabic as well as French — the exporter finds and verifies a
  font with Arabic coverage itself, so nothing has to be configured. A format
  whose rendering library is absent is not offered; on a host with no Arabic font
  at all an Arabic PDF is **refused with a message naming Markdown** rather than
  downloading a page of empty boxes. The button is *absent* before a report is
  ready rather than disabled — a greyed-out Download beside a progress bar reads
  as broken.
- **Regenerate is offered only on a finished run**, because the API answers 409
  for one already in flight, and only to a caller holding both permissions a
  generation needs.
- The **delete confirmation states plainly** that the report leaves the history
  and can no longer be opened or exported, that the record is *kept*, and that
  the case, its documents, and its timeline are untouched — which is the question
  anyone hesitating over that button is actually asking.

States:

- Skeleton loader matching the table's column layout while the first page loads.
- Distinct empty states for "no reports yet" (offering *Generate*) and "no
  results" (offering *Clear filters*).
- An error state with a retry, and — inside the dialog — four real states rather
  than variations on "loading": queued or generating (the progress bar, no empty
  headings), failed (the cause and Regenerate), ready (the document), and gone (a
  404, which the API returns for a deleted report and for somebody else's alike).

`ReportMetricsPanel` sits on `/reports`, gated on `reports:monitor` — the sixth
stage of the same pipeline. It is the only metrics panel on the platform with
**no "since" caveat**, because a report is a persisted run and every figure is an
exact SQL aggregate.

Business-specific components for this area live in:

```
components/reports/
```

---

### Live Updates

Gated on `realtime:connect`, which **every role holds** — and unusually for this
platform, the permission grants access to nothing on its own: every update is
delivered on a topic that is authorized per resource, so the socket a court
representative opens carries exactly the changes to the cases they could already
open.

**There is no page for this, and there is deliberately almost no UI.** The
feature's job is that screens stop being stale, and a synchronization feature the
user has to think about has failed at it. What it adds to the interface is one
indicator, and what it changes everywhere else is that lists, badges, progress
bars, and activity feeds catch up on their own.

- **The connection indicator lives in the top bar and renders nothing while
  updates are live** — which is nearly always. A permanently-green dot is
  furniture people learn to ignore, and the one place that would tell them
  something is wrong is the last place that should be ignorable. It appears in
  three states, each with an icon *and* a label because `ui-context.md` forbids
  colour alone: **Connecting**, **Reconnecting** (updates were interrupted; the
  page still works), and **Updates paused** (they are not coming back on their
  own — refresh to see other people's changes).
- **The wording is about the data, never the transport.** "Updates paused", not
  "WebSocket disconnected". A lawyer needs to know whether the case in front of
  them is current; the name of the protocol carrying it is not their concern, and
  naming it invites a support ticket instead of a refresh.
- **A retry appears only when retrying is meaningful.** The client backs off on
  its own and gives up after enough consecutive failures; *that* is the state a
  person can act on, so it is the only one with a button. Offering retry
  mid-reconnect would invite somebody to reset a backoff that is already working.
- **The case workspace subscribes once, to its case**, and that covers everything
  inside it: the case record, every document on it, their extraction and indexing
  progress, and the activity timeline. Subscribing per document would mean
  re-subscribing on every page of a document list for no additional access.
  **Reports are the exception and follow their own topic**, because a report is
  its author's private work product — the case's participants see from the
  timeline that one was produced; only its author watches it being written,
  section by section, as the progress bar moves between polls.
- **An event refreshes; it never patches.** A `case.status_changed` arrives
  carrying the new status and the client still refetches, because the cached case
  is an *authorized read* scoped to that caller while the event is a
  *notification* delivered to everyone following the case. Rendering data that
  never passed an authorization check is not a shortcut anyone asked for.
- **Everything degrades to what existed before.** Every list, pipeline, and report
  still polls. A deployment with live updates switched off, a failed connection, a
  browser that blocks WebSockets, and a refused subscription all leave the screen
  exactly as usable as it was — slower to notice a change, never wrong about one.

Business-specific components for this area live in:

```
components/realtime/
```

---

### Collaboration

Case collaboration displays:

- Assigned lawyers
- Recent edits
- Court updates
- Shared documents
- Activity timeline

Real-time updates appear instantly without requiring page refreshes — delivered
by the channel described under **Live Updates** above, with polling as the
fallback that keeps every one of these correct when it is unavailable.

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