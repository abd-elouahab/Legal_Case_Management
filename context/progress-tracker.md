# Progress Tracker

Update this file after every meaningful implementation
change.

## Current Phase

- In progress

## Current Goal

- **Next:** Role-Based Access Control (RBAC) — role gating on top of the identity
  established by Authentication. The seams are in place: `User.role` is persisted
  and returned by `GET /auth/me`, `api/deps.py` exposes `CurrentUser` for
  role-checking dependencies to build on, and the `access-denied` route exists
  and is already covered by the proxy's protected-route list.

## Completed

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

- **Role-Based Access Control (RBAC)** — role gating on top of the identity
  established by Authentication, wiring the `access-denied` route. Add
  role-checking dependencies beside `api/deps.py::get_current_user` and a
  client-side role guard alongside `RequireAuth`.
- **User Management** — administrator-managed creation, editing, enabling, and
  disabling of users. This is why registration is deliberately absent from
  Authentication; `scripts/create_user.py` is the interim provisioning path and
  should be superseded by real admin endpoints + UI.
- **Change-password UI** — the backend endpoint, API client, validation schema, and
  `useChangePassword` hook are all in place and tested, but no settings screen wires
  them to a form yet (spec `03` only required a login page). A form should also tell
  the user that other devices were signed out, using the `sessions_revoked` flag.

## Open Questions

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

- **Creating users (no self-registration).** From `apps/api`:
  ```
  python -m scripts.create_user --email admin@example.com --name "Amina Benali" \
      --role administrator          # omit --password to be prompted securely
  ```
  Roles: `administrator` | `lawyer` | `court`. Re-running for an existing email
  updates that account (including resetting the password). Note that
  `email-validator` rejects `.local`/`.test` domains, so use a real-looking domain.
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
