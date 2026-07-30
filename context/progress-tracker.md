# Progress Tracker

Update this file after every meaningful implementation
change.

## Current Phase

- In progress

## Current Goal

- **Next:** awaiting the next feature specification. Document Management is
  complete; per its "Out of Scope" section, OCR, text extraction, embeddings,
  vector storage, semantic search, the AI Assistant, AI report generation,
  automatic classification, summarization, the Timeline, and Notifications are
  each their own unit. All of them now have a Document to attach to.

## Completed

- **Document Management (spec `07-document-management.md`)** — secure upload,
  versioning, preview, download, metadata management, and archiving of documents
  attached to a case, layered on the Authentication, Authorization, User
  Management, and Case Management modules already in place. **No new
  dependencies**, backend or frontend: `python-multipart` and the MinIO client
  were already installed by earlier specs.
  - **Two entities** (`models/document.py` + migration `e2f8a4c19d57`).
    `documents` carries **exactly the fields the spec lists** — `id`, `case_id`,
    `original_filename`, `stored_filename`, `file_extension`, `mime_type`,
    `file_size`, `storage_bucket`, `storage_key`, `category`, `description`,
    `version`, `uploaded_by`, `created_at`, `updated_at`, `deleted_at` — and
    describes the *current* version. `document_versions` is one immutable row per
    uploaded file, including the current one, with a **unique
    `(document_id, version)` constraint**: the next number is a read-then-write,
    so without it two simultaneous replacements could both commit N+1 and one file
    would vanish from the history.
  - **`DocumentCategory`** (`contract` / `evidence` / `court_decision` /
    `pleading` / `correspondence` / `invoice` / `identity_document` / `other`),
    persisted as a PostgreSQL enum, declared once, with `CATEGORY_RANK` **derived
    from the declaration order** so no second list can drift.
  - **Document utilities** (`core/documents.py`): the category rank, the
    extension → MIME map (the *only* source of a served MIME type), the
    previewable set, the magic-byte signatures, filename sanitisation, storage-key
    construction, and size formatting. Pure functions, unit-testable without a
    database, a request, or a running MinIO.
  - **Schemas** (`schemas/document.py`): `DocumentRead` (with **computed**
    `is_deleted`, `version_count`, `is_previewable`, and `file_size_label`, so the
    payload cannot drift from the policy), `DocumentVersionRead`,
    `DocumentCaseSummary`, `DocumentPage`, `DocumentUploadForm`, `DocumentUpdate`,
    `DocumentListQuery`. Every binary and immutable field is **absent** from
    `DocumentUpdate` rather than validated and rejected; with `extra="forbid"`,
    sending one is a 422. The uploader summary is `CaseUserSummary` reused, not a
    second copy.
  - **Repository** (`repositories/document.py`): search, filtering, sorting,
    pagination, **and the case scope** all execute in the database. LIKE wildcards
    are escaped, the primary key is appended to every `ORDER BY` as a tiebreaker,
    the upload-date filter covers the whole end day, and category sorts through a
    **searched** SQL `CASE` built from `CATEGORY_RANK`.
  - **MinIO storage service** (`services/document_storage.py`): upload, download
    (streamed, never buffered), retrieve metadata, and a **logical-only** delete
    that deliberately removes nothing. Every MinIO failure becomes a generic 503
    with the specifics in the log.
  - **Upload validation** (`services/document_validation.py`): missing, empty,
    oversized, unsupported type, and *corrupted* (leading bytes must match the
    declared format). Framework-independent — it takes a filename and a stream,
    not an `UploadFile` — so a future importer validates through the same path.
  - **Per-resource authorization** (`services/document_access.py`): owns no policy
    of its own and **delegates every decision to `CaseAccessPolicy`**, so document
    access cannot drift from case access. The shared scope predicate was extracted
    as `assigned_case_scope` in `repositories/case.py` rather than restated.
  - **Service** (`services/document.py`): upload, replace (a new version, never an
    overwrite), metadata update, idempotent soft delete, and the download/preview
    paths. Storage is written **before** the metadata commit, deliberately: the
    reverse order can leave a row pointing at an object that was never written,
    which no retry repairs, while this order can only leave an unreferenced object.
  - **Endpoints** (`api/v1/documents/router.py`): `GET /documents` (page, size,
    search, case, category, uploader, file type, upload-date range,
    include_deleted, sort_by, sort_order), `POST /documents/upload` (201),
    `GET /documents/{id}`, `GET /documents/{id}/versions`,
    `GET /documents/{id}/download?version=`, `GET /documents/{id}/preview?version=`,
    `PATCH /documents/{id}`, `POST /documents/{id}/replace`,
    `DELETE /documents/{id}`. Each guarded by `require_permission`, so
    authorization is declared beside the route and appears in OpenAPI.
  - **Errors** (`core/exceptions.py`): `DocumentNotFoundError` (404),
    `DocumentVersionNotFoundError` (404), `InvalidDocumentFileError` (422, naming
    the `file` field), `DocumentPreviewUnavailableError` (415, pointing at the
    download), `DocumentStorageError` (503, generic body with the S3 specifics in
    the log only), and `DocumentAccessDeniedError` (403, generic body).
  - **Configuration:** `MINIO_DOCUMENTS_BUCKET`, `MAX_DOCUMENT_SIZE_MB` (25), and
    `ALLOWED_DOCUMENT_EXTENSIONS`, all documented in `.env.example`. The extension
    list can only ever **narrow** the policy — a type with no MIME entry cannot be
    served, so configuration alone cannot enable one.
  - **Logging:** `document_uploaded`, `document_downloaded`, `document_previewed`,
    `document_replaced`, `document_updated` (field **names** only),
    `document_deleted`, plus `document_object_uploaded`,
    `document_object_logically_deleted`, `document_upload_rejected`,
    `document_access_denied`, and every lookup failure. Identifiers, the category,
    and the file's shape only — **never a filename and never a description**, both
    of which can name a client or quote a matter.
  - **Frontend:** `types/document.ts`, `types/document-management.ts`,
    `lib/validation/document.ts` (form + response Zod schemas mirroring the API),
    `lib/api/documents.ts` (typed client, snake_case ↔ camelCase in one place),
    `lib/api/upload.ts` (multipart with real progress, plus authenticated binary
    fetch and save), `hooks/use-documents.ts`, `hooks/use-document-list-query.ts`,
    and `hooks/use-document-cases.ts` — which reads the **Case Management** list
    rather than adding a second "cases I may upload to" endpoint.
  - **UI** (`components/documents/`): `DocumentList` (the container),
    `DocumentTable` (sortable headers as real buttons carrying `aria-sort`),
    `DocumentFilters`, `DocumentPagination`, `DocumentTableSkeleton`,
    `DocumentRowActions`, `DocumentCategoryBadge` / `DocumentTypeIcon`,
    `UploadProgress`, `DocumentVersionHistory`, `CaseDocuments`, and five dialogs —
    upload, details (metadata + version history + inline editing), preview,
    replace, and delete (an `AlertDialog`, stating plainly that the document is
    *kept*). Page at `/documents`, plus the case-scoped list on `/cases/[id]`.
    Design System components only.
  - **The case workspace's Documents placeholder was replaced with the real
    list**, pinned to that case. `CasePlaceholderSections` now reserves four cards
    (Timeline, Notes, AI Assistant, Reports) instead of five.
  - **Every UI gate names a permission, never a role**, and no action the API would
    refuse is offered: Replace and Delete are hidden without `documents:update` /
    `documents:delete`, and **Preview is hidden for a file type the server says it
    cannot render**, taken from the computed `is_previewable` rather than a second
    client-side copy of the rule.
  - **Uploads use `XMLHttpRequest`, not `fetch` — a deliberate deviation from the
    rest of the API client.** `fetch` reports nothing while a request body is being
    sent, and streaming request bodies are not available across the browsers this
    platform targets, so a real progress bar is impossible with it. A 25 MB scan on
    a slow link is exactly the case the spec's "display upload progress" is about.
    `lib/api/upload.ts` reuses the same Bearer credential, cookie handling,
    `ApiError` envelope, and refresh-once-and-replay behaviour.
  - **One FastAPI trap found during implementation:** `Annotated[Model, Form()]`
    is **not** flattened when the same request also carries a separate `File`
    part — the model arrives as one missing field called `payload` and every upload
    is a 422 (reproduced in isolation). The upload endpoint therefore declares its
    form fields individually and assembles `DocumentUploadForm` in a helper, which
    keeps the rules in the schema layer and re-raises a Pydantic failure as
    FastAPI's own validation error so it reaches the client in the standard
    envelope.
  - **The PostgreSQL enum-vs-VARCHAR bug that shipped in Case Management was not
    repeated.** The category `ORDER BY` is a searched `CASE WHEN category = …` from
    the start, and a dialect-compiled regression test asserts no category value is
    bound as a `String` — the SQLite test database still cannot catch this class of
    fault on its own.
  - **Validation (live Postgres + Redis + MinIO + Qdrant, real HTTP):** 1067
    backend tests (up from 841 — 226 of them for documents) and 341 frontend tests
    (up from 282) pass; `ruff` clean across `apps/api` and `tests`,
    `mypy --strict` clean on `apps/api`; `tsc` and ESLint clean; the production
    build succeeds and prerenders every route. Migration verified on **live
    PostgreSQL in both directions**: the upgrade creates the enum type, both
    tables, all seven indexes, the unique `(document_id, version)` constraint, and
    all four foreign keys; the downgrade drops both tables **and the enum type**
    (confirmed absent from `pg_type`), and a re-upgrade is clean. **132/132
    end-to-end HTTP checks passed** against a running API with real MinIO:
    unauthenticated requests to all nine routes return **401** with a
    `WWW-Authenticate: Bearer` challenge; an upload stores the bytes in MinIO under
    a case/document/version key and the metadata in PostgreSQL; the original
    filename is preserved while the stored name is generated; empty, unsupported,
    corrupted, and missing files each return **422** naming the `file` field, and
    an unknown case **404**; a spoofed `Content-Type: text/html` on a `.txt` is
    ignored and the file is served as `text/plain`; `../../etc/passwd.pdf` is
    stored as `passwd.pdf`; downloads carry the original name in both
    `Content-Disposition` forms (an Arabic filename verified to survive) with
    `nosniff`, a sandbox CSP, and `no-store`; a PDF previews inline while a DOCX
    returns **415** pointing at the download and still downloads; three
    replacements produce v1/v2/v3 under three distinct keys with **the earlier
    objects byte-for-byte intact in MinIO**, all three downloadable, and an unknown
    version **404**; a PATCH changes only category and description and leaves the
    binary untouched, while all five binary/immutable fields return 422; search is
    case-insensitive across filename, description, and category name and treats
    `%` literally; every filter, all five sort columns in both directions, and
    pagination behave; the category sort keeps `other` last; both restricted roles
    read and upload only on their assigned case, are refused another case's
    documents with a **403** naming neither permission nor role, and are refused
    update and delete entirely; deletion is soft — 404 afterwards, gone from the
    list, recoverable with `include_deleted`, idempotent, **and the file still in
    MinIO**. **Zero 5xx responses and no tracebacks in the server log**; the log
    shows all fourteen document events with **no filename, description, password,
    hash, or JWT anywhere**. Frontend routes: `/documents` 307s to `/login`
    anonymously (carrying `?next=`) and 200s with a session cookie, as does
    `/cases/[id]`; no errors or warnings in the dev-server log.

