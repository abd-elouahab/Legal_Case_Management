# Manual Test Checklist

A human pass over the whole platform, end to end. It exists because the automated
suites answer a different question: `apps/web` has 711 unit and integration tests
and the API has its own, but every one of them runs against a mock or a fixture.
Nothing in either suite has ever watched a real 25 MB scan travel through
Tesseract into Qdrant and come back as a cited answer, and nothing in either has
ever read an Arabic screen right-to-left.

**How to read it.** Sections 1–3 are setup and must pass before anything else is
meaningful. Sections 4–15 are the feature walkthroughs, in the order a case
actually moves through the platform, so running them top to bottom builds the
data the later ones need. Section 16 is the localization pass the recent change
calls for, and it is deliberately last: it re-walks screens the earlier sections
already proved *work*, asking only whether they can be **read**.

**Three roles, and you need all three.** A great many of the rules below are
about what a lawyer *cannot* see. Create the three accounts in section 2 and keep
three browser profiles (or one normal window and two private ones) signed in
side by side — several checks require two roles watching the same screen at once.

**Recording a result.** For each box: ✅ passed, ❌ failed (write what you saw and
what you expected), ⏭️ skipped (write why — an unconfigured provider is a
legitimate skip and is called out where it applies).

---

## 1 — Environment and services

- [ ] `docker compose up -d` starts PostgreSQL, Redis, MinIO, and Qdrant with no
      container in a restart loop (`docker compose ps` shows all four healthy).
- [ ] `.env` exists, copied from `.env.example`, and `LLM_API_KEY` is set to a
      working key. **Without it every AI section below fails identically**, which
      is the single most common cause of a confusing test run.
- [ ] Tesseract is installed and on `PATH` (`tesseract --version`), with the
      `fra` and `ara` language packs — `tesseract --list-langs` shows `eng`,
      `fra`, and `ara`. In Docker this comes with the API image.
- [ ] Poppler is installed (`pdftoppm -v`). Without it, PDF pages cannot be
      rendered for OCR and every scanned PDF fails at extraction.
- [ ] `alembic upgrade head` completes and reports no pending migrations.
- [ ] The API starts (`uvicorn main:app --reload` from `apps/api`) with no
      exception in the first ten seconds of log output.
- [ ] `GET /api/v1/health` returns `200` and every backing service reads
      `healthy`.
- [ ] `GET /api/v1/health/ready` returns `200`.
- [ ] The web app starts (`npm run dev` in `apps/web`) and `http://localhost:3000`
      redirects an anonymous visitor to `/login`.

## 2 — Accounts and first sign-in

- [ ] `python -m scripts.create_user --email <you> --name "<Your Name>" --role
      administrator` creates the first administrator. **Run it as a module from
      `apps/api`** — `python scripts/create_user.py` fails with
      `ModuleNotFoundError: No module named 'core'`.
- [ ] Signing in with the wrong password says *"Incorrect email or password"* and
      never reveals whether the address exists.
- [ ] Signing in with the right password lands on the dashboard.
- [ ] Six or more consecutive failures lock the account, the message says so, and
      the response carries `Retry-After`.
- [ ] From the administrator account, create a **lawyer** and a **court
      representative** through Users → Add user.
- [ ] Each new account can sign in, and is forced to change its password on first
      sign-in if *Require a password change* was ticked.
- [ ] Signing out returns to `/login`, and pressing Back does **not** restore the
      authenticated page.

## 3 — Authorization (run as each role)

- [ ] A **lawyer** sees no *Users* and no *Monitoring* item in the sidebar.
- [ ] A **court representative** sees no *AI Assistant* and no *Reports* item.
- [ ] Typing `/users` directly as a lawyer shows *Access denied*, not a blank page
      and not a crash.
- [ ] Typing `/monitoring` directly as a lawyer shows *Access denied*.
- [ ] A lawyer's case list contains **only** the cases they are assigned to, and
      the total count at the bottom matches what is listed — not the platform
      total.
