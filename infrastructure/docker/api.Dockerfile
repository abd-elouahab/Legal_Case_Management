# syntax=docker/dockerfile:1
#
# The FastAPI backend (`apps/api`).
#
# `architecture.md` invariant 13 requires every service to be containerized and
# deployable with Docker Compose. This is the API's half of that; the compose
# file at the repo root builds it behind the `api` profile, so `docker compose
# up -d` keeps meaning "bring up the infrastructure" for local development and
# `docker compose --profile api up -d --build` brings up the application too.
#
# --------------------------------------------------------------------------- #
# What is installed here that pip cannot install, and why each one is needed
# --------------------------------------------------------------------------- #
#
# * **tesseract-ocr** (+ `-fra`, `-ara`) — the OCR engine. `pytesseract` is a
#   wrapper around this binary, not a replacement for it, and the language packs
#   have to match `OCR_LANGUAGES` (`eng+fra+ara`); `eng` ships with the base
#   package. Without them extraction records a `failed` run with
#   `engine_failure` — handled, but useless.
# * **poppler-utils** — `pdftoppm`/`pdfinfo`, which `pdf2image` shells out to in
#   order to render PDF pages before recognition. Same story: handled, but the
#   feature does not work.
# * **fonts-hosny-amiri** (+ **fonts-dejavu-core**) — a font that can typeset an
#   Arabic report, for PDF export. ReportLab's built-in Type 1 fonts are
#   Latin-only, and `project-overview.md` names Arabic as one of the platform's
#   two languages, so without one of these half the intended users cannot export
#   a PDF at all.
#
#   **`fonts-noto-core` is deliberately not the answer here**, which is
#   counter-intuitive enough to be worth the sentence: `NotoNaskhArabic` is the
#   obvious package by name, renders Arabic beautifully, and carries **no Latin
#   letters and no em dash** — so every case number, filename, page reference,
#   and citation dash in an Arabic report would come out as a box. Amiri is a
#   Naskh face built for Arabic body text *with* a Latin companion, which is what
#   a bilingual legal document actually needs; DejaVu is the near-universal
#   fallback and covers both scripts too. `services/report_export.py` discovers
#   either with no configuration and verifies it against the font's own character
#   map before using it.
#
# `TESSERACT_CMD` and `POPPLER_PATH` are deliberately left unset: on Debian all
# three binaries are on `PATH`, and those settings exist for hosts (notably
# Windows) where they are not.

# --------------------------------------------------------------------------- #
# Stage 1 — build the virtual environment
# --------------------------------------------------------------------------- #
#
# Split from the runtime stage so the compiler toolchain pip needs for source
# distributions does not travel into the shipped image.

FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt

# **Torch is installed from the CPU index first, and this is not a micro-
# optimisation.** `sentence-transformers` (the bge-m3 runtime) depends on
# `torch`, and the default PyPI wheel for Linux bundles the CUDA runtime — some
# **2.5 GB of NVIDIA libraries** that a CPU-only deployment will never execute.
# Resolving torch from the CPU index ahead of the main install cuts the image by
# roughly that much. A GPU deployment removes this line and gets the default
# wheel back.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install -r /tmp/requirements.txt

# --------------------------------------------------------------------------- #
# Stage 2 — the runtime image
# --------------------------------------------------------------------------- #

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # The bge-m3 download (~2.3 GB) lands here. Mounted as a volume by the
    # compose file so it survives a container being replaced — it is fetched
    # lazily on the first indexing run, and re-downloading it on every deploy
    # would make a restart take minutes and cost bandwidth for a file that has
    # not changed.
    HF_HOME=/models \
    # `apps/api` is the application root: `main:app`, `alembic.ini`, and the
    # `prompts/` directory are all resolved relative to it.
    PYTHONPATH=/app/apps/api

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        tesseract-ocr-fra \
        tesseract-ocr-ara \
        poppler-utils \
        fonts-hosny-amiri \
        fonts-dejavu-core \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# A non-root user, because the API writes nothing to its own filesystem: uploads
# go to MinIO, business data to PostgreSQL, and vectors to Qdrant. The only
# writable path it needs is the model cache, which is a mounted volume.
RUN useradd --create-home --uid 10001 legal \
    && mkdir -p /models \
    && chown -R legal:legal /models

WORKDIR /app
COPY --chown=legal:legal apps/api ./apps/api

USER legal
WORKDIR /app/apps/api

EXPOSE 8000

# `/health` is the liveness probe and answers without touching a dependency;
# `/ready` deliberately probes PostgreSQL, Redis, MinIO, and Qdrant and is the
# wrong thing for Docker to restart a container over — a momentarily unreachable
# Qdrant is not a reason to kill the API.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# No `--reload`: that is a development flag, and it doubles memory by running a
# reloader process beside the worker. Migrations are **not** run here either —
# `alembic upgrade head` is a deploy step rather than a container start step, so
# that N replicas starting at once do not race the same migration.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
