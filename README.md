# AI-Powered Legal Case Management Platform

A collaborative web application for managing legal cases end to end — connecting
**administrators, lawyers, and court representatives** in one workspace where they
track case progress, share documents, follow hearings, and ask questions of their
own case files in **Arabic, French, or English**.

The AI side is retrieval-first: uploaded documents are OCR'd, chunked, embedded,
and indexed into a vector database, and every answer the assistant gives is built
**only** from passages it retrieved from documents the reader is authorized to
see — with a citation to the file, version, and page behind each statement. When
the documents don't support an answer, it says so instead of guessing.

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Getting started](#getting-started)
- [Running the stack](#running-the-stack)
- [Creating the first user](#creating-the-first-user)
- [Testing](#testing)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Documentation](#documentation)

---

## What it does

**Cases and collaboration**
- Create, edit, archive, and organize cases with a full lifecycle and audit trail
- Assign one or more lawyers per case; court representatives get scoped access
- Shared timeline of every case event, synchronized live over WebSocket
- Hearings, court decisions, and deadlines

**Documents**
- Versioned uploads to object storage, with category and access control
- **OCR** (Tesseract) over scanned PDFs and images, one text row per page
- Automatic **indexing** into Qdrant after extraction — no manual step

**AI**
- **Semantic search** across the documents a user is allowed to read, in any of
  the three languages, against documents written in any of them
- **AI assistant** — conversational Q&A grounded in retrieved passages, with
  streaming, citations, follow-up suggestions, and per-answer feedback
- **Report agent** — five report types (case summary, hearing preparation,
  evidence summary, chronological timeline, executive summary), generated
  section by section in the background and exportable as Markdown or PDF

**Platform**
- Role-based access control (administrator / lawyer / court representative)
- Notifications in-app, by **email**, and by **WhatsApp**
- Dashboard and analytics; monitoring and structured logging throughout
- Full **English / French / Arabic** interface with RTL layout for Arabic

---

## Architecture

```text
┌──────────────────────────────────────────────────────────┐
│  apps/web  — Next.js 16, React 19, TypeScript, Tailwind  │
└───────────────────────────┬──────────────────────────────┘
                            │  REST + WebSocket
┌───────────────────────────▼──────────────────────────────┐
│  apps/api  — FastAPI, SQLAlchemy, Alembic, Pydantic      │
└──┬─────────┬──────────┬──────────┬──────────┬────────────┘
   │         │          │          │          │
┌──▼────┐ ┌──▼───┐ ┌────▼───┐ ┌────▼────┐ ┌───▼──────────┐
│Postgres│ │Redis │ │ MinIO  │ │ Qdrant  │ │ Gemini API   │
│ cases, │ │cache,│ │document│ │ vector  │ │ (via a       │
│ users  │ │queues│ │ files  │ │ search  │ │  provider    │
└────────┘ └──────┘ └────────┘ └─────────┘ │  abstraction)│
                                            └──────────────┘
```

The AI pipeline is a one-directional chain — each stage consumes what the
previous produced and never reaches past it:

```text
OCR → Indexing → Semantic Search → RAG Pipeline → ┬→ AI Assistant
(Tesseract) (bge-m3 → Qdrant)     (LangGraph +    └→ Report Agent
                                   Gemini)
```

Every third-party capability sits behind a Protocol seam with a registry
resolver, so the OCR engine, chunker, embedder, vector store, ranker, prompt
library, and LLM provider are each replaceable by adding one class. See
[docs/ai-features-report.md](docs/ai-features-report.md) for the full account,
including why Qdrant rather than ChromaDB and why bge-m3 rather than a hosted
embedding API.

**Stack**

| Layer | Technology |
| --- | --- |
| Frontend | Next.js 16, React 19, TypeScript, Tailwind 4, shadcn/ui, TanStack Query, next-intl |
| Backend | Python 3.12, FastAPI, SQLAlchemy, Alembic, Pydantic |
| Database | PostgreSQL |
| Cache / queues | Redis |
| Object storage | MinIO (S3-compatible) |
| Vector database | Qdrant |
| OCR | Tesseract + Poppler |
| Embeddings | BAAI/bge-m3 (local, via sentence-transformers) |
| Orchestration | LangGraph |
| LLM | Google Gemini (`gemini-2.5-flash`), behind a provider interface |

---

## Repository layout

```text
apps/
  api/          FastAPI backend — core/, services/, repositories/, models/, api/v1/
  web/          Next.js frontend — app/, components/, hooks/, lib/, messages/
  worker/       background worker entry points
context/        product, architecture, and per-feature specifications
docs/           the AI implementation report, manual test checklist, ADRs
infrastructure/ Dockerfiles and service configuration
tests/          backend test suite — unit/, integration/, e2e/, ai/, performance/
```

`context/` is where the project is actually specified: `project-overview.md`,
`architecture.md`, `code-standards.md`, and 23 numbered feature specs under
`context/feature-specs/`.

---

## Getting started

### Prerequisites

| Requirement | Notes |
| --- | --- |
| **Docker Desktop** | Runs PostgreSQL, Redis, MinIO, Qdrant, and Mailpit |
| **Python 3.12** | Backend |
| **Node.js 22+** and npm | Frontend (pnpm is not used) |
| **Tesseract OCR** | Needed only if you run the API on the host. Install the `eng`, `fra`, and `ara` language packs |
| **Poppler** | `pdftoppm` / `pdfinfo`, used to render PDF pages for OCR |
| **A Gemini API key** | Free tier is sufficient — <https://aistudio.google.com/apikey> |

On Windows, `winget install UB-Mannheim.TesseractOCR` installs Tesseract with all
language packs. If it doesn't land on `PATH`, set `TESSERACT_CMD` in `.env` to the
full path of `tesseract.exe` rather than editing your `PATH`.

### 1. Clone and configure

```bash
git clone <your-repo-url>
cd "Legal Case Management Platform"

cp .env.example .env                       # backend + infrastructure
cp apps/web/.env.example apps/web/.env.local   # frontend
```

Open `.env` and set at minimum:

- `LLM_API_KEY` — your Gemini key
- `JWT_SECRET_KEY` — any long random string (the shipped default is a placeholder
  that is safe only for local development)
- `TESSERACT_CMD` / `POPPLER_PATH` — only if those binaries aren't on `PATH`

> `.env` is gitignored. Never commit real keys; `.env.example` is the file that
> is tracked.

### 2. Start the infrastructure

```bash
docker compose up -d
```

This brings up five containers:

| Service | Ports | Notes |
| --- | --- | --- |
| PostgreSQL | `5433` → 5432 | 5433 on the host so it can't collide with a local install |
| Redis | `6379` | |
| MinIO | `9000` API, `9001` console | `minioadmin` / `minioadmin123` |
| Qdrant | `6333` REST, `6334` gRPC | |
| Mailpit | `1025` SMTP, `8025` inbox | Catches all outgoing mail — <http://localhost:8025> |

### 3. Backend

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate  on macOS/Linux
pip install -r requirements.txt

cd apps/api
python -m alembic upgrade head
uvicorn main:app --reload
```

The API is at <http://localhost:8000>, with interactive docs at
<http://localhost:8000/docs> and the OpenAPI schema at `/openapi.json`. All
endpoints are under `/api/v1`.

### 4. Frontend

```bash
cd apps/web
npm install
npm run dev
```

The application is at <http://localhost:3000>.

---

## Running the stack

Day to day, three commands in three terminals:

```bash
docker compose up -d                                  # once
cd apps/api && uvicorn main:app --reload              # terminal 1
cd apps/web && npm run dev                            # terminal 2
```

The API can also run in Docker — it's behind a compose profile so it doesn't
start by default:

```bash
docker compose --profile api up -d
```

Stop everything with `docker compose down`, or `docker compose down -v` to also
wipe the volumes (databases, uploaded files, and the vector index).

---

## Creating the first user

There is no public sign-up: accounts are created by an administrator, and the
first administrator is created from the command line. Run it **as a module**,
from `apps/api`:

```bash
cd apps/api
python -m scripts.create_user \
  --email you@example.com \
  --name "Your Name" \
  --role administrator
```

Roles are `administrator`, `lawyer`, and `court`. Running it as a file path
(`python scripts/create_user.py`) fails with `ModuleNotFoundError: No module
named 'core'`, because that puts `scripts/` on `sys.path` instead of `apps/api`.

---

## Testing

**Backend** — 4,250 tests, from the repository root:

```bash
.venv/Scripts/python.exe -m pytest              # all
.venv/Scripts/python.exe -m pytest tests/unit   # one suite
.venv/Scripts/python.exe -m ruff check apps/api tests
.venv/Scripts/python.exe -m mypy apps/api
```

**Frontend** — 713 tests, from `apps/web`:

```bash
npm run test        # vitest
npm run typecheck   # tsc --noEmit
npm run lint        # eslint
npm run build       # production build
```

Every automated test runs against a mock or a throwaway database. Nothing in the
suite watches a real scan travel through Tesseract into Qdrant, or reads an
Arabic screen right-to-left — [docs/manual-test-checklist.md](docs/manual-test-checklist.md)
is the human pass that covers those.

---

## Configuration

All backend settings live in `.env`; `.env.example` documents every one with the
reasoning behind its default. The ones most worth knowing:

| Setting | Default | What it controls |
| --- | --- | --- |
| `LLM_API_KEY` | — | Gemini key. Without it the AI features report unavailable and everything else works |
| `LLM_MODEL` | `gemini-2.5-flash` | Generation model |
| `OCR_LANGUAGES` | `eng+fra+ara` | Tesseract language packs to load |
| `OCR_DPI` | `300` | Rasterization resolution; lower is faster and less accurate |
| `OCR_MAX_PAGES` | `100` | Page cap per document |
| `OCR_TIMEOUT_SECONDS` | `600` | **Whole-run** deadline, not per page — must cover `OCR_MAX_PAGES` |
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | Downloaded on first index (~2 GB), then cached |
| `INDEX_CHUNK_SIZE` | `1000` | Characters per passage, with 200 overlap |
| `RAG_RETRIEVAL_TOP_K` | `8` | Passages retrieved per question |
| `SMTP_HOST` | `localhost` | Points at Mailpit in development |
| `DEFAULT_LANGUAGE` | `en` | Fallback language when a user has no preference |

---

## Troubleshooting

**OCR fails with `timeout`.** `OCR_TIMEOUT_SECONDS` is a deadline for the whole
document, shared across all its pages — not a per-page limit. At 300 DPI with
three languages, throughput is roughly 4 seconds per page, so a 100-page document
needs about 420 seconds. If you lower the timeout, lower `OCR_MAX_PAGES` with it,
or documents past the crossover will fail identically every time.

**OCR fails with `engine_unavailable`.** Tesseract isn't on `PATH`. Set
`TESSERACT_CMD` in `.env` to the binary's full path. Note that a bare
`python -c "import pytesseract; pytesseract.get_tesseract_version()"` will report
it missing even when configured correctly, because it never loads settings —
check with `get_ocr_engine().is_available()` instead.

**Emails never arrive.** In development, `SMTP_HOST=localhost:1025` points at
Mailpit, which **captures** mail and delivers none of it — read it at
<http://localhost:8025>. Real delivery needs a real relay and a real sending
domain; `notifications@legal.local` cannot route anywhere, since `.local` is
reserved for mDNS.

**Indexing is slow.** Embeddings run on CPU unless a CUDA build of PyTorch is
installed, and the first run additionally downloads bge-m3 (~2 GB). A 50-page
document takes several minutes on CPU. This is expected, not a hang.

**The assistant refuses a question it should be able to answer.** It answers only
from what a retrieved passage *states*. Counting questions ("how many articles?"),
exhaustive lists, and whole-file summaries have no single passage that answers
them — the report agent covers that kind of work. It also never gives legal
advice or applies law that isn't in the indexed documents; that's a deliberate
constraint, not a defect.

**`ModuleNotFoundError: No module named 'core'`.** Run backend scripts as modules
(`python -m scripts.create_user`) from `apps/api`, not by file path.

---

## Documentation

| Document | What it covers |
| --- | --- |
| [context/project-overview.md](context/project-overview.md) | Product definition, goals, features, scope |
| [context/architecture.md](context/architecture.md) | System structure, boundaries, storage model, invariants |
| [context/ai-architecture.md](context/ai-architecture.md) | AI stack decisions every AI feature must follow |
| [context/code-standards.md](context/code-standards.md) | Implementation rules and conventions |
| [context/feature-specs/](context/feature-specs/) | 23 numbered specifications, one per feature |
| [context/progress-tracker.md](context/progress-tracker.md) | What is built, what is next, and the reasoning behind each decision |
| [docs/ai-features-report.md](docs/ai-features-report.md) | The AI implementation in depth, with technology justifications |
| [docs/manual-test-checklist.md](docs/manual-test-checklist.md) | Human verification pass across the whole platform |

---

## License

See [LICENSE](LICENSE).