- [ ] A court representative can edit a case's court name and next hearing date
      but **cannot** edit its title or description.
- [ ] The API refuses independently: with a lawyer signed in, calling
      `GET /api/v1/users` from the browser console returns `403`.

## 4 — Case management

- [ ] Create a case with only a title; the case number is generated as
      `CASE-YYYY-NNNN`.
- [ ] Create a second case supplying your own case number; it is stored uppercase
      and does not disturb the generated series.
- [ ] Creating a third case with a case number already in use is refused with a
      message naming the collision.
- [ ] A case opens as **Draft**, and the Status menu offers only the transitions
      the lifecycle allows — not every status.
- [ ] Move a case Draft → Open → In Progress; each move is accepted.
- [ ] Attempting Draft → Closed is refused (use the API directly; the UI does not
      offer it, which is itself the check).
- [ ] Sorting by **Priority** orders `urgent` above `high` above `medium` above
      `low` — not alphabetically.
- [ ] Assign a lawyer and a court representative; both appear on the case.
- [ ] Assigning a *lawyer* account to the court-representative position is
      refused.
- [ ] Archive a case: it disappears from the default list but is still findable,
      and its documents and timeline survive.
- [ ] Restore the archived case; it returns as **Open**, not as Draft.
- [ ] Every filter narrows the list, and *Clear* restores it.

## 5 — Document management

- [ ] Upload a **PDF**, a **DOCX**, and a **JPG or PNG** to one case.
- [ ] Uploading a `.exe` (or any unlisted type) is refused with a message naming
      the accepted types.
- [ ] Uploading a file over 25 MB is refused, and the message names the ceiling.
- [ ] The upload progress bar moves for a large file rather than sitting at zero.
- [ ] The document list shows name, category, size, version, uploader, and date.
- [ ] Preview opens a PDF and an image inline; a DOCX offers **Download** instead
      of a broken preview.
- [ ] Download saves the original file, and it opens correctly.
- [ ] Replace the PDF with a new file: the version becomes **2**, and version 1 is
      still listed **and still downloadable** from the version history.
- [ ] Edit a document's category and description; both persist after a reload.
- [ ] Delete a document: it leaves every list, and the confirmation said plainly
      that the file is kept rather than destroyed.
- [ ] The documents panel inside a case shows only that case's files and hides
      the *Case* column.

## 6 — OCR / text extraction (spec 09)

- [ ] Upload a **scanned PDF** (an image-only one, not a text PDF). Its
      extraction panel shows **Queued**, then **Extracting**, then **Completed**,
      updating **without a manual refresh**.
- [ ] *View extracted text* shows the text page by page, with page numbers.
- [ ] An **Arabic** scan extracts Arabic text, and the page renders
      right-to-left inside the text view.
- [ ] A **French** scan extracts accented characters correctly (é, à, ç), not
      mojibake.
- [ ] The metadata line reports pages, confidence, engine, and duration.
- [ ] A DOCX shows *"Text extraction applies to PDFs and images"* rather than a
      failure — a missing run is not an error.
- [ ] Upload a deliberately corrupt PDF: the run reaches **Failed**, names the
      cause in words, and says the document itself is unaffected.
- [ ] **Retry extraction** on the failed document queues a new run.
- [ ] A court representative sees the extracted text but is offered **no Retry**
      button.
- [ ] *Copy all text* copies the whole document to the clipboard.

## 7 — Document indexing (spec 10)

- [ ] The same scanned PDF's *Search index* panel moves **Queued → Indexing →
      Searchable** on its own, after extraction completed.
- [ ] The panel reports passages, pages, characters, detected language, the
      embedding **model**, and the passage size.
- [ ] The detected language of the Arabic scan reads `Arabic`, not `ar`.
- [ ] A document whose text has not been extracted says indexing begins once it
      has — it does not show a failure.