- **Case Management (spec `06-case-management.md`)** — the platform's central
  business entity and the workflow around it, layered on the Authentication,
  Authorization, and User Management modules already in place. No new
  dependencies, backend or frontend.
  - **Case entity** (`models/case.py` + migration `b7d4e21c8f36`): every field
    the spec lists — `case_number` (unique, indexed), `title`, `description`,
    `category`, `status`, `priority`, `court_name`, `filing_date`,
    `next_hearing_date`, both assignments, and the four audit columns. Dates are
    `Date`, not timestamps: a filing happens on a day, and storing an instant
    would make the value depend on the reader's timezone. All four foreign keys
    into `users` are `ON DELETE SET NULL` — a case with an unknown assignee is
    recoverable, a deleted case is not.
  - **`CaseStatus`** (`draft` / `open` / `in_progress` / `waiting_for_hearing` /
    `closed` / `archived`) and **`CasePriority`** (`low` / `medium` / `high` /
    `urgent`), both persisted as PostgreSQL enums.
  - **Case utilities** (`core/cases.py`): `STATUS_TRANSITIONS` (a read-only
    mapping, so the policy cannot be widened by mutation at runtime),
    `can_transition`, `PRIORITY_RANK`, normalization, and case-number formatting
    and parsing. Pure functions, unit-testable without a database.
  - **Schemas** (`schemas/case.py`): `CaseRead` (with a **computed**
    `allowed_transitions` so the payload cannot drift from the lifecycle rules),
    `CaseUserSummary`, `CasePage`, `CaseCreate`, `CaseUpdate`,
    `CaseAssignmentUpdate`, `CaseListQuery`. Immutable fields (`id`,
    `case_number`, `created_by`, `created_at`) are **absent** from `CaseUpdate`
    rather than validated and rejected, so there is no field to forget to guard;
    with `extra="forbid"`, sending one is a 422.
  - **Repository** (`repositories/case.py`): search, filtering, sorting,
    pagination, **and the assignment scope** all execute in the database. LIKE
    wildcards are escaped, the primary key is appended to every `ORDER BY` as a
    tiebreaker, and priority sorts through a SQL `CASE` built from
    `PRIORITY_RANK`.
  - **Per-resource authorization** (`services/case_access.py`): `CaseAccessPolicy`
    decides which cases a caller reaches and which fields they may write. Two new
    permissions — `cases:view-all` (lifts the row restriction) and
    `cases:update-hearing` (the court-facing fields only). This closes the open
    question RBAC left behind.
  - **Service** (`services/case.py`): case-number generation with retry on
    collision, uniqueness, legal transitions, assignee role and status
    validation, the date rule that needs the stored case, soft-delete archiving,
    and audit fields populated from the authenticated caller rather than the
    request.
  - **Endpoints** (`api/v1/cases/router.py`): `GET /cases` (page, size, search,
    status, priority, both assignees, court, two date ranges, sort_by,
    sort_order), `GET /cases/{id}`, `POST /cases` (201), `PATCH /cases/{id}`,
    `PATCH /cases/{id}/assignments`, `DELETE /cases/{id}` (archive, returns the
    updated case). The assignment endpoint delegates to the same service method
    as the general update, so neither can drift from the other.
  - **Errors** (`core/exceptions.py`): `CaseNotFoundError` (404),
    `DuplicateCaseNumberError` (409), `InvalidCaseTransitionError` (409, naming
    both statuses), `InvalidAssignmentError` (422, naming the field),
    `InvalidCaseDatesError` (422), `CaseAccessDeniedError` (403, generic body),
    and `CaseNumberGenerationError` (500, specifics in the log only).
  - **Logging:** `case_created`, `case_updated` (field **names** only),
    `case_status_changed` and `case_assignment_changed` as their own events — so
    Notifications and the Timeline can subscribe to them rather than parsing a
    field list — plus `case_archived` and every rejection path. Case *numbers*
    are logged, never titles, descriptions, or courts, which are
    client-confidential.
  - **Frontend:** `types/case.ts`, `types/case-management.ts`,
    `lib/validation/case.ts` (form + response Zod schemas mirroring the API),
    `lib/api/cases.ts` (typed client, snake_case ↔ camelCase in one place),
    `hooks/use-cases.ts` (TanStack Query: list, detail, create, update, assign,
    archive, restore), `hooks/use-case-list-query.ts`, and
    `hooks/use-case-assignees.ts` — which reads the **User Management**
    directory rather than adding a second "assignable users" endpoint.
  - **UI** (`components/cases/`): `CaseList` (the container), `CaseTable`
    (sortable headers as real buttons carrying `aria-sort`), `CaseFilters`,
    `CasePagination`, `CaseTableSkeleton`, `CaseRowActions`, `CaseAssignee`,
    status/priority badges, `CaseFormFieldset`, `CaseDetails`,
    `CasePlaceholderSections`, and four dialogs — create, edit, assign, and
    archive (an `AlertDialog`, stating plainly that the case is *kept* and stays
    searchable). Pages at `/cases` and `/cases/[id]`. Design System components
    only.
  - **Placeholder sections only, as the spec requires:** dashed cards reserving
    the case workspace's layout for Documents, Timeline, Notes, AI Assistant, and
    Reports, each saying explicitly that the module is not built yet. No
    functionality.
  - **Every UI gate names a permission, never a role**, and no action the API
    would refuse is offered: assignment fields are hidden from a caller without
    `cases:assign`, and Archive and Restore each name the permission their own
    request needs.
  - **One real defect found by end-to-end verification, not by tests:**
    sorting by priority returned **500** on PostgreSQL. The `ORDER BY` used
    SQLAlchemy's shorthand `case({...}, value=Case.priority)`, whose keys bind as
    `VARCHAR` — and PostgreSQL has no `case_priority = character varying`
    operator. **The whole test suite ran on SQLite, which is untyped enough to
    accept it**, so 269 passing case tests said nothing about it. Rewritten as a
    searched `CASE WHEN Case.priority == …`, which binds each value with the
    column's own type. A regression test now compiles the clause against the
    PostgreSQL dialect and asserts no priority value is bound as a `String` —
    verified to fail on the old form and pass on the new one, so the gap is
    closed without needing a running database. **General lesson: the SQLite test
    database cannot catch a PostgreSQL type mismatch; anything that builds SQL by
    hand needs either a dialect-compiled assertion or a live check.**
  - **Validation (live Postgres + Redis + MinIO + Qdrant, real HTTP):** 841
    backend tests (up from 563 — 272 of them for cases) and 282 frontend tests
    (up from 211) pass; `ruff` clean across `apps/api` and `tests`,
    `mypy --strict` clean on `apps/api`; `tsc` and ESLint clean; the production
    build succeeds and prerenders all 16 routes (`/cases/[id]` added alongside
    `/users/[id]`). Migration verified on **live PostgreSQL in both directions**:
    the upgrade creates both enum types, the table, all five indexes, and all
    four `ON DELETE SET NULL` foreign keys; the downgrade drops the table **and
    both enum types** (confirmed absent from `pg_type`), and a re-upgrade is
    clean. Over HTTP against a running API: unauthenticated requests to all six
    routes return **401** with a `WWW-Authenticate: Bearer` challenge; both
    restricted roles get **403** on create with a body naming neither permission
    nor role; a case number is generated (`CASE-2026-0001` → `0002` → `0003`) and
    a registry number (`TC/2026/9999`) does not disturb the series; a duplicate
    returns **409** and a wrongly-rolled assignee **422** naming the field; an
    unassigned lawyer gets **403** on read while the assigned one gets 200, and
    the list totals differ accordingly (1 / 0 / 1 / 4 for lawyer / other lawyer /
    court / administrator); a court representative can record a hearing and a
    status change but is refused a title edit, and a mixed update is refused **in
    full** with both fields verified unchanged; an illegal transition returns 409
    naming both statuses; a hearing moved before the stored filing date returns
    422; all four immutable fields return 422; assignment grants and withdraws
    access immediately, and a lawyer self-assigning is refused; search is
    case-insensitive across all four fields and treats `%` literally; every
    status and priority filter, the court substring, date ranges, and combined
    filters return the right counts; priority sorts by urgency in both
    directions and case numbers in issue order; pages do not overlap; archiving
    is a soft delete that stays readable, searchable, and idempotent, and
    restores to `open`. **Zero 5xx responses and no tracebacks in the server
    log**; the log shows `case_created`, `case_updated`, `case_status_changed`,
    `case_assignment_changed`, `case_archived`, `case_access_denied`, and the
    rejection paths — with case *numbers* only, and no title, description, court,
    password, hash, or JWT anywhere. Frontend routes: `/cases` and `/cases/[id]`
    307 to `/login` anonymously (carrying `?next=`) and 200 with a session
    cookie; no errors or warnings in the dev-server log.