- [ ] **Rebuild index** is offered to an administrator and a lawyer, and **not**
      to a court representative.
- [ ] Rebuild a document's index: the run completes and the passage count is the
      same as before (the same text produces the same chunks).
- [ ] Replace a document with a new file: a **new** indexing run starts for
      version 2, and version 1's passages are not left behind as duplicates
      (check the passage total on the metrics panel).

## 8 — Semantic search (spec 11)

- [ ] Ask a question in **your own words** (not keywords) whose answer is inside
      an indexed document — the relevant passage comes back.
- [ ] Each result shows the file name, page, passage number, version, and a
      relevance percentage.
- [ ] Ask the **same question in French** against an Arabic document (or the
      reverse): passages still come back — this is the cross-language property
      the embedding model provides, and is worth verifying explicitly.
- [ ] The full passage text is shown, not an ellipsis-truncated fragment.
- [ ] *Open case* on a result opens the case that passage belongs to.
- [ ] Filter by case, category, file type, and language; each narrows results.
- [ ] Search as a **lawyer**: no passage from a case they are not assigned to
      ever appears. **Verify positively** — as the administrator, note a phrase
      that exists only in an unassigned case, then search for it as the lawyer
      and confirm nothing comes back.
- [ ] A search with no matches shows *"No matching passages"* rather than an
      error.
- [ ] A one-character query is refused before a request is sent.

## 9 — RAG pipeline and AI Assistant (specs 12, 13)

- [ ] Ask a question answerable from an indexed document. The answer arrives and
      **carries citations**.
- [ ] Every citation names a real file, version, and page. **Open the cited page
      and confirm the answer is actually supported by it** — this is the single
      most important check in this document.
- [ ] The answer **streams** (text appears progressively) rather than arriving
      all at once, if `ASSISTANT_STREAMING_ENABLED=true`.
- [ ] Ask something the documents do **not** cover — for example a question about
      an unrelated statute. The assistant says it has no supporting document
      rather than inventing an answer, and shows **no citations**.
- [ ] Ask a follow-up using a pronoun (*"and what about the second one?"*). The
      answer is interpreted against the previous turn, and carries the
      **Follow-up** badge.
- [ ] Suggested follow-up questions appear beneath the last answer, and clicking
      one **fills the box** rather than sending it.
- [ ] Rate an answer *Helpful*, then press it again — the rating is withdrawn.
- [ ] Rate an answer *Not helpful*; the transcript itself is unchanged.
- [ ] *Copy* copies the answer text.
- [ ] Rename a conversation; the transcript is untouched.
- [ ] Archive a conversation: it leaves the Active list and appears under
      Archived, still readable.
- [ ] Delete a conversation; it is gone from both lists.
- [ ] Open the assistant **inside a case**: answers cite only that case's
      documents.
- [ ] Ask a question in **Arabic**; the answer comes back in Arabic.
- [ ] Ask a question in **French**; the answer comes back in French.
- [ ] A **court representative** has no AI Assistant at all — not a disabled one.
- [ ] Stop the Qdrant container and ask a question: the failure names retrieval
      as the cause and says your documents are unaffected. Restart it afterwards.

## 10 — AI report generation (spec 14)

- [ ] Generate a **Case Summary** for a case with several indexed documents.
- [ ] The dialog listed the sections the report will contain **before** it was
      generated.
- [ ] The report is queued, and the progress bar moves section by section with a
      real denominator ("Writing section 3 of 7").
- [ ] Closing the dialog and reopening it later shows the run still progressing —
      generation is genuinely in the background.
- [ ] The finished report has every section the template promised.
- [ ] A section the case file does not cover is **marked as not covered** rather
      than filled with invented text.
- [ ] The reference list at the end resolves to real documents and pages.
- [ ] The platform's disclaimer appears on the report.
- [ ] Export as **Markdown**: the file downloads and opens.
- [ ] Export as **PDF**: the file downloads and opens, and **an Arabic report
      renders Arabic glyphs rather than boxes** (this is what the Noto font in
      the API image is for).
- [ ] Generate a report in **Arabic**: the section headings *and* the prose are
      Arabic, and the document reads right-to-left.
- [ ] Generate a report in **French**.
- [ ] Generate each of the other four report types once.
- [ ] **Regenerate** a finished report; a new run starts.
- [ ] Delete a report; it leaves your history.
- [ ] **Sign in as a different user: the first user's reports are not in their
      history at all** — a report is private to whoever generated it, and an
      administrator holding `cases:view-all` still cannot read somebody else's.

## 11 — Timeline and audit trail

- [ ] Creating, editing, assigning, and archiving a case each add a timeline
      entry.
- [ ] Uploading, replacing, and deleting a document each add one.
- [ ] Extraction start, completion, and failure each add one, worded as *"Text
      extraction …"* rather than *"OCR …"*.
- [ ] Each entry names who acted and when.
- [ ] Renaming a user does **not** rewrite the name on their past entries.
- [ ] Filter by activity type, by actor, and by date range.
- [ ] Reverse the sort order; the oldest entry comes first.

## 12 — Real-time synchronization

- [ ] Open the same case in **two browsers signed in as two different assigned
      users**.
- [ ] Change the case status in one; the other updates **without a refresh**.
- [ ] Upload a document in one; it appears in the other's list.
- [ ] Watch an extraction run from the second browser; its status advances there
      too.
- [ ] Stop the API. The second browser shows *"Updates paused"* and the page
      still works — nothing is blank and nothing throws.
- [ ] Restart the API; the indicator returns to connected on its own.
- [ ] A user **not** assigned to the case receives none of these updates.

## 13 — Notifications, email, and WhatsApp

- [ ] Assigning a lawyer to a case gives that lawyer an in-app notification, and
      the bell's unread count increases.
- [ ] The notification names the case and links to it.
- [ ] Opening a notification does **not** mark it read; *Mark read* does.
- [ ] *Mark all as read* empties the badge.
- [ ] Filter the history by category, type, priority, and unread state.
- [ ] Switch a preference off in Settings → Notifications; a new event of that
      kind produces **no** new notification, and the ones already in the feed are
      untouched.
- [ ] Send a **platform announcement** as an administrator; every other signed-in
      account receives it, and the toast reports the recipient count.
- [ ] Send a **maintenance** announcement; it arrives as high priority.
- [ ] **Email** — skip unless `EMAIL_ENABLED=true`. With Mailpit running
      (`http://localhost:8025`), assigning a case produces an email whose subject
      and body match the in-app wording, in the recipient's own language.
- [ ] **Email** — a document upload produces **no** email (only selected kinds
      travel on that channel).
- [ ] **WhatsApp** — skip unless `WHATSAPP_ENABLED=true` **and** the templates are
      approved in Meta's console. An account with no phone number is skipped
      rather than failed.

## 14 — Dashboard

- [ ] The dashboard loads and its widgets differ by role.
- [ ] Every figure matches what the underlying page shows — spot-check the case
      count against the case list, and the document count against the documents
      list.
- [ ] Switch the time filter through Today / 7 days / 30 days; the figures change.
- [ ] Set a **custom range**; *Apply* stays disabled until both dates are set and
      ordered.
- [ ] Refresh a single widget with its own button; only that card reloads.
- [ ] Quick actions open the pages they name.
- [ ] Stop Qdrant: the AI widgets report themselves unavailable **and the rest of
      the dashboard still loads**. Restart it afterwards.

## 15 — Settings and monitoring

- [ ] Change your **theme**; it applies immediately and survives a reload.
- [ ] Change the **date format**; a hearing date on the case list re-renders in
      the new shape.
- [ ] Change the **time zone**; a timestamp shifts by the right number of hours.
- [ ] Change your **password**; every other device is signed out and the device
      you changed it on stays signed in.