- **User Management (spec `05-user-management.md`)** — the complete administrator
  workflow for provisioning and managing accounts, layered on the identity
  (Authentication) and capability (RBAC) systems already in place. No new
  dependencies, backend or frontend.
  - **User entity completed** (`models/user.py` + migration `c41d7b8e5a92`): the
    identity-only row grew into the full entity the spec defines —
    `first_name` / `last_name` (replacing `full_name`), `phone`, `profile_image`,
    `status`, `must_change_password`, `created_by`, `updated_by`. Two derived
    properties keep every existing caller working unchanged: `full_name` composes
    the parts, and `is_active` reads `status is ACTIVE`. **No authentication code
    was touched.**
  - **`UserStatus`** (`active` / `inactive` / `suspended`) replaces the `is_active`
    boolean, which could not express "suspended". `inactive` *is* the soft-delete
    state, so there is no second "deleted" flag that could disagree with it.
  - **User utilities** (`core/users.py`): name/email/phone normalization and
    `split_full_name` / `compose_full_name`. Pure functions, so the same rules
    apply through the API, through `scripts/create_user.py`, and through any
    future import — and they are unit-testable without a request.
  - **Schemas** (`schemas/user.py`): `UserRead` (one user shape on the wire, used
    by both `/auth/me` and the directory), `UserCreate`, `UserUpdate`,
    `UserListQuery`, `UserPage`, `PasswordResetResponse`. `UserUpdate.provided_fields()`
    uses `exclude_unset`, which is what separates "leave the phone alone" from
    `"phone": null` meaning "clear it".
  - **Password policy extracted** to `schemas/password.py` so `schemas.auth` (a
    user changing their own) and `schemas.user` (an administrator setting one)
    enforce the same rules from one definition — `schemas.auth` imports `UserRead`
    from `schemas.user`, so either importing the other directly would be a cycle.
    `schemas.auth` re-exports `MIN_PASSWORD_LENGTH` / `NewPassword`, so existing
    importers are unaffected.
  - **Repository** (`repositories/user.py`): search, filtering, sorting, and
    pagination all execute **in the database**, so a page costs the same whatever
    the directory's size. LIKE wildcards in a search term are escaped (an
    unescaped `%` would match everyone), and the primary key is appended to every
    ORDER BY as a tiebreaker — without it, rows tying on a sort value (two users
    who have never signed in) could be duplicated or skipped across pages.
  - **Service** (`services/user.py`): the business rules no permission can
    express — email uniqueness (case-insensitive, and an edit may re-submit its
    own email), audit fields populated from the authenticated caller rather than
    the request, soft delete, and password reset.
  - **Endpoints** (`api/v1/users/router.py`): `GET /users` (page, size, search,
    role, status, sort_by, sort_order), `GET /users/{id}`, `POST /users` (201),
    `PATCH /users/{id}`, `DELETE /users/{id}` (soft delete, returns the updated
    user), `POST /users/{id}/reset-password`. Each guarded by
    `require_permission(Permission.USERS_*)`, so authorization is declared beside
    the route and appears in OpenAPI.
  - **Password reset** generates a 16-character password with `secrets`, stores
    only its bcrypt hash, returns it **once** (it is never logged and cannot be
    retrieved again), sets `must_change_password`, and revokes every session for
    that user. Deactivation revokes sessions the same way, so a disabled user
    loses access immediately rather than when their token expires.
  - **Force password change:** `must_change_password` is set by a reset, carried
    on every user payload (so a client sees it at sign-in), and cleared by
    `PATCH /auth/change-password`.
  - **Errors** (`core/exceptions.py`): `UserNotFoundError` (404),
    `DuplicateEmailError` (409 — the request is well-formed; whether it can
    succeed depends on system state), and `SelfModificationError` (400).
  - **Self-lockout guard (a judgement call, not in the spec):** an administrator
    may not deactivate themselves or change their own role or status. The
    alternative is an administrator who cannot undo it — and, if they are the last
    one, a platform recoverable only by running a script on the server. Editing
    one's own name, phone, or avatar stays permitted, and re-submitting an
    unchanged role is not a change, so an edit form that posts every field works.
  - **Logging:** `user_created`, `user_updated`, `user_deactivated`,
    `user_password_reset`, plus the rejection paths. `user_updated` records the
    field **names** only — so an operator can see what an administrator touched
    without an email or phone number entering the log. Verified: no password,
    hash, JWT, email, name, or phone appears anywhere.
  - **Frontend:** `types/user.ts` (+ `UserStatus`, `ManagedUser`, labels),
    `types/user-management.ts` (query/payload DTOs), `lib/validation/user.ts`
    (form + response Zod schemas mirroring the API's rules),
    `lib/api/users.ts` (typed client, snake_case ↔ camelCase in one place),
    `lib/format.ts` (Intl date formatting, locale pinned so SSR and client agree),
    `hooks/use-users.ts` (TanStack Query: list, detail, create, update,
    deactivate, activate, reset) and `hooks/use-user-list-query.ts` (search,
    filters, sort, page).
  - **UI** (`components/users/`): `UserDirectory` (the container), `UserTable`
    (sortable headers as real buttons carrying `aria-sort`), `UserFilters`,
    `UserPagination`, `UserTableSkeleton`, `UserRowActions`, `UserAvatar`,
    role/status badges, `UserFormFieldset`, and four dialogs — create, edit,
    deactivate (an `AlertDialog`, stating plainly that the account is *kept*),
    and reset-password (confirm, then reveal once with a copy control). Pages at
    `/users` and `/users/[id]`. Design System components only.
  - **Every UI gate names a permission, never a role** (`<Protected permission=…>`),
    so a policy change in `core/roles.py` reaches the menus with no edit. Actions
    the API would refuse — deactivating your own account — are not offered.
  - **Two real defects found and fixed by end-to-end verification, not by tests:**
    (1) `proxy.ts` carried a **hand-maintained** list of protected route prefixes
    and `/users` was missing from it, so the app shell was served to anonymous
    visitors. The list is now *derived* from `ROUTES` minus the two public
    routes — the previous shape failed **open** whenever someone forgot an entry.
    A test now asserts every route in `ROUTES` is protected. (2) A stale
    pre-existing dev server was serving `/users/[id]` as a 500; a clean restart
    confirmed the route itself was fine.
  - **Validation (live Postgres + Redis, real HTTP):** 563 backend tests (up from
    377) and 211 frontend tests (up from 153) pass; `ruff`, `mypy --strict`, `tsc`,
    and ESLint clean; production build succeeds and prerenders all 14 routes.
    Migration verified on live Postgres in **both** directions: upgrade split five
    existing `full_name` values into correct first/last names and mapped
    `is_active` onto `status`; downgrade restored `full_name` and `is_active`
    exactly and dropped the `user_status` enum type; re-upgrade clean. Over HTTP:
    unauthenticated requests to all six routes return **401** with a
    `WWW-Authenticate: Bearer` challenge; both restricted roles get **403** on all
    six with a body that names neither the permission nor the role; create
    normalizes and populates audit fields; a duplicate email returns **409**; a
    bad phone returns **422** with the offending field named; search is
    case-insensitive and treats `%` literally; filters combine; pagination and
    sorting work; a partial PATCH leaves other fields alone and `"phone": null`
    clears it; a password cannot be set through PATCH; self role-change and
    self-deactivation are refused while editing one's own profile is allowed. Full
    reset lifecycle verified: victim's session dies, old password stops working,
    the temporary password signs in with `must_change_password: true`, and
    changing the password clears the flag. Deactivation kills the live session,
    refuses login with `account_disabled`, keeps the row readable, is idempotent,
    and reactivation restores sign-in. OpenAPI carries a summary, description,
    request schema, and error responses for all six endpoints. Frontend routes:
    `/users` and `/users/[id]` 307 to `/login` anonymously and 200 with a session
    cookie; no errors or warnings in the dev-server log.

- **Authorization / RBAC (spec `04-authorization-rbac.md`)** — a centralized,
  reusable permission system layered on the identity established by
  Authentication. No business features were implemented (all explicitly out of
  scope), and no dependencies were added — backend and frontend both.
  - **Permissions** (`core/permissions.py`): all 23 identifiers from the spec as a
    `Permission` `StrEnum` with `group:action` values, plus `PermissionGroup`,
    `ALL_PERMISSIONS`, and utilities (`permission_from_value`,
    `permissions_in_group`, `sort_permissions`). A permission's group is *derived*
    from its identifier, so the two can never disagree. Extending the system is
    one enum member plus a grant.
  - **Roles** (`core/roles.py`): `UserRole` stays the single role definition;
    `ROLE_PERMISSIONS` is the only place that decides what a role may do. Held as
    a `MappingProxyType` of `frozenset`s, so the policy cannot be widened by
    mutation at runtime. Administrators are granted `ALL_PERMISSIONS` **by
    reference** — a newly defined permission reaches them with no edit.
    `permissions_for_role` fails closed (500) for a role with no policy entry.
  - **Authorization service** (`services/authorization.py`): stateless and pure.
    Four checks — role, permission, any, all — each in a boolean (`has_*`) and a
    raising (`require_*`) form. An empty requirement list raises rather than
    silently granting (`require_all_permissions([])`) or denying everyone
    (`require_any_permission([])`).
  - **Dependencies** (`api/authorization.py`): `require_role`,
    `require_permission`, `require_any_permission`, `require_all_permissions`
    factories usable per-route, per-router, or app-wide; plus
    `CurrentPermissions`. Each yields the authorized `User`, so an endpoint need
    not also depend on `CurrentUser`. FastAPI dependencies *are* the permission
    decorator here — they compose with the dependency graph and appear in OpenAPI.
  - **Status codes follow from dependency order:** `CurrentUser` resolves first
    and raises **401**, so an anonymous caller never reaches the permission check.
    Authenticated-but-unauthorized is the only path to **403**.
  - **Errors** (`core/exceptions.py`): `AuthorizationError` (403, `forbidden`,
    generic message) and `AuthorizationConfigurationError` (500, generic
    `internal_error` body with the specifics carried in `detail`). The exception
    handler now logs `detail` and escalates 5xx to error level.
  - **Logging:** `authorization_denied` records user id, role, the rule kind, and
    what was required — correlatable with the response's `request_id`. Never an
    email, name, password, or token.
  - **Endpoints** (`api/v1/authorization/router.py`): `GET /authorization/me`
    (any authenticated caller — describes only their own grants) and
    `GET /authorization/roles` (the role + permission catalog, gated on
    `users:view`). Deliberately the only two: they exercise the 401/403/200
    contract without touching an out-of-scope business domain.
  - **Auth integration:** `UserRead` gained a **computed** `permissions` field, so
    every payload carrying a user — login, refresh, `/auth/me`, change-password —
    exposes the current role *and* its permissions. Computed rather than stored,
    so no row can hold a stale grant and a policy change takes effect at once.
  - **Frontend:** `types/authorization.ts` (permission identifiers mirroring the
    API, the `PERMISSION` constant map, and the shared `AccessRule` shape);
    `lib/authorization/access.ts` (the single rule evaluator);
    `lib/authorization/routes.ts` (path → rule, longest-prefix match, so nested
    routes inherit their section's requirement); `usePermissions` and `useRole`
    hooks; `<Protected>` (page fragments) and `<ProtectedRoute>` (whole pages,
    renders the Unauthorized state in place — it never redirects); `RouteGuard`
    wired into the protected layout so every page is authorized by construction.
    The `access-denied` route and `AccessDenied` component were promoted from
    placeholders into the real Unauthorized page.
  - **Role-aware sidebar:** each nav item declares its `access` rule in
    `config/navigation.ts`; `routeAccessRules` is *derived* from that list and
    feeds both the sidebar filter and the route guard, so "the sidebar never
    offers what the guard would block" holds by construction (and is asserted as
    such in the tests). Sections whose items are all hidden disappear with them.
    No permission is named inside a component.
  - **Validation (live Postgres + Redis, real HTTP):** 377 backend tests
    (`ruff`, `mypy` strict clean) and 153 frontend tests pass; `tsc` and ESLint
    clean; production build succeeds and prerenders all 13 routes. Verified
    against a freshly started API with one user per role: unauthenticated and
    malformed-token requests return **401** with a `WWW-Authenticate: Bearer`
    challenge (never 403); `/authorization/me` reports 23 / 11 / 8 permissions for
    administrator / lawyer / court; `/authorization/roles` returns **200** for the
    administrator and **403** for both restricted roles; the 403 body is
    `{"error":"forbidden"}` with no mention of `users:view` or `administrator`;
    `/auth/me` and the login response both carry `permissions`; the served catalog
    matches `ROLE_PERMISSIONS` exactly. The log shows three `authorization_denied`
    events with user id, role, and `required=['users:view']` — and no email,
    password, hash, or JWT anywhere. Frontend route sweep: all protected routes
    307 to `/login` anonymously, 200 with a session cookie, `/login` 307s a
    signed-in user away; no errors or warnings in the dev-server log.

- **Auth hardening: login throttling + session revocation on password change**
  (follow-up to spec `03`, requested after it shipped). Closes the two gaps that
  were previously logged as open questions:
  - **Failed-login throttling** (`services/login_throttle.py`): after
    `MAX_FAILED_LOGIN_ATTEMPTS` (5) consecutive failures inside
    `LOGIN_FAILURE_WINDOW_MINUTES` (15), login is refused with **HTTP 429** plus a
    `Retry-After` header for `LOGIN_LOCKOUT_MINUTES` (15). Counters are kept in
    Redis (bounded TTLs) for **both** the account and the client IP — either
    tripping blocks the attempt, so it stops single-account guessing *and* one host
    spraying a password across many accounts. Checked **before** credentials are
    verified, so a correct password cannot unlock a locked account and no bcrypt
    work is done for a blocked caller. A success clears the counters, which is what
    makes the threshold apply to *consecutive* failures. Unknown emails are counted
    too, so the lockout is not an account-enumeration oracle. Disabled accounts do
    **not** count (presenting valid credentials is not a guess), so those users keep
    the actionable `account_disabled` message instead of an opaque 429.
  - **Session revocation on password change**: `users.session_generation` (new
    column) is embedded in every token as the `sgen` claim; a token whose
    generation is behind the user's is rejected. A password change increments it,
    invalidating **every** session for that user in one write. The changing device
    is handed a **replacement token pair** (and refresh cookie) so it stays signed
    in — `PATCH /auth/change-password` now returns `ChangePasswordResponse`
    (tokens + `message` + `sessions_revoked`) instead of a bare message. All other
    devices must authenticate again. The caller's outgoing tokens are additionally
    denylisted.
  - **Client IP resolution** (`api/deps.py::get_client_ip`): uses
    `X-Forwarded-For` only when `TRUST_PROXY_HEADERS` is enabled. Trusting it
    unconditionally would let a client spoof the header to evade per-IP throttling,
    or set a victim's address to get *them* locked out. Must be enabled behind
    Nginx.
  - **Frontend:** the login form surfaces the server's lockout message verbatim
    (only the server knows the remaining wait); `ApiError` now parses `Retry-After`
    and exposes `isRateLimited`. `changePassword` swaps in the replacement access
    token — without that the very next request would fail, since the token the call
    was made with is revoked by the call itself — and never retries through the
    refresh path. New `useChangePassword` hook.
  - **Validation (live Postgres + Redis):** 240 backend tests and 111 frontend
    tests pass; `ruff`, `mypy` strict, `tsc`, and ESLint clean; production build
    succeeds. Verified over HTTP: attempts 1–4 return 401 and the 5th returns 429
    with `Retry-After: 900`; the correct password is still refused mid-lockout and
    issues no session; both `auth:login_lock:email:*` and `auth:login_lock:ip:*`
    appear in Redis with bounded TTLs; a success after 4 failures resets the
    counter. For revocation: three devices signed in, one changed the password —
    the other two got 401 on both access **and** refresh, the changing device kept
    working on its new pair, its old pair was rejected, the old password stopped
    working, affected devices re-authenticated successfully, and a second user's
    session was untouched. `session_generation` incremented 0→1→2 in the database.
    No password, JWT, hash, or traceback in the logs.

- **Authentication (spec `03-authentication.md`)** — complete JWT authentication
  establishing **user identity only** (no RBAC, no user management, no
  registration — all deferred by the spec):
  - **Dependencies:** installed `python-jose[cryptography]`, `python-multipart`,
    `email-validator`, and `types-python-jose` (mypy strict) on the backend;
    `react-hook-form`, `zod`, `@hookform/resolvers` plus a Vitest +
    Testing Library test stack on the frontend. `requirements.txt` and
    `package.json` updated.
  - **Config** (`core/config.py`): `JWT_SECRET_KEY`, `JWT_ALGORITHM`,
    `ACCESS_TOKEN_EXPIRE_MINUTES` (15), `REFRESH_TOKEN_EXPIRE_DAYS` (7),
    `JWT_ISSUER`/`JWT_AUDIENCE`, `BCRYPT_ROUNDS`, and the refresh-cookie settings.
    Production validators reject the dev secret, a secret under 32 chars, and a
    non-Secure cookie. All documented in `.env.example`.
  - **User model** (`models/user.py`) + migration `9a0f33933f6d`: `users` table
    with unique indexed email, bcrypt hash, `user_role` enum, `is_active`,
    `last_login_at`, timestamps. The downgrade also drops the Postgres enum type
    so it is a true inverse. Upgrade → downgrade → upgrade verified on live
    Postgres.
  - **Security primitives** (`core/security.py`): bcrypt hashing (with explicit
    rejection of >72-byte passwords rather than silent truncation) and JWT
    sign/verify enforcing signature, expiry, issuer, audience, and a `type` claim
    so an access and a refresh token can never be substituted for one another.
  - **Service layer:** `repositories/user.py` (data access),
    `services/auth.py` (`AuthService`: authenticate, login, refresh, logout,
    change password, token→identity), and `services/token_revocation.py` (Redis
    denylist keyed by `jti` with TTL = the token's remaining life).
  - **Endpoints** (`api/v1/auth/router.py`): `POST /login`, `POST /logout`,
    `POST /refresh`, `GET /me`, `PATCH /change-password` — all under
    `/api/v1/auth`, thin, delegating to the service, with `api/deps.py` providing
    `CurrentUser` via an `HTTPBearer` dependency.
  - **Error handling:** distinct codes in the existing `ErrorResponse` envelope —
    `invalid_credentials` (401), `missing_token`, `invalid_token`,
    `token_expired` (401, so clients know to refresh rather than re-login),
    `account_disabled` (403), `invalid_password` (400) — plus a
    `WWW-Authenticate: Bearer` challenge on 401. Unknown email and wrong password
    return byte-identical responses.
  - **Logging:** structured events for `login_succeeded`, `login_failed` (with
    reason), `logout_succeeded`, `password_changed`, `token_refreshed`,
    `token_rejected`. Verified that no password, JWT, hash, or secret ever
    reaches the log.
  - **Frontend:** real login page + `LoginForm` (Design System components only,
    React Hook Form + Zod), `lib/api/` client with in-memory token storage and
    transparent refresh-and-replay, real `session-store`, `SessionProvider`
    (init / persistence / auto-refresh / auto-logout), `RequireAuth` and
    `RedirectIfAuthenticated` guards, working `UserMenu` sign-out, and
    `proxy.ts` for request-level route protection.
  - **User provisioning:** `scripts/create_user.py` (`python -m
    scripts.create_user`) creates/updates accounts until the admin UI ships —
    the spec forbids self-registration.
  - **Validation (live infra + both servers running):** 168 backend tests
    (`ruff`, `mypy` strict clean) and 81 frontend tests pass; production build
    succeeds; `tsc` and ESLint clean. Verified against real Postgres + Redis:
    login issues a token pair and an httpOnly cookie; `/me` works and rejects
    missing/malformed/expired/revoked/wrong-type tokens; refresh rotates and
    replay of the consumed token is rejected (denylist entry confirmed in Redis
    with a bounded TTL); logout kills both tokens and clears the cookie;
    change-password works and invalidates the old password; passwords are stored
    as `$2b$` hashes. Route protection verified over HTTP in both directions,
    and cross-origin CORS-with-credentials confirmed between `:3000` and `:8000`.

- **Backend Foundation (spec `02-backend-foundation.md`)** — FastAPI application
  infrastructure in `apps/api` (no business logic; infrastructure only):
  - **Dependencies:** installed `pydantic-settings`, `redis`, `minio`,
    `qdrant-client`, `structlog`, and dev tools `pytest`, `pytest-asyncio`,
    `httpx`, `ruff`, `mypy` into the project `.venv`. Rewrote the previously
    UTF-16/partial `requirements.txt` as a clean, categorized, pinned UTF-8 file.
  - **FastAPI app** (`apps/api/main.py`): application factory (`create_app`),
    API versioning (`/api/v1` via `settings.API_V1_PREFIX`), router registration
    (system router at root + empty `api/v1/router.py` aggregate for future
    features), lifespan events, middleware, exception handlers, Swagger (`/docs`)
    + ReDoc (`/redoc`) + `/openapi.json` (toggleable via `ENABLE_DOCS`).
  - **Configuration** (`core/config.py`): `pydantic-settings` `Settings` loaded
    from env / `.env`, `Environment` enum (development/production/testing),
    computed `DATABASE_URL` (psycopg driver) + `REDIS_URL`, and fail-fast
    production invariants (no DEBUG, no wildcard `ALLOWED_HOSTS`, no default
    DB/MinIO secrets). Cached singleton `settings`. `.env.example` documents all
    variables.
  - **Logging** (`core/logging.py`): `structlog` structured logging (JSON in
    prod/test, console in dev), stdlib bridge so uvicorn/SQLAlchemy share the
    format, configurable `LOG_LEVEL`. No `print()` anywhere.
  - **Database** (`db/`): SQLAlchemy 2.0 `Engine` with pooling + `pool_pre_ping`
    + bounded `connect_timeout` (`db/session.py`), `SessionLocal`, `get_db`
    dependency, `check_database_connection`, `dispose_engine`; declarative
    `Base` with a constraint naming convention (`db/base.py`). No business
    models/tables.
  - **Alembic** (`apps/api/alembic.ini` + `db/migrations/`): `env.py` resolves
    the URL from settings and targets `Base.metadata`; timestamped migration
    template; empty `versions/`. Offline (`--sql`) run verified.
  - **Infrastructure clients** (`core/`): Redis pooled client
    (`core/cache.py`), MinIO client with a bounded urllib3 http client
    (`core/storage.py`), Qdrant client with `check_compatibility=False`
    (`core/vector.py`) — each with a fail-fast health check and no
    business/caching/bucket/collection logic.
  - **Middleware & errors:** CORS + `TrustedHostMiddleware` +
    `RequestLoggingMiddleware` (per-request `X-Request-ID`, timing, structured
    access logs). Global exception handlers (`core/exceptions.py`) return a
    consistent `ErrorResponse` envelope (`schemas/errors.py`) and never leak
    stack traces; unhandled errors are logged and returned as generic 500.
  - **Health endpoints** (`api/health.py`): `GET /health` (liveness),
    `GET /ready` (concurrent dependency probes via `core/readiness.py`, 200/503),
    `GET /version`. Response models in `schemas/health.py`.
  - **Tooling** (root `pyproject.toml`): `ruff`, `mypy` (strict), and `pytest`
    (`pythonpath=apps/api`, `asyncio_mode=auto`) configuration.
  - **Tests** (`tests/`): `conftest.py` (forces testing env + `TestClient`
    fixture), integration tests for health/version/ready/docs/404-envelope,
    unit tests for settings + production validation. 11 passed.
  - **Infrastructure (`docker-compose.yml` + `.env`):** populated the previously
    empty `docker-compose.yml` with PostgreSQL 16, Redis 7, MinIO, and Qdrant
    (named volumes + healthchecks). Fixed the Qdrant healthcheck to use bash
    `/dev/tcp` against `/healthz` because the `qdrant/qdrant` image ships no
    `curl`/`wget` and its `sh` is dash (no `/dev/tcp`). Added a local `.env`
    (git-ignored intent) whose credentials match the compose services —
    notably `MINIO_SECRET_KEY=minioadmin123` to match `MINIO_ROOT_PASSWORD`.
  - **`.env` path fix:** `Settings.model_config.env_file` now resolves to an
    absolute repo-root path (`Path(__file__).parents[3] / ".env"`) so the same
    `.env` loads whether the process runs from `apps/api` (uvicorn/Alembic) or
    the repo root (pytest). Previously the relative `".env"` was missed when
    running from `apps/api`, causing MinIO auth (`SignatureDoesNotMatch`) to fail.
  - **`.env` / `.env.example` reconciled:** both now carry the **same keys**;
    `.env.example` is the committed copy-to-work template (no real secrets, and
    `MINIO_SECRET_KEY=minioadmin123` matching the compose stack so
    `cp .env.example .env` works out of the box), `.env` is the git-ignored local
    copy actually loaded at runtime. Added a repo-root `.gitignore` that excludes
    `.env` (keeps `.env.example`), `.venv/`, caches, and `node_modules/`.
  - **Config parsing robustness:** list fields (`CORS_ORIGINS`, `ALLOWED_HOSTS`)
    use `Annotated[..., NoDecode]` so they accept a plain comma-separated string
    from `.env` (pydantic-settings otherwise JSON-decodes complex fields before
    validators run); optional secrets (`REDIS_PASSWORD`, `MINIO_REGION`,
    `QDRANT_API_KEY`) coerce a blank `.env` value to `None`.
  - **Validation (live infra, all four services up via Docker Compose):** app
    boots cleanly under uvicorn — startup log shows `dependency_connected` for
    postgres/redis/minio/qdrant, no warnings; `/health` 200, `/version` 200,
    `/ready` **200 with all dependencies `up`** (and 503-with-breakdown when a
    dependency is down, verified separately), `/docs` + `/redoc` +
    `/openapi.json` 200, unknown route → consistent 404 envelope with
    `X-Request-ID`. `ruff` clean, `mypy` clean (21 files), 11/11 tests pass,
    Alembic connects to the live PostgreSQL (`alembic current`, no revisions yet)
    and runs offline (`--sql`). All four compose containers report `healthy`.

- **Application Shell (spec `01-application-shell.md`)** — reusable responsive
  shell on top of the design system (no business logic; mocked placeholder data
  only):
  - **Routing (App Router + route groups):** `app/(auth)/` (public) with a
    centered `AuthLayout` and a `/login` placeholder; `app/(protected)/` with the
    app-shell layout wrapping placeholder pages for Dashboard, Cases, Documents,
    Lawyers, Court Updates (`/court`), Reports, Notifications, AI Assistant
    (`/ai`), Settings, plus a placeholder `access-denied` route. Root `/`
    redirects to `/dashboard` via a `next.config.ts` redirect (HTTP 307).
  - **Providers** (`components/providers.tsx`): composes Theme (next-themes,
    forced dark) → React Query (`@tanstack/react-query`) → Tooltip (Radix) →
    Toaster (`sonner`). Root layout reduced to a thin server component rendering
    `<Providers>`.
  - **Shell components** (`components/layout/`): `AppShell`, `AppSidebar`
    (desktop rail with collapse + mobile `Sheet` drawer), `AppHeader`
    (sticky top nav), `AppBrand`, `SidebarNav` (active-route highlighting),
    `Breadcrumbs` (auto-generated from pathname), `PageContainer`, `PageHeader`,
    `AppFooter`, and placeholders `UserMenu`, `NotificationButton`, `SearchBar`.
  - **Shared state components** (`components/shared/`): `Spinner`,
    `LoadingState`, `PageSkeleton`, `EmptyState`, `ErrorState`, `AccessDenied`.
  - **Special files:** `app/loading.tsx`, `app/not-found.tsx` (404),
    `app/error.tsx` + `app/global-error.tsx`, and protected-scoped
    `loading.tsx` (skeleton) / `error.tsx`.
  - **Global state:** `stores/sidebar-store.ts` (zustand — collapsed + mobile
    drawer), `stores/session-store.ts` (mocked placeholder user, **not** auth),
    hooks `use-current-user`, `use-theme-mode`, `use-active-route`.
  - **Utilities:** `lib/routes.ts` (route constants), `config/navigation.ts`
    (sidebar config + route→label map), `lib/breadcrumbs.ts` (pathname → trail),
    `lib/metadata.ts` (per-page metadata helper).
  - **Accessibility:** skip-to-content link, `<main>`/`<nav>`/`<aside>`
    landmarks, `aria-current` on active nav + breadcrumb, focus-visible rings,
    keyboard-operable drawer/menus.
  - **Validation passed:** production build succeeds, `tsc --noEmit` clean,
    ESLint clean, all 11 routes return 200, `/` → 307 `/dashboard`, dark theme
    present in SSR output, no runtime errors/warnings in the server log.

- **Design System (spec `00-design-system.md`)** — shared UI foundation for
  `apps/web`:
  - Bootstrapped the Next.js (App Router) + TypeScript strict-mode frontend
    (`package.json`, `tsconfig.json`, `next.config.ts`, `postcss.config.mjs`,
    `eslint.config.mjs`).
  - Tailwind CSS v4 configured via `@tailwindcss/postcss`; `tw-animate-css`
    for component animations.
  - `app/globals.css` declares the platform color tokens from `ui-context.md`
    once and maps shadcn semantic tokens (`--background`, `--primary`,
    `--border`, `--ring`, chart + sidebar tokens, plus `--success`/`--warning`/
    `--error`/`--info` utilities) onto them. No custom colors introduced.
  - Dark-mode-only theme: `:root` and `.dark` carry an identical dark palette;
    the root layout hardcodes the `dark` class and the theme provider uses
    `forcedTheme="dark"`, so no light theme can render.
  - Theme provider (`components/theme-provider.tsx`, next-themes wrapper) and
    root layout (`app/layout.tsx`) with Geist Sans/Mono fonts and a global
    `TooltipProvider`.
  - `lib/utils.ts` `cn()` helper (clsx + tailwind-merge) — verified to resolve
    conflicting Tailwind utilities last-wins.
  - All 22 required shadcn/ui components generated into `components/ui/` via
    the shadcn CLI (Button, Card, Dialog, Dropdown Menu, Input, Label, Select,
    Separator, Sheet, Skeleton, ScrollArea, Tabs, Textarea, Tooltip, Avatar,
    Badge, Alert, AlertDialog, Checkbox, Command, Popover, Table).
  - `app/page.tsx` was a design-system reference page exercising a
    representative component set (validated rendering + theme). *(Removed by
    spec `01`; `/` now redirects into the app.)*
  - **Validation passed:** production build succeeds, `tsc --noEmit` clean,
    ESLint clean, dark theme confirmed in SSR output, no hydration mismatch,
    `cn()` verified.

## In Progress

- None.

## Next Up

- **Change-password UI** — the backend endpoint, API client, validation schema, and
  `useChangePassword` hook are all in place and tested, but no settings screen wires
  them to a form yet (spec `03` only required a login page). A form should also tell
  the user that other devices were signed out, using the `sessions_revoked` flag.
  **User Management raised the stakes:** a password reset now sets
  `must_change_password`, which every user payload carries — but with no
  change-password screen, a user who receives a temporary password has nowhere in
  the UI to replace it. See the open question below.
- **Profile image upload — now unblocked.** `users.profile_image` stores a
  location and the UI renders it, but nothing uploads one yet. This was waiting on
  MinIO integration, which Document Management has now built
  (`services/document_storage.py`, plus the filename and type policy in
  `core/documents.py`). An avatar is **not** a case document, so it should not
  reuse the `documents` tables — but it can reuse the storage service and the
  validation helpers against an `avatars` bucket. Until then avatars fall back to
  initials, which is what nearly every row shows.

## Open Questions

- **No cleanup job exists yet for archived document files — by design, but it is
  now owed.** Deleting a document is logical: the row keeps `deleted_at` and every
  stored object stays in MinIO, which is exactly what the spec and
  `code-standards.md` require ("do not immediately remove the file", "never
  permanently delete legal documents without authorization"). The spec then says
  *"future cleanup jobs can permanently remove archived files"*, and that job does
  not exist. Consequence: storage grows monotonically, including superseded
  versions and objects orphaned by a metadata write that failed after a successful
  upload. `services/document_storage.py` logs
  `document_object_logically_deleted` (with the key) precisely so such a job has a
  record to work from. **What product needs to decide is the retention period** —
  how long a deleted document and its versions must remain recoverable before the
  bytes may go. `apps/worker/cleanup_worker.py` is the empty placeholder it belongs
  in.

- **Court representatives cannot replace a document, and lawyers cannot either.**
  `documents:update` and `documents:delete` are administrator-only, which matches
  the spec's per-role lists exactly (lawyers and court representatives are granted
  upload, view, and download and nothing more). The consequence is that a lawyer
  who uploads the wrong file must ask an administrator to replace it, or upload a
  second document. Widening it is a one-line policy change in `core/roles.py` —
  and "may replace a document **they** uploaded" would need a new per-resource
  rule rather than a permission. **Flagged rather than decided, because the spec's
  role lists are explicit.**

- **The upload ceiling is enforced after the body is received.** `MAX_DOCUMENT_SIZE_MB`
  (25) is checked once Starlette has parsed the multipart body into a spooled
  temporary file, because that is the first point at which the length is known.
  A caller can therefore make the server buffer an arbitrarily large upload before
  it is refused. The correct outer guard is at the edge — `client_max_body_size`
  in Nginx — which `.env.example` now says explicitly. **It must be configured when
  the reverse proxy is set up**; until then, only the application check applies.

- **File-type validation checks the leading bytes, not the whole file.** The
  "corrupted uploads" rule compares the first 512 bytes against the format's
  signature, which catches a truncated transfer, a zero-padded placeholder, and a
  renamed executable. It does **not** catch a valid PDF with a malicious payload
  inside it, and it is not meant to: content is never executed, previews are
  sandboxed with a `default-src 'none'` CSP, and the served MIME type comes from
  the extension mapping rather than from the bytes. **Antivirus scanning is not in
  scope for this feature** and would belong with the background workers.

- **`category` is free text, not an enumeration — product decision needed.** The
  spec names the field but defines no set of categories, and
  `ai-workflow-rules.md` forbids inventing business behaviour, so it is stored as
  a trimmed string (max 100 characters) and the form offers a plain input.
  Consequences while it stays free text: two administrators can spell the same
  category differently, and there is no "filter by category" (the spec's filter
  list does not include one either). Promoting it to an enum later is a migration
  plus a `<Select>`, not a redesign. **A list of categories from product would
  settle it.**

- **"Force password change" is signalled, not enforced — product decision needed.**
  The spec asks to "support forcing password change during the next
  authentication". What is implemented: a reset sets `must_change_password`, the
  flag rides on every user payload (login, refresh, `/auth/me`), and changing the
  password clears it. What is **not** implemented: blocking API access until the
  password is changed. Enforcing it now would lock a reset user out of the whole
  platform, because **no change-password screen exists yet** (see Next Up) — they
  would have a valid session and no way to satisfy the requirement. The strict
  reading should be adopted *together with* that screen: reject every request
  except `/auth/me` and `/auth/change-password` while the flag is set, and have
  the client redirect on it. Flagged so this is not mistaken for finished work.

- **The temporary password is returned in the API response.** With no email
  service (out of scope), an out-of-band channel does not exist, so the
  administrator is handed the password to relay themselves. It is shown once, only
  its hash is stored, and it is never logged — but it does pass through the
  administrator's browser. When Email Notifications ship, the better design is to
  mail a **single-use reset link** to the user and return nothing to the
  administrator. Worth revisiting then.

- **`/lawyers` is still a placeholder, and its purpose has narrowed.** It was
  described as "the case-facing view of lawyers and their assignments, which
  belongs to Case Management". Case Management shipped without it, deliberately:
  the spec's scope is the Case entity, and "who is on this case" is answered on
  the case itself while "which cases is this lawyer on" is answered by the
  existing `?assigned_lawyer_id=` filter on `/cases`. **What remains for
  `/lawyers` is a per-lawyer workload view** (their caseload, upcoming hearings,
  capacity). If product does not want one, deleting the nav item removes it from
  the sidebar and the route guard automatically. Its provisional `users:view`
  gate is also now wrong for that purpose and should become `cases:view`.

- **Per-resource authorization — RESOLVED by Case Management.** Implemented in
  `services/case_access.py`: `cases:view-all` lifts the row restriction, and
  every other holder of `cases:view` is scoped **in the SQL query** to the cases
  they are assigned to. Applies to reading, updating, and archiving alike. See
  the Case Management entry under Completed.

- **`hearings:*` permissions — RESOLVED differently than anticipated.** Court
  representatives no longer ride on the full `cases:update`; they hold
  `cases:update-hearing`, which reaches only the court-facing fields (court name,
  filing date, next hearing date, status). It sits in the `cases` group rather
  than a new `hearings` one because there is no Hearing entity yet — these are
  fields *of a case*. **When Hearing Management ships as its own entity, a
  `hearings:*` group belongs with it**, and `cases:update-hearing` should be
  reviewed then.

- **Baseline permissions were a judgement call.** `notifications:view` and
  `settings:view` are granted to every role even though the spec's per-role lists
  do not mention them, because invariant 3 and `ui-context.md` both assume every
  user sees their own notifications and settings. If the intent was genuinely
  "lawyers cannot open the Notifications page", removing them from
  `BASE_PERMISSIONS` in `core/roles.py` is a one-line change — the sidebar and
  route guard follow automatically. **Product confirmation would settle it.**

- **No UI assigns roles to users — RESOLVED:** the User Management create and edit
  dialogs assign roles, and `scripts/create_user.py` is now the bootstrap path
  only (creating the first administrator, before an account exists to authorize
  that call).

- **Login rate limiting — RESOLVED:** implemented as a Redis-backed throttle
  (5 consecutive failures / 15-minute window → 429 for 15 minutes, per account and
  per IP). See the hardening entry under Completed.

- **Password-change session policy — RESOLVED:** the chosen policy is *invalidate
  every session, but keep the current one alive via a replacement token pair*, so
  the user is not signed out of the device they just used. Implemented with a
  per-user `session_generation` counter. See the hardening entry under Completed.

- **Lockout is per account+IP, not per device (accepted limitation):** an attacker
  who can reach the API from many addresses can still lock a known account out of
  its own login for 15 minutes at a time by failing 5 attempts — a targeted
  nuisance-DoS inherent to account lockout. Mitigations if this becomes a concern:
  progressive delays instead of a hard block, CAPTCHA after N failures, or
  notifying the account owner. **Not currently a product requirement.**

- **Live datastore validation — RESOLVED:** `docker-compose.yml` was populated
  (Postgres 16, Redis 7, MinIO, Qdrant) and all four services were brought up
  and verified. The app's `/ready` returns 200 with every dependency `up`, and
  the startup log shows all four connected. (Compose stack provided by the user;
  the Qdrant healthcheck was fixed to not depend on `curl`.)

- **Theme scope conflict (unresolved):** `ui-context.md` states the platform
  supports both light and dark (dark default), while `00-design-system.md`
  mandates **dark mode only** ("No light theme appears"). This iteration
  shipped **dark only** per the spec. The provider is a thin next-themes
  wrapper and `globals.css` isolates the palette, so a light theme can be added
  later with minimal rework. **Product decision needed** on whether to
  reconcile `ui-context.md` down to dark-only or plan a future light theme.

## Architecture Decisions

### User Management (spec `05`)

- **`full_name` was split into `first_name` / `last_name`, with the display name
  derived.** The spec's entity lists both parts, and the platform searches and
  sorts on them independently — "sort by name" means family name in a directory.
  Storing the composed name *as well* would let the two disagree after an edit, so
  `User.full_name` is a property. Every existing consumer (`UserRead`, the
  frontend's `SessionUser.name`) is unchanged, and the migration backfills by
  splitting on the first space.
- **`is_active` became `status`, and survives as a derived property.** A boolean
  cannot express "suspended", and keeping both would allow a row where the two
  disagree about whether sign-in is permitted. `User.is_active` now reads
  `status is UserStatus.ACTIVE`, so **authentication was not modified at all** —
  it still asks one question, and a future status cannot accidentally grant
  sign-in.
- **Soft delete *is* the inactive status.** A separate `deleted_at` would create a
  second source of truth about whether an account works, and the first bug would
  be a row that is deleted but still active. `DELETE /users/{id}` sets
  `status = inactive`; reactivation is an ordinary `PATCH`.
- **Deactivation and password reset revoke sessions immediately** by incrementing
  `session_generation` — the mechanism a password change already uses. Without it
  a user disabled for cause keeps working until their access token expires, which
  is precisely the window an administrator is trying to close.
- **Audit fields are populated from the authenticated caller, never from the
  request.** `UserCreate`/`UserUpdate` use `extra="forbid"`, so a client cannot
  supply `created_by` and claim someone else made the change. A test asserts the
  attempt is a 422.
- **`UserUpdate` distinguishes "omitted" from "null" via `exclude_unset`.** A
  plain `model_dump` would send every field, so a PATCH that changed a name would
  silently wipe the phone. The frontend mirrors this by sending a **diff**: a
  dialog that echoed every field would also overwrite a concurrent edit by another
  administrator with values it loaded before that edit happened.
- **The password is absent from `UserUpdate` entirely.** Changing one must revoke
  sessions, which is not something a profile edit should do as a side effect — so
  it has its own endpoint, and the field does not exist to be forgotten.
- **404 and 409 are informative, unlike the 403s.** The caller has already proved
  both who they are and that they may manage users, so naming the problem helps
  them fix it and reveals nothing they could not learn from the list endpoint they
  are entitled to use. This is the opposite of the RBAC decision above, and
  deliberately so — the two answer different questions.
- **An administrator cannot disable or demote themselves.** Not in the spec, and
  recorded as a judgement call: the alternative is an administrator who cannot
  undo it and, if they are the last one, a platform recoverable only by running a
  script on the server. Scoped as narrowly as possible — only `role` and `status`,
  only on one's own account, and re-submitting an unchanged value is not a change.
- **Search, filtering, sorting, and pagination run in the database.** Fetching and
  filtering in Python would make every page cost the size of the whole directory.
  The count is taken over the filtered set before pagination, from the same filter
  clause, so it cannot drift from the rows when a filter is added later.
- **Every ORDER BY ends with the primary key.** Users tying on a sort value —
  everyone who has never signed in — would otherwise come back in an arbitrary
  order per request, duplicating or skipping rows across page boundaries. This is
  invisible until the directory outgrows one page.
- **LIKE wildcards in a search term are escaped.** An unescaped `%` matches every
  user, which reads as a broken filter rather than as the injection-shaped bug it
  is. Verified over HTTP: searching `%` returns zero results.
- **The list query state lives in one hook, not in the page.** `useUserListQuery`
  owns the rule that *any* change except the page itself resets to page 1 —
  otherwise typing a search while on page 4 requests the fourth page of a
  two-page result and shows an empty table. Spread across individual control
  handlers, that rule gets reintroduced as a bug.
- **The `proxy.ts` protected-route list is derived from `ROUTES`, not written
  out.** The hand-maintained version shipped with `/users` missing, serving the
  app shell to anonymous visitors — a list that must be updated in lockstep with
  every new feature fails **open** when someone forgets. A test now asserts every
  route in `ROUTES` outside the two public ones redirects.
- **The password policy lives in `schemas/password.py`.** `schemas.auth` imports
  `UserRead` from `schemas.user`, so putting the shared `NewPassword` type in
  either would be an import cycle; a third module lets both enforce one
  definition, and `schemas.auth` re-exports it so existing importers are
  unaffected.
- **The user details page fetches client-side.** The access token lives in browser
  memory only, so a server render has no credential to call the API with.
  Authorization is unaffected: `RouteGuard` gates `/users/*` through the `/users`
  rule by longest-prefix match, and the API authorizes the request itself.
- **`useWatch` rather than `watch()` in the dialogs.** `watch()` returns a
  function the React Compiler cannot memoize, so it skips the whole component;
  `useWatch` is a real hook and additionally limits re-renders to the named fields
  instead of every keystroke.
- **jsdom polyfills were added to `tests/setup.ts`** (`ResizeObserver`, pointer
  capture, `scrollIntoView`). Radix's Select, Checkbox, and Dropdown Menu measure
  elements and capture pointers; jsdom implements no layout engine and only part
  of the Pointer Events API, so they throw on mount. Nothing under test depends on
  real geometry.

### Case Management (spec `06`)

- **A permission grants a capability; `cases:view-all` grants the rows.** The
  spec's "lawyers view assigned cases" is a *per-resource* rule, and RBAC
  deliberately deferred it. Expressing "sees everything" as a capability rather
  than as `if user.role is ADMINISTRATOR` keeps the rule out of the enforcement
  code — a future supervising role is admitted by editing policy, and
  `code-standards.md`'s "do not hardcode role names" holds all the way down.
- **The scope is applied in SQL, not in Python.** Filtering after the query would
  mean fetching the whole caseload to hide most of it, and — worse — the
  pagination total would count cases the caller is not entitled to know exist.
  `visibility_scope()` returns a user id or `None`, and the repository ANDs it
  into both the page query and the count, built from the same clause.
- **Write access is decided per field, and a partial write is never performed.**
  `cases:update` covers the case, `cases:update-hearing` the court-facing fields,
  `cases:assign` the two assignment fields. `FIELD_PERMISSIONS` records only the
  *exceptions*, so a field added to `CaseUpdate` without an entry defaults to the
  strictest rule rather than arriving ungoverned. If any one field is out of
  reach the whole request is refused — a court representative who submits a full
  case form must not silently have half of it applied.
- **Court representatives were narrowed from `cases:update` to
  `cases:update-hearing`.** RBAC had provisionally given them the full update
  because no better permission existed; that also let them rewrite a case's title
  and description, which their role description ("update hearing-related
  information") does not cover. Three existing tests documenting the provisional
  policy were updated with the reason.
- **Lawyers gained `cases:update`,** scoped to their assigned cases by the
  per-resource check rather than by the permission — which is exactly the shape
  the RBAC decisions predicted.
- **Archiving *is* the `archived` status.** A separate `deleted_at` would be a
  second source of truth about whether a case is live, and the first bug would be
  a case that is archived but still open. Same reasoning as User Management's
  soft delete, and it satisfies "archived cases remain searchable" for free:
  archived cases stay in the list and the search index because nothing filters
  them out.
- **Transitions are data, and the legal ones are served to the client.**
  `STATUS_TRANSITIONS` is a read-only mapping in `core/cases.py`; `CaseRead`
  exposes `allowed_transitions` as a **computed** field. The edit dialog renders
  what the server sent, so the UI cannot offer a move the API is about to refuse,
  and a policy change reaches the menu without a frontend release. Re-submitting
  the current status is not a transition, so a form that round-trips every field
  still saves.
- **Priority ordering lives in `PRIORITY_RANK`, and the SQL is built from it.**
  Sorting on the stored value gives high, low, medium, urgent — alphabetical and
  meaningless. One definition feeds the `ORDER BY` and any future report. It must
  be a **searched** `CASE WHEN priority = …`, not the `case({...}, value=…)`
  shorthand: the shorthand binds its keys as `VARCHAR`, which PostgreSQL will not
  compare to a `case_priority` column. SQLite accepts both, so only a live
  database — or the dialect-compiled regression test now guarding it — can tell
  them apart.
- **Case numbers are unique in the database, not only in the service.** The
  service checks first so a client gets a clean 409, but the generated series is
  a read-then-write and two simultaneous creations can pick the same sequence.
  The unique index is what actually guarantees uniqueness; the service retries
  the `IntegrityError` (rolling back first — a failed flush leaves the session
  unusable) up to five times before failing as a 500.
- **A registry number cannot disturb the generated series.**
  `case_number_sequence` only parses `CASE-YYYY-NNNN`, so filing `TC/2026/9999`
  does not advance the platform's counter. Numbers are zero-padded, which is what
  makes "sort by case number" chronological without a second numeric column, and
  uppercased, so the same reference cannot be filed twice in different casings.
- **Assignments are validated against the assignee's role and status, but only
  when they change.** A court representative in the lawyer position would hold
  the lawyer's access without the role that carries it. Re-validating an
  *unchanged* assignment would make a case whose lawyer was later deactivated
  impossible to edit in any other respect.
- **Assignees and auditors are returned as people, not identifiers, through a
  narrow `CaseUserSummary` — deliberately not `UserRead`.** A case is readable by
  lawyers and court representatives, who hold no `users:view`; embedding the full
  directory record would hand them account status, audit trail, and the
  assignee's effective permissions through a side door.
- **Relationships are `lazy="selectin"`.** Those names are needed on every read,
  so a default lazy load would be one query per case per relationship — the
  classic N+1. `selectin` batches a whole page into four extra queries whatever
  its size, and unlike an explicit `joinedload` it cannot be forgotten at a call
  site.
- **403, not a concealing 404, for a case the caller may not reach.** Case ids
  are random UUIDs, so answering honestly enables no enumeration, and a lawyer
  following a colleague's link needs to know the case exists and that they should
  ask to be assigned. A 404 would read as a broken link. The body stays generic —
  it never says *which* permission or assignment would have admitted them.
- **The assignment endpoint delegates to the one update path.**
  `PATCH /cases/{id}/assignments` converts its body into a `CaseUpdate` and calls
  `update_case`, so the validation, audit, and logging cannot drift from the
  general endpoint — "do not duplicate business logic", enforced structurally.
- **`category` is free text.** The spec names the field but defines no set of
  categories, and inventing one is exactly what `ai-workflow-rules.md` forbids.
  Recorded as an open question above.
- **The status and priority *labels* live in one map each on both sides.** The
  API sends identifiers; `CASE_STATUS_LABELS` / `CASE_PRIORITY_LABELS` render
  them. When next-intl lands they become translation keys and nothing else moves.
- **`refineDateOrder` is a function applied to each schema, not a generic
  wrapper.** A `withCoherentDates<T extends ZodTypeAny>(schema)` helper erases the
  object's output type, which silently turns `z.infer` into `any` and costs every
  form its field typing — caught by `tsc` on the first build.

### Document Management (spec `07`)

- **Two tables: a current-state row and an immutable version history.** The
  alternative — one row per version, with the "document" being the newest of a
  group — makes every list query a "latest version per group" problem, which is a
  window function or a correlated subquery on the hot path, and makes the
  document's identity a synthetic group key rather than a real primary key. This
  shape keeps `documents` **exactly the entity the spec enumerates**, keeps its
  `id` stable across replacements so existing links work, and lets search, the
  type filter, and the size sort run against one table with no join. The cost is a
  denormalization: the document's binary columns mirror its current version. That
  has one writer (`DocumentService`), one rule with no exceptions ("the document
  row describes its current version"), and a test asserting the mirror holds after
  a replacement.
- **The version number comes from the history, never from `documents.version`.**
  A version row is never deleted, so `max(version) + 1` cannot collide with a
  number already issued — whereas the current-state column could, if a future
  feature ever reverted it. The unique `(document_id, version)` constraint is what
  actually guarantees it under concurrency; the service's read is the fast path.
- **The storage key contains the version, so "never overwrite" is structural.**
  `cases/{case}/documents/{document}/v{n}/{generated}.ext` cannot address a
  predecessor, which means the guarantee holds even if someone later writes a code
  path that forgets it. The generated filename is a fresh UUID rather than
  anything derived from the upload: two users filing `contract.pdf` on one case
  must not contend for a key, and the layout must not be influenceable by a
  crafted name.
- **Storage is written before the metadata is committed.** The reverse order can
  produce a committed row pointing at an object that was never written — a
  document that exists and cannot be downloaded, which no retry repairs. This
  order can only produce an *unreferenced object*, which is a storage cost and is
  exactly what the cleanup job the spec anticipates is for. The failure path logs
  the orphaned key rather than deleting it, because the storage service has no
  physical delete by design.
- **`DocumentStorageService.delete_object` deliberately deletes nothing.** It is
  not a stub: the spec says "do not immediately remove the file from MinIO",
  `code-standards.md` forbids permanently deleting a legal document without
  authorization, and `architecture.md` invariant 6 makes an uploaded document
  immutable. Exposing a real physical delete would put a destructive operation one
  call away from every future feature. What the method does instead is leave the
  audit record a cleanup job will need.
- **Document access delegates to case access rather than restating it.**
  `DocumentAccessPolicy` holds no rule of its own — a document is reachable
  exactly when its case is. Two copies of that predicate would be one policy
  change away from disagreeing about who can see what, so the SQL half was
  extracted as `assigned_case_scope` in `repositories/case.py` and the Python half
  is a straight delegation to `CaseAccessPolicy`. The module exists so the
  delegation is stated once and testable as the invariant it is.
- **The MIME type comes from the extension, never from the client.** The
  browser's `Content-Type` on a multipart part is attacker-controlled, and it is
  the value that would decide how the preview endpoint's response is rendered —
  which is how an "image" gets served as HTML from the platform's own origin.
  `EXTENSION_MIME_TYPES` is the only source, and `ALLOWED_DOCUMENT_EXTENSIONS` can
  only intersect with it, so configuration can narrow the policy but never widen
  it past what the platform can safely serve.
- **Preview is a separate endpoint from download, and answers 415 rather than
  falling back.** The document exists and the request is well-formed; it is the
  *representation* that does not. Making preview silently serve an attachment
  would leave a client unable to tell whether its inline viewer failed. The
  computed `is_previewable` on every document is what stops the UI from offering
  the action in the first place — the same reasoning as a case's
  `allowed_transitions`.
- **Files are fetched as blobs on the client, not linked to.** The access token
  lives in memory and travels as an `Authorization` header, so a plain `<a href>`
  or `<iframe src>` pointed at the API arrives anonymous and is refused. Every
  download and preview is an authenticated request whose blob becomes an object
  URL, revoked as soon as it is consumed — an object URL pins the whole file in
  memory until it is.
- **Uploads use `XMLHttpRequest` while everything else uses `fetch`.** A
  deliberate, contained deviation: `fetch` fires no upload-progress events and
  streaming request bodies are not broadly available, so "display upload progress"
  is not implementable with it. `lib/api/upload.ts` is the only module that knows
  this, and it reuses the same credential, cookie handling, error envelope, and
  refresh-once-and-replay semantics as `lib/api/client.ts`.
- **`category` is a PostgreSQL enum, unlike a case's free-text `category`.** The
  spec *enumerates* the document categories, so inventing nothing is possible
  here — which is exactly why the case field stayed free text. Extending it is one
  enum member plus a one-line `ALTER TYPE`, and the sort order follows the
  declaration order automatically.
- **The upload form's fields are declared individually on the endpoint.**
  `Annotated[DocumentUploadForm, Form()]` is not flattened by FastAPI when the
  same request also carries a separate `File` part; the model arrives as one
  missing field called `payload` and every upload is a 422. Verified in isolation.
  The rules still live in `schemas/document.py` — the endpoint assembles the model
  and re-raises a Pydantic failure as FastAPI's own validation error, so a bad
  description reaches the client in the standard envelope with the field named.
- **Deletion is idempotent, unlike the read paths.** A second `DELETE` succeeds
  and preserves the original timestamp, matching how case archiving behaves, while
  `GET` on a deleted document is a 404 — the row survives so the deletion is
  recoverable, not so the API can still serve it.
- **`documents:update` and `documents:delete` stayed administrator-only.** The
  spec's per-role lists grant lawyers and court representatives upload, view, and
  download and nothing else, so replace and delete are theirs alone. Recorded as an
  open question rather than quietly widened.

### Authorization / RBAC (spec `04`)

- **Permissions, not roles, are the unit of enforcement.** Every guard names a
  capability (`cases:view`); nothing outside `core/roles.py` branches on a role.
  Role checks *are* supported (`require_role`) because the spec asks for them, but
  they are documented as the fallback: a role check hard-codes policy at the call
  site and must be revisited whenever the role model changes, whereas a
  capability outlives it. This is what makes "these permissions will be refined by
  future features" a policy edit rather than a code migration.
- **`UserRole` stays in `models/user.py`; only the policy is new.** The role enum
  is persisted, so the storage definition is the canonical one. Duplicating or
  moving it to satisfy the "centralized role definitions" bullet would have
  created two sources of truth — the constraint "do not rename existing files"
  points the same way. `core/roles.py` re-exports it so authorization code has one
  import site.
- **Administrators are granted `ALL_PERMISSIONS` by reference, not by copy.** A
  new permission is theirs the moment it is defined. Copying the set would make
  "administrator has full access" quietly false for every permission added later —
  the exact failure mode that produces an admin who cannot use a new feature.
- **Permissions are computed from the role, never stored on the user.** A
  `permissions` column would need backfilling on every policy change and could
  hold a grant the policy no longer allows. `UserRead.permissions` is a Pydantic
  computed field, so the wire payload is always the live policy. (Per-user
  overrides, if ever needed, would be an *addition* to the role's set — the shape
  supports it without changing this decision.)
- **A permission grants a capability, not a row.** `cases:view` means "may use the
  case-viewing feature", not "may view every case". The spec's "assigned cases
  only" rule is per-resource and needs data that does not exist yet (assignments);
  it belongs to Case Management, which will check assignment *on top of*
  `cases:view`. Implementing it now would mean inventing the assignment model.
- **Two permissions are granted to every role as a baseline**
  (`notifications:view`, `settings:view`). The spec's per-role lists describe
  business-resource access and omit them, but invariant 3 says every user receives
  notifications and `ui-context.md` shows both in the sidebar for all roles;
  withholding them would hide a user's own alerts and preferences from them.
  Managing *others'* notification configuration remains a separate permission.
- **Hearing management has no dedicated permission yet.** The spec's suggested
  list has none, and it also says not to invent business behaviour, so court
  representatives get `cases:update` — which is exactly what "trigger case status
  updates" requires. Case Management should introduce `hearings:*` and narrow this.
- **403 responses are deliberately uninformative.** Every denial returns the same
  `forbidden` code and message, whichever rule refused it — a test asserts all four
  rule kinds are byte-identical. Naming the missing permission would let a caller
  map the platform's capability model by probing. The specifics go to the log with
  the same `request_id` the client received, so an operator can still diagnose it.
- **An unknown role or permission is a 500, not a 403.** Both are impossible for a
  client to provoke (no endpoint accepts an identifier), so they are bugs.
  Answering 403 would hide a missing policy entry behind a plausible-looking
  authorization failure; answering 500 with a generic body surfaces it without
  telling the caller anything.
- **An empty requirement list raises.** `require_all_permissions([])` would admit
  everyone and `require_any_permission([])` would deny everyone — both are almost
  always a requirement built dynamically that came out empty. The frontend
  evaluator makes the same call in the opposite direction (an empty `anyOf`/`allOf`
  denies) because a UI cannot usefully throw; an *absent* clause still means "no
  requirement".
- **401 before 403, by dependency order.** `CurrentUser` resolves first, so an
  anonymous caller is asked to authenticate rather than told they lack permission
  — they may well be entitled once signed in. The same reasoning puts `RequireAuth`
  outside `RouteGuard` on the client.
- **`ProtectedRoute` renders in place; it does not redirect.** Redirecting to
  `/access-denied` would lose the URL the user asked for, so a reload would retry
  the error page rather than the real one, and a mistyped link would look like a
  broken app. The `/access-denied` route still exists for direct links.
- **The shell wraps the guard, not the reverse.** A denied user keeps the sidebar
  and can navigate somewhere they *can* reach, instead of landing on a bare error
  page with no way out.
- **Route rules are derived from the navigation config, not written twice.** Each
  nav item declares its `access`; `routeAccessRules` is computed from that list and
  feeds both the sidebar filter and `RouteGuard`. That makes "the sidebar never
  offers a destination the guard would block" true by construction — and a test
  asserts it for all three roles rather than trusting the convention.
- **The client holds permission *identifiers*, never the policy.** The role →
  permission mapping exists only on the server; the browser receives its effective
  list with the session. The one client-side copy of the mapping lives in the test
  helpers, where its purpose is to describe a realistic fixture without a backend.
- **Unknown permission identifiers in an API response are dropped, not fatal.** A
  backend that has added a permission this build does not know about must not be
  able to break sign-in — and a name the client cannot express is a name it cannot
  gate on anyway, so ignoring it is also the safe outcome.

### Authentication (spec `03`)

- **Token transport — Bearer access token + httpOnly refresh cookie.** The
  short-lived access token is held **in memory only** (`lib/api/token-store.ts`),
  never in `localStorage`/`sessionStorage`/a JS-readable cookie, so an XSS cannot
  exfiltrate a durable credential. The long-lived refresh token is delivered as an
  `httpOnly; SameSite=strict; Path=/` cookie (Secure enforced in production), so
  it is unreadable by script. This is the **CSRF-safe strategy** the spec asks
  for: ordinary API calls authenticate with an `Authorization` header (a forged
  cross-site request carries no usable credential), and the cookie only means
  anything to `/auth/refresh` and `/auth/logout`, which `SameSite` protects.
  `Path=/` (rather than scoping to `/auth`) is required so the Next.js proxy can
  see the cookie during route protection.
- **Session persistence without persisting the token.** "Restore the session after
  page refresh" is satisfied by exchanging the refresh cookie for a new access
  token on mount (`SessionProvider`), not by storing the token. The refresh token
  is also returned in the login/refresh response body for non-browser clients; the
  web client deliberately ignores it.
- **Refresh tokens are single-use and rotated.** Each refresh revokes the token it
  consumed, so replaying a captured refresh token fails. A shared in-flight
  refresh promise in `lib/api/client.ts` prevents concurrent 401s from starting
  competing refreshes (which rotation would cause to fail and sign the user out).
- **Logout needs server-side state, so revocation lives in Redis.** JWTs are valid
  until expiry, so `services/token_revocation.py` denylists the `jti` of revoked
  tokens with a TTL equal to the token's remaining life — bounded growth, and
  logout genuinely ends the session. The store **fails closed**: if Redis is
  unreachable the request is rejected rather than assuming a token is still good.
- **`token_expired` is a distinct error code from `invalid_token`.** The client
  uses this to decide between transparently refreshing and forcing a re-login; a
  revoked token is never retried.
- **bcrypt is used directly, not through `passlib` — deviation from the spec's
  dependency list.** The spec names `passlib[bcrypt]`, but `passlib` 1.7.4 (its
  final release, 2020) is **incompatible with `bcrypt` 5.x**: its backend probe
  hashes a >72-byte password and `bcrypt` 5 raises
  `ValueError: password cannot be longer than 72 bytes` (reproduced; hashing fails
  outright). The alternatives were pinning `bcrypt` back to 4.x — which still logs
  a `(trapped) error reading bcrypt version` traceback on first use and holds a
  security library back for an unmaintained wrapper — or calling `bcrypt`
  directly. We call it directly. The spec's actual requirement ("passwords must be
  hashed using bcrypt") is met, `bcrypt` stays current, and there is no dead
  dependency in the auth path. `passlib` was removed from `requirements.txt`.
- **Passwords over 72 bytes are rejected, not truncated.** bcrypt ignores input
  past 72 bytes, which would make two different long passwords equivalent. The
  limit is enforced on *bytes* (not characters, which `max_length` would check) in
  both the Pydantic schema and `core/security.py`.
- **Role is stored but not enforced.** `User.role` is persisted and returned by
  `/auth/me` because `architecture.md`'s storage model requires it and the shell's
  `UserMenu` already displays it — but **no endpoint is role-gated** in this spec,
  and tests assert every role can authenticate equally. Enforcement belongs to
  RBAC.
- **Two-layer route protection.** `apps/web/proxy.ts` (the Next 16 `proxy`
  convention that replaces `middleware`) is a *fast pre-filter*: it can only see
  whether the refresh cookie is **present**, not whether it is valid, since
  validation needs the API's signing secret. `RequireAuth` on the client is the
  authoritative check (it catches revoked/expired sessions the cookie check
  cannot), and the API — which rejects every unauthenticated request — is the only
  layer that actually protects data.
- **The root `/` redirect moved from `next.config.ts` into `proxy.ts`,** because
  the destination now depends on session state (`/dashboard` vs `/login`).
- **`?next=` is validated before redirecting** (`safeRedirectTarget` in
  `lib/routes.ts`): same-origin paths only, rejecting absolute and
  protocol-relative URLs so the login page cannot become an open redirect.
- **`useLogin` reads `?next=` from `window.location` rather than
  `useSearchParams()`.** `useSearchParams` is a dynamic API: using it opted the
  login form out of prerendering, so the served HTML contained no `<input>`
  elements until JavaScript hydrated. The value is only needed once, at submit, so
  there is nothing to gain from reactivity. Verified: form inputs now appear in the
  SSR output.
- **No self-registration; accounts come from `scripts/create_user.py`** until User
  Management ships, per the spec's explicit exclusion.

### Auth hardening (post-spec-`03` follow-up)

- **Bulk session revocation uses a generation counter, not a timestamp.** The
  first attempt stored a `sessions_valid_from` cut-off and rejected tokens with
  `iat < cutoff`. That is **inherently racy**: JWT `iat` has whole-second
  precision, so the replacement pair minted immediately after the change had
  `iat` *earlier* than the sub-second cut-off and was rejected — and truncating the
  cut-off to whole seconds instead let same-second tokens from other devices
  survive. An integer `session_generation` compared with the token's `sgen` claim
  has no such ambiguity: the replacement pair is minted under the new generation
  and everything older is rejected, exactly. The timestamp migration was reverted
  and replaced (`6f3ebd7e2669`).
- **`session_generation` lives in PostgreSQL, not Redis.** Revocation caused by a
  password change must be durable — flushing the cache must never resurrect a
  revoked session. The Redis denylist remains for *individual* token revocation
  (logout, refresh rotation), where entries are short-lived by design.
- **A missing `sgen` claim reads as generation 0**, so tokens issued before the
  claim existed keep working against users still on the default generation.
  Deploying the migration does not sign everyone out.
- **Throttling fails closed.** If Redis is unreachable, login returns 503 rather
  than proceeding unthrottled — consistent with the revocation store. This costs no
  real availability, because a Redis outage already fails every authenticated
  request through `is_revoked`.
- **`change-password` returns tokens, a deliberate contract change.** Because the
  change revokes the token it was called with, returning a bare message would leave
  the caller holding a dead credential. The response now carries the replacement
  pair, and the client swaps it in immediately.
- **Only credential failures count toward the lockout.** Disabled-account and
  validation failures do not, so legitimate users keep receiving actionable errors
  rather than being throttled into an opaque 429.

### Foundation

- **Backend baseline:** FastAPI + SQLAlchemy 2.0 + Alembic + `pydantic-settings`
  + `structlog`, with Redis/MinIO/Qdrant clients. Matches `architecture.md`.
  Package layout under `apps/api`: `core/` (config, logging, middleware,
  exceptions, lifespan, readiness, and infra clients cache/storage/vector),
  `db/` (Base, session, Alembic migrations), `api/` (system health router +
  `api/v1/` aggregate), `schemas/` (errors, health). Modules import as
  top-level packages (`from core.config import settings`), which works because
  `apps/api` is the runtime root (uvicorn `main:app`, Alembic `prepend_sys_path`,
  pytest `pythonpath`).
- **Synchronous SQLAlchemy (not async):** the foundation uses a sync engine +
  `Session`; FastAPI runs sync dependencies/handlers in a threadpool. Simpler
  and sufficient for CRUD; long-running work goes to background workers per
  `architecture.md` (invariant 7), not the request DB session. Revisit if a
  feature needs async DB I/O.
- **`structlog` for structured logging:** `architecture.md` names Langfuse/OTel
  (AI observability) and Sentry (error tracking) but no general app logger;
  `structlog` fills that gap (JSON in prod/test, console in dev) and can later
  feed OTel/Sentry. Recorded here as a foundation decision, not a contradiction.
- **Liveness vs. readiness split:** `/health` is pure liveness (always 200 when
  the process runs); `/ready` probes all four datastores concurrently
  (`core/readiness.py`) and returns 503 with a per-dependency breakdown if any
  is down. A downstream outage logs a warning at startup but does **not** abort
  boot, so the app can start and report readiness. All infra clients carry
  bounded connect timeouts so probes fail fast instead of hanging.
- **Config-driven, fail-fast settings:** a cached `Settings` singleton validates
  at import (bad config crashes startup); production invariants reject unsafe
  defaults (DEBUG on, `*` hosts, default DB/MinIO secrets). No secrets in VCS —
  Alembic resolves the DB URL from settings at runtime.
- **Python tooling config lives in a root `pyproject.toml`** (ruff, mypy strict,
  pytest) — a config file, not a package; it does not alter the folder layout.

- **Frontend baseline:** Next.js 16 (App Router, Turbopack) + React 19 +
  TypeScript strict, Tailwind CSS v4, shadcn/ui (Radix primitives via the
  unified `radix-ui` package), `lucide-react` icons, `next-themes`. Matches
  `architecture.md`.
- **Design tokens:** single source of truth in `app/globals.css`; shadcn
  tokens are aliases over the `ui-context.md` palette so components inherit the
  theme without hardcoded colors.
- **`components/ui/*` are treated as protected/generated** (per
  `ai-workflow-rules.md`) and not hand-edited — with one necessary exception
  below.
- **Client UI state → Zustand; server state → TanStack Query.** `architecture.md`
  names TanStack Query for server/cache state but does not cover ephemeral UI
  state (sidebar collapse, mobile drawer). Zustand (`stores/`) fills that gap —
  a small, standard companion to React Query. Introduced by the Application
  Shell.
- **Toasts → `sonner`.** The Toast Provider required by spec `01` uses shadcn's
  `sonner` wrapper (`components/ui/sonner.tsx`), colored via the platform
  tokens. `sonner` is authored as a design-system component (net-new, not an
  edit to a previously generated file).
- **Routing uses App Router route groups:** `(auth)` (public, `AuthLayout`) and
  `(protected)` (app shell). Groups don't affect URLs, so `/login`, `/dashboard`,
  etc. stay clean while sharing per-group layouts.
- **Root `/` redirect lives in `next.config.ts`** (`redirects()` → `/dashboard`,
  307), not a page. A page-level `redirect()` in Next 16 renders a 1s
  meta-refresh page for hard GETs; the config redirect is instant and edge-level.
  Replaced by auth-aware routing later.
- **Design-system reference page removed:** the spec-00 `app/page.tsx` showcase
  was validation scaffolding; `/` now redirects into the app, so it was deleted.

### Notable implementation notes / deviations (build-critical)

- **TypeScript pinned to 6.x (not 7.x):** `typescript-eslint` (bundled by
  `eslint-config-next`) does not yet support the TS 7 native compiler; TS 7
  broke linting. TS 6.x satisfies strict-mode requirements.
- **ESLint pinned to 9.x (not 10.x):** the `eslint-plugin-react` bundled by
  `eslint-config-next@16` uses APIs removed in ESLint 10 (`context.getFilename`).
  ESLint 9 is the supported line (peer range `>=9`).
- **`eslint.config.mjs` uses the native flat config** exported by
  `eslint-config-next@16` (spread directly), not `FlatCompat`.
- **`next lint` removed in Next 16** — lint is run via `eslint .` directly.
- **`"use client"` added to `components/ui/button.tsx` and `badge.tsx`**
  (the only necessary edit to generated files): both import `Slot` from the
  unified `radix-ui` **barrel** but were server components; on the server the
  barrel eagerly evaluates client-only Radix modules that call
  `React.createContext`, which does not exist under React's `react-server`
  condition, crashing the build. Marking these two files client-side moves the
  barrel evaluation to the client layer. Documented here as the sanctioned
  exception to the "don't modify generated files" rule.
- **Removed empty `apps/web/middleware.ts`** (0-byte scaffold placeholder) — it
  had no function export and broke the Next 16 build. Request-level middleware
  arrived with spec `03` as **`apps/web/proxy.ts`** (Next 16's `proxy` convention).

Fixed while implementing spec `03` (pre-existing breakage, unrelated to auth but
blocking its validation):

- **`requirements.txt` was UTF-16LE, so `pip install -r` failed outright**
  (`Invalid requirement: '\x00#\x00 ...'`). The tracker claimed it had been
  rewritten as UTF-8; it had not. Rewritten as real UTF-8.
- **Alembic's `ruff_format` post-write hook never ran** — it used
  `type = console_scripts` with `entrypoint = ruff`, but the ruff wheel ships a
  prebuilt binary and declares no `console_scripts` entry point
  (`Could not find entrypoint console_scripts.ruff`). Changed to `type = exec`
  with a path relative to `alembic.ini`, so it works without activating the venv.
- **Ruff's migration exclude never matched:** `extend-exclude` used
  `db/migrations/versions`, but the pattern resolves from the repo root, so it had
  to be `apps/api/db/migrations/versions`. Also fixed the generated-migration
  import order at the source, in `script.py.mako`.
- **`apps/web` `lint` script pointed at `next lint`,** which Next 16 removed;
  changed to `eslint .` (the tracker already noted the command was unavailable).
- **`status.HTTP_422_UNPROCESSABLE_ENTITY` is deprecated in Starlette 1.x** and
  emitted a `StarletteDeprecationWarning` on *every* validation failure at
  runtime. Switched to `HTTP_422_UNPROCESSABLE_CONTENT` (same code, 422).
- **`.local` and `.test` are special-use domains that `email-validator` rejects.**
  The app-shell's mock user email (`amina.benali@legal-platform.local`) is not a
  valid login email; fixtures and docs use `example.com`.
- **Removed the empty `apps/api/services/auth/` scaffold directory.** It sat
  beside the new `services/auth.py` and, while Python correctly prefers the
  module today, adding an `__init__.py` there would have silently shadowed the
  whole `AuthService`. The other `services/*` placeholders were left alone since
  they collide with nothing yet.

## Session Notes

- **Creating users.** Day to day, use the **Users** page (`/users`) or
  `POST /api/v1/users` — there is no self-registration. `scripts/create_user.py`
  is the **bootstrap** path, for the first administrator (before any account
  exists to authorize that call) or to recover from a total lockout. From
  `apps/api`:
  ```
  python -m scripts.create_user --email admin@example.com --name "Amina Benali" \
      --role administrator          # omit --password to be prompted securely
  ```
  Roles: `administrator` | `lawyer` | `court`. `--name` is split on the first
  space into first/last name. Re-running for an existing email updates that
  account, resets its password, and **revokes its sessions** (an out-of-band
  credential change must end sessions holding the old one). Note that
  `email-validator` rejects `.local`/`.test` domains, so use a real-looking domain.
- **User Management test strategy:** the same no-Docker approach as auth —
  `tests/unit/test_user_service.py` runs the *real* repository against SQLite
  in-memory, so search/sort/pagination SQL is exercised without a container, while
  `tests/integration/test_users.py` drives the endpoints over HTTP through
  `api_client`. The `make_user` fixture accepts `first_name`/`last_name`,
  `status` (or the `is_active` shorthand), `phone`, `last_login_at`, and an
  explicit `created_at` — the last so ordering tests do not depend on wall-clock
  gaps between rows inserted in the same millisecond.
- **Frontend Radix components need jsdom polyfills**, now in `tests/setup.ts`
  (`ResizeObserver`, `hasPointerCapture`/`setPointerCapture`/`releasePointerCapture`,
  `scrollIntoView`). Without them Select and Checkbox throw on mount or on click.
  Any future test rendering a Radix primitive inherits these for free.
- **A locally installed PostgreSQL can shadow the container on port 5432.** During
  spec `07`'s validation, every host connection to `localhost:5432` failed with
  *password authentication failed for user "postgres"* while the container was
  healthy and held the real data. The cause was a Windows PostgreSQL service
  (`D:\Apps\PostgreSQL\bin\postgres.exe`) listening on the same port and winning
  the loopback binding — Docker's published port is still there, but connections
  reach the local server instead. `Get-NetTCPConnection -LocalPort 5432 -State
  Listen` plus `Get-Process -Id <pid> | Select Path` identifies the owner. The
  non-invasive workaround, used for that validation, is a throwaway TCP proxy on a
  spare port, which leaves the user's services untouched:
  ```
  docker run -d --rm --name legal-pgproxy \
      --network legalcasemanagementplatform_default -p 55432:5432 \
      alpine/socat tcp-listen:5432,fork,reuseaddr tcp-connect:legal-postgres:5432
  # then run anything with POSTGRES_PORT=55432
  ```
  Stopping the local Windows service is the permanent fix, but that is the user's
  machine to change.
- **Document Management test strategy:** the same no-Docker approach as cases —
  `tests/unit/test_document_service.py` runs the *real* repository against SQLite
  in-memory so the search/filter/sort/scope SQL is exercised without a container,
  with only object storage faked. `tests/conftest.py` provides
  `InMemoryDocumentStorage` (which deliberately keeps the "logical delete keeps
  the bytes" behaviour — a double that actually removed them would let a
  retention test pass falsely) and a `make_document` factory that writes both the
  metadata and the bytes, so a fixture-built document is genuinely downloadable.
  Real file signatures live in `tests/helpers.py` (`PDF_BYTES`, `PNG_BYTES`,
  `DOCX_BYTES`, `TXT_BYTES`): a `b"x"` placeholder is rejected by the
  corrupted-upload check, which is the rule those bytes exist to prove.
- **Frontend upload tests need an XHR double, not the `fetch` double.** Uploads go
  through `lib/api/upload.ts`, which uses `XMLHttpRequest`, so `mockFetch` never
  sees them. `mockUpload` in `apps/web/tests/helpers.ts` scripts them the same
  way, emits progress events, and takes `hold: true` + `release()` so a test can
  observe an in-flight state — without it a request completes on the next
  macrotask, faster than any assertion, and a progress bar appears never to
  render. Note also that `userEvent` honours a file input's `accept` attribute:
  to exercise the *schema* rule behind it, set it up with
  `userEvent.setup({ applyAccept: false })` (a setup option in v14, not a
  per-call one).
- **Watch out for a stale `next dev` server.** A dev server left running from an
  earlier session serves its old compilation and reports new routes as 500s.
  `Get-NetTCPConnection -LocalPort 3000 -State Listen` finds the owning PID; kill
  it and restart before concluding a route is broken. The same applies to
  `uvicorn` on 8000 — bind failures are logged and the process exits, so requests
  silently hit the *other* server.
- **Auth test strategy:** backend auth tests need **no Docker** — `tests/conftest.py`
  overrides `get_db` with SQLite in-memory, and `get_token_revocation_store` /
  `get_login_throttle` with in-memory doubles, and forces `BCRYPT_ROUNDS=4`
  (bcrypt's minimum) so the suite is not dominated by deliberate hashing slowness.
  The throttle double exposes `advance(timedelta)` so tests can step past the
  failure window and lockout without sleeping. `tests/unit/test_login_throttle.py`
  additionally exercises the **real** Redis-backed throttle, and skips itself when
  Redis is unavailable. Frontend tests run under Vitest + Testing Library
  (`npm test` in `apps/web`) against a scripted `fetch` double in
  `tests/helpers.ts`.
- **Adding a permission (the whole checklist):** add the member to
  `Permission` in `apps/api/core/permissions.py`, grant it to the roles that
  should hold it in `apps/api/core/roles.py` (administrators get it for free),
  mirror the identifier in `apps/web/types/authorization.ts` (`PERMISSIONS` and
  `PERMISSION`), and guard the endpoint with
  `Depends(require_permission(Permission.X))`. If it gates a *page*, declare
  `access` on its item in `apps/web/config/navigation.ts` and both the sidebar and
  the route guard pick it up — nothing else to wire.
- **RBAC test strategy:** the authorization service is pure, so its unit tests
  build `User` objects in memory and never touch a database.
  `tests/integration/test_authorization.py` additionally mounts a throwaway
  `FastAPI` app with four guarded routes, because the dependencies should be
  testable independently of whatever they happen to guard (and no business
  endpoints exist yet). Tokens are signed rather than session-bound, so one issued
  through the main app authenticates against that throwaway app unchanged.
- **Careful when importing from `conftest.py`:** pytest loads it as top-level
  `conftest`, so a runtime `from tests.conftest import X` creates a *second*
  distinct class object and breaks `isinstance`. Import such helpers under
  `TYPE_CHECKING` only and get the instance from the fixture.
- **Throttle lockouts survive a test run** if the real Redis throttle is used
  against a real account. To clear them manually:
  ```
  python -c "import sys; sys.path.insert(0,'apps/api'); from core.cache import redis_client; [redis_client.delete(k) for p in ('auth:login_attempts:*','auth:login_lock:*') for k in redis_client.scan_iter(p)]"
  ```
- **Running the full stack locally:** `docker compose up -d`, then
  `uvicorn main:app --reload` from `apps/api` (port 8000) and `npm run dev` from
  `apps/web` (port 3000). `apps/web/.env.local` (copy of `apps/web/.env.example`)
  points the frontend at the API and must carry the **same** refresh-cookie name as
  the backend's `.env`.
- **Cookies across ports in dev:** cookies ignore port, so the refresh cookie the
  API sets on `localhost:8000` is visible to the Next server on `localhost:3000`,
  and `localhost:3000 → localhost:8000` counts as same-site — `SameSite=strict`
  therefore works in local development, not just behind a single origin in prod.

- **Backend (apps/api) tooling:** Python 3.12.6 in `.venv` at the repo root
  (`.venv/Scripts/python.exe` on Windows). Commands from the repo root:
  `.venv/Scripts/python.exe -m ruff check apps/api tests`,
  `... -m mypy apps/api`, `... -m pytest`. Run the API from `apps/api` with
  `uvicorn main:app --reload`. Run Alembic from `apps/api`
  (`python -m alembic revision --autogenerate -m "..."`, `... upgrade head`) —
  `alembic.ini` uses `prepend_sys_path = .` so app imports resolve. Copy
  `.env.example` → `.env` for local config; tests force `ENVIRONMENT=testing`.
- **Backend dependency install note:** the initial `requirements.txt` shipped
  UTF-16 with a partial package set; it was rewritten as pinned UTF-8. The venv
  already contained auth libs (`python-jose`, `passlib`, `bcrypt`, `argon2-cffi`)
  reserved for the upcoming Auth feature — kept and listed in `requirements.txt`.
- **Known benign test warning:** `pytest` emits one
  `StarletteDeprecationWarning` (`httpx` vs `httpx2`) from FastAPI's `TestClient`
  import — third-party test tooling only; it does **not** appear at application
  runtime (verified clean uvicorn startup log).
- **Infrastructure (Docker Compose):** `docker-compose.yml` at the repo root runs
  Postgres (5432), Redis (6379), MinIO (9000 API / 9001 console), and Qdrant
  (6333 REST / 6334 gRPC). Start with `docker compose up -d`, stop with
  `docker compose down` (add `-v` to wipe volumes). MinIO console creds:
  `minioadmin` / `minioadmin123`. The root `.env` mirrors these credentials for
  the API; keep `.env` out of version control (only `.env.example` is committed).
  The compose file still carries the obsolete top-level `version:` key (harmless
  warning) as provided; the Qdrant healthcheck was changed from `curl` to a bash
  `/dev/tcp` probe since that image has no curl.

- Project began as a bare scaffold (empty domain directories + `.gitkeep`, no
  `package.json`/`globals.css`); this iteration bootstrapped the whole
  `apps/web` frontend.
- Tooling: Node v22.17, npm v11.12. pnpm is not installed — use **npm**.
- Commands (run from `apps/web`): `npm run dev`, `npm run build`,
  `npm run typecheck` (`tsc --noEmit`), lint via `npx eslint .`.
- `next lint` is not available on Next 16; use `npx eslint .`.
- Localization (next-intl) is NOT part of the design-system or app-shell specs —
  strings across the shell (nav titles, page headers, placeholders, empty/error
  states) are English placeholders to be replaced with translation keys when
  i18n lands. Navigation config and shared components are structured so only the
  label fields change. RTL (Arabic) is likewise deferred to the localization
  feature.
- **App-shell dependencies added:** `@tanstack/react-query` (server state),
  `zustand` (client UI state), `sonner` (toasts). `npm audit` reports 3
  pre-existing high-severity advisories in the transitive tree; not introduced
  by this work and left untouched (no `audit fix --force`, which forces breaking
  upgrades).