- [ ] *Sign out everywhere else* ends the other sessions and not this one.
- [ ] The active-session list names each sign-in, and marks the current device.
- [ ] As an administrator, change a **platform default**; an account that has
      never chosen for itself picks it up.
- [ ] Turn **maintenance mode** on; every signed-in user sees the notice and the
      platform keeps working.
- [ ] `/monitoring` shows health, latency, queue depths, errors, and security
      counts, and refuses a lawyer.
- [ ] The security panel reports **counts only** — no email address and no IP
      appears anywhere on it.

---

## 16 — Localization: Arabic and French

This is the section the recent change is about. Everything above was run in the
default language; this runs the platform again in the other two.

### 16.1 Switching

- [ ] The language switcher in the header lists **English, Français, العربية** —
      each written in its own language, never translated.
- [ ] Choosing **Français** re-renders the interface immediately, without a page
      reload and without a flash of English.
- [ ] The choice survives a full reload.
- [ ] The choice survives **signing out and back in** — it is stored on the
      account, not only in the browser.
- [ ] Sign in on a **second browser** with the same account: it is already
      French. This is the property that proves the preference is server-side.
- [ ] Sign out and look at the **login page**: it is still French. (This one is
      `localStorage`, and it is why the login screen can be translated at all.)
- [ ] Settings → Language & region shows the same language, and changing it there
      changes the header switcher too — one stored value, two surfaces.

### 16.2 Coverage — the actual fix

Walk **every** sidebar destination in French and then again in Arabic. For each,
the check is the same: **no English text anywhere on the screen**, including
inside dialogs, dropdown menus, empty states, and toasts.

- [ ] **Dashboard** — widget titles, descriptions, metric labels, the time
      filter, quick actions, and the section headings above each group.
- [ ] **Cases** — the table's column headers, status and priority badges, every
      filter label and placeholder, the row action menu, and all four dialogs
      (Create, Edit, Assignments, Archive).
- [ ] **Case details** — the four cards (General, Assignment, Court, Audit), every
      field label, and the embedded Documents, Timeline, Search, Assistant, and
      Reports sections.
- [ ] **Documents** — the table, filters, row actions, and all five dialogs
      (Upload, Details, Preview, Replace, Delete), plus the **OCR** and **Search
      index** panels inside the details dialog.
- [ ] **AI Assistant** — the conversation list, its filters and row menu, the
      composer with its hint line, the citation list, the feedback controls, the
      empty states, and the Rename and Delete dialogs.
- [ ] **Reports** — the table, filters, the Generate dialog (including the section
      preview), the detail dialog, the progress line, the export menu, and the
      Delete dialog.
- [ ] **Search** — the query label and placeholder, the filter bar, the result
      cards, and both empty states.
- [ ] **Notifications** — all three tabs, the filters, the item rows, the
      preferences grid, and the announcement form.
- [ ] **Users** — the table, filters, row actions, and all five dialogs.
- [ ] **Settings** — all nine section names, every setting's title and
      description, and every permitted value in every dropdown.
- [ ] **Monitoring** — every panel heading, metric label, and state badge.
- [ ] **Court Updates** and **Lawyers** — the placeholder copy.
- [ ] The **404 page** (visit `/nonexistent`).
- [ ] The **Access denied** page (visit a page your role cannot reach).

### 16.3 Error and validation messages

These were English until this change, and they are the easiest thing to miss —
they only appear when something goes wrong.

- [ ] Submit the **Create case** form with a blank title: the validation message
      is in the interface language.
- [ ] Submit **Create user** with a blank name and a malformed email: both
      messages are translated.
- [ ] Enter a password shorter than the minimum on the password-change form.
- [ ] Try to upload an unsupported file type: the refusal names the accepted
      types, in the interface language.
- [ ] Try a case-status transition the lifecycle forbids (via the API): the
      banner is translated.
- [ ] Stop the API and try any action: *"The server could not be reached"* is
      translated, not an English network error.
- [ ] Sign in with a wrong password **on the Arabic login screen**: the refusal is
      in Arabic.
- [ ] Lock yourself out: the throttling message is translated.

### 16.4 Right-to-left (Arabic only)

- [ ] The whole layout mirrors: the sidebar is on the **right**, and the main
      content on the left.
- [ ] Text is right-aligned throughout.
- [ ] The sidebar collapse chevron points the correct way, and pressing it still
      collapses.
- [ ] Pagination **Previous** and **Next** arrows point the correct way, and
      **Next really advances a page** — a mirrored arrow with an unmirrored
      handler is the classic RTL defect.
- [ ] Dropdown menus and popovers open **inside** the viewport, not off the left
      edge.
- [ ] Dialogs are centred, and their close button is in the mirrored corner.
- [ ] Icons that sit beside text (the search magnifier, the file-type glyphs) are
      on the correct side of it.
- [ ] The progress bar on a running report **fills from the right**.
- [ ] Tables read right-to-left: the first column is on the right.
- [ ] Nothing is clipped, overlapping, or requires horizontal scrolling that the
      English layout did not.
- [ ] A **mixed-direction** case: open a case whose title is Arabic and whose
      court name is French. Each renders in its own direction rather than one
      forcing the other.
- [ ] The AI assistant's transcript: an Arabic question and a French answer each
      render in their own direction, in the same thread.

### 16.5 Dates, times, and numbers

- [ ] In **French**, a thousands separator is a space (`1 024`), not a comma.
- [ ] In **Arabic**, digits are Western Arabic numerals (`1024`) — this is the
      `ar-MA` locale doing its job, and Eastern Arabic numerals here would make a
      case number unreadable to a French colleague on the same matter.
- [ ] A file size reads `2,4 Mo`-style in French and keeps the `MB`/`KB` symbol
      in Arabic — unit symbols are not translated.
- [ ] A percentage renders with the locale's decimal mark.
- [ ] A relative timestamp reads *"Aujourd'hui"* / *"اليوم"* rather than
      *"Today"*.
- [ ] A long-format date shows the month name in the interface language.

### 16.6 Generated content

The interface is one half of localization; what the platform *writes* is the
other.

- [ ] An **AI answer** in Arabic is Arabic prose, not English prose with Arabic
      chrome around it.
- [ ] A **report** generated in French has French section headings and French
      prose.
- [ ] A **notification** in the feed is in the reader's language — and, with two
      accounts on different languages, **the same event reads differently to
      each of them**. This is the property that stored prose would have made
      impossible.
- [ ] An **email**, if the channel is on, arrives in the recipient's language.
- [ ] **An uploaded document is never translated.** Open a French contract with
      the interface in Arabic: the document, its preview, and its extracted text
      are all still French. Localization is presentation only.

### 16.7 Fallback and integrity

- [ ] Switching language does **not** change what you can see: note a lawyer's
      case count in English, switch to Arabic, and confirm the same cases and the
      same total.
- [ ] Switching language does not sign you out or reset any filter.
- [ ] **No translation key ever appears on screen** — nothing that looks like
      `cases.filters.status`. If a translation were missing you would see English
      or a humanized phrase, never a key.
- [ ] With the network throttled, switch language: the previous language stays on
      screen until the new catalogue arrives rather than the page blanking.
- [ ] Monitoring → the localization figures report the active languages and the
      language distribution, and report **keys only** for missing translations.

---

## Sign-off

| Area | Result | Notes |
| --- | --- | --- |
| 1–3 Environment, accounts, authorization | | |
| 4–5 Cases, documents | | |
| 6–8 OCR, indexing, search | | |
| 9–10 Assistant, reports | | |
| 11–13 Timeline, real-time, notifications | | |
| 14–15 Dashboard, settings, monitoring | | |
| 16 Localization (fr + ar) | | |

**Blocking defects found:**

**Non-blocking defects found:**
