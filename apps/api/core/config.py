"""Application configuration.

Settings are loaded from environment variables (and an optional ``.env`` file)
using ``pydantic-settings``. Validation runs when :data:`settings` is
instantiated at import time, so an invalid configuration fails fast during
startup rather than at first use.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, ValidationInfo, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repo root (apps/api/core/config.py -> parents[3]). The .env lives here, so it
# resolves the same way regardless of the process working directory (the API
# runs from apps/api, tests from the repo root, Alembic from apps/api).
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Development-only JWT secret. Never used in production: the model validator
# below rejects it, so a real secret must be supplied via JWT_SECRET_KEY.
DEV_JWT_SECRET_PLACEHOLDER = "dev-only-insecure-jwt-secret-change-me"

# Minimum acceptable length for the production signing secret (bytes of entropy
# for HMAC-SHA256; 32+ chars is the widely recommended floor).
MIN_JWT_SECRET_LENGTH = 32


class Environment(StrEnum):
    """Supported runtime environments."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"
    TESTING = "testing"


class Settings(BaseSettings):
    """Strongly-typed application settings sourced from the environment."""

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Application ---
    ENVIRONMENT: Environment = Environment.DEVELOPMENT
    PROJECT_NAME: str = "Legal Case Management Platform API"
    VERSION: str = "0.1.0"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = False

    # --- Server ---
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # --- Logging ---
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_JSON: bool = True

    # --- API documentation ---
    ENABLE_DOCS: bool = True

    # --- CORS / trusted hosts ---
    # NoDecode: skip pydantic-settings' JSON decoding so the values can be given
    # as a plain comma-separated string in .env (parsed by the validator below).
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    ALLOWED_HOSTS: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])

    # --- PostgreSQL ---
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "legal_platform"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_CONNECT_TIMEOUT: int = 3
    DB_ECHO: bool = False

    # --- Redis ---
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_MAX_CONNECTIONS: int = 10
    REDIS_SOCKET_TIMEOUT: int = 3

    # --- MinIO ---
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_SECURE: bool = False
    MINIO_REGION: str | None = None
    MINIO_CONNECT_TIMEOUT: int = 3
    #: Bucket holding case documents. Created on first use if absent.
    MINIO_DOCUMENTS_BUCKET: str = "legal-documents"

    # --- Document management ---
    # Upload ceiling, enforced by the API. In production the reverse proxy should
    # carry a matching `client_max_body_size` so an oversized body is refused at
    # the edge rather than after being buffered.
    MAX_DOCUMENT_SIZE_MB: int = 25
    # Accepted file types. Narrowing this list is the supported way to restrict
    # uploads for a deployment; it can never *widen* the set, because a type the
    # platform has no MIME entry for cannot be served (see `core/documents.py`).
    ALLOWED_DOCUMENT_EXTENSIONS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["pdf", "docx", "doc", "txt", "jpg", "jpeg", "png"]
    )

    # --- OCR processing ---
    # OCR is the first stage of the AI pipeline: it extracts machine-readable
    # text from uploaded documents in the background. Disabling it stops new work
    # from being scheduled; existing results stay readable and retryable.
    OCR_ENABLED: bool = True
    # Which engine implementation to use (see `services/ocr_engine.py`). An
    # unrecognised value falls back to the default rather than failing startup.
    OCR_ENGINE: str = "tesseract"
    # Tesseract language packs to load, joined with `+`. The platform is
    # Arabic/French/English, and a document may mix them, so all three are tried
    # by default — each additional pack costs recognition time, so a
    # single-language deployment should narrow this.
    OCR_LANGUAGES: str = "eng+fra+ara"
    # Resolution PDF pages are rendered at before recognition. 300 DPI is the
    # accuracy floor Tesseract's own guidance recommends for scanned text; lower
    # is markedly worse and higher costs time without improving it.
    OCR_DPI: int = 300
    # Whole-run deadline, covering page rendering *and* recognition.
    OCR_TIMEOUT_SECONDS: int = 180
    # Pages read from a single document. A bound is what keeps a 900-page bundle
    # from being a guaranteed timeout rather than a partial result.
    OCR_MAX_PAGES: int = 100
    # Background workers processing OCR jobs in this API process. OCR is a
    # subprocess-heavy workload, so a small pool keeps it from starving the
    # request handlers.
    OCR_WORKER_CONCURRENCY: int = 2
    # Absolute path to the Tesseract binary. Needed when it is not on PATH,
    # which is the default on Windows. Blank means "use the library's default".
    TESSERACT_CMD: str | None = None
    # Directory holding the Poppler binaries (pdftoppm/pdfinfo) that pdf2image
    # shells out to. Same reasoning as TESSERACT_CMD.
    POPPLER_PATH: str | None = None

    # --- Qdrant ---
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_API_KEY: str | None = None
    QDRANT_HTTPS: bool = False
    QDRANT_TIMEOUT: int = 3
    # Collection holding document chunk embeddings. Created on first index if
    # absent, at the width EMBEDDING_DIMENSIONS declares.
    QDRANT_COLLECTION: str = "document_chunks"
    # Vectors written per Qdrant request. One request per vector pays a round
    # trip each; one request for a 900-page bundle is a payload large enough to
    # time out. This is the middle.
    QDRANT_UPSERT_BATCH_SIZE: int = 128

    # --- Document indexing (chunking + embeddings) ---
    # The second stage of the AI pipeline: it turns the text OCR extracted into
    # vectors in Qdrant. Disabling it stops new work from being scheduled;
    # existing indexes stay readable and re-indexable.
    INDEXING_ENABLED: bool = True
    # Which chunking strategy to use (see `services/chunking.py`). An
    # unrecognised value falls back to the default rather than failing startup.
    INDEX_CHUNKER: str = "recursive-character"
    # Target passage length in *characters*, and how much each passage repeats
    # from its predecessor. Both change what a vector means, so both are recorded
    # on every index: a collection built at one setting is not comparable with
    # one built at another. 1000/200 is the widely used starting point for prose
    # and leaves ample room inside bge-m3's 8192-token window.
    INDEX_CHUNK_SIZE: int = 1000
    INDEX_CHUNK_OVERLAP: int = 200
    # Chunks read from a single document. A bound is what keeps a 900-page bundle
    # from being a guaranteed timeout rather than a partial index.
    INDEX_MAX_CHUNKS: int = 5000
    # Whole-run deadline, covering chunking, embedding, *and* vector writes.
    INDEXING_TIMEOUT_SECONDS: int = 900
    # Background workers processing indexing jobs in this API process. Small,
    # because each holds the embedding model's working memory and saturates a
    # core while encoding.
    INDEXING_WORKER_CONCURRENCY: int = 1

    # --- Semantic search (retrieval) ---
    # The third stage of the AI pipeline: it embeds a natural-language query with
    # the *same* model indexing used and retrieves the nearest passages from
    # Qdrant. Disabling it refuses new searches; nothing already indexed is
    # affected. Retrieval only — no answer generation and no LLM.
    SEARCH_ENABLED: bool = True
    # Results returned when a request does not ask for a number, and the ceiling
    # a request may ask for. The ceiling exists so one call cannot be used to
    # enumerate a case file, and so the response stays a page rather than a dump.
    SEARCH_DEFAULT_LIMIT: int = 10
    SEARCH_MAX_LIMIT: int = 50
    # How far a caller may page into a result set. Vector search degrades with
    # deep offsets (every skipped hit is still scored), and nobody reads past a
    # few pages of relevance-ordered passages.
    SEARCH_MAX_OFFSET: int = 500
    # Longest accepted query, in characters. A natural-language question, not a
    # document: anything longer is a paste, and embedding it would silently
    # truncate at the model's context window instead of saying so.
    SEARCH_QUERY_MAX_LENGTH: int = 1000
    # Similarity floor applied when a request does not set its own. 0.0 keeps
    # every hit Qdrant returns, which is the right default for cosine similarity
    # on a multilingual model — a threshold that is right for French prose is
    # wrong for an Arabic filing, so the platform does not guess one.
    SEARCH_MIN_SCORE: float = 0.0
    # Which ranking strategy to use (see `services/search_ranking.py`). An
    # unrecognised value falls back to the default rather than failing startup.
    SEARCH_RANKER: str = "similarity"
    # Ceiling on the number of documents a metadata filter may resolve to before
    # being pushed into the vector query. Beyond it the request is refused and
    # the caller is asked to narrow, which is honest — silently truncating the
    # set would drop matches with no way for anyone to notice.
    SEARCH_MAX_FILTER_DOCUMENTS: int = 2000
    # Whether the *text* of a user's query may be written to the application log.
    # Off by default: `11-semantic-search.md` forbids logging queries carrying
    # sensitive legal information unless policy explicitly allows it, and this
    # platform's policy (`code-standards.md`) forbids logging document contents.
    # A query is correlated by a salted fingerprint instead — see
    # `core/search.py`.
    SEARCH_LOG_QUERIES: bool = False

    # --- RAG pipeline (grounded answer generation) ---
    # The fourth stage of the AI pipeline: it retrieves passages through the
    # semantic search service, assembles a prompt from them, invokes the
    # configured language model, and returns a grounded answer with citations.
    # Disabling it refuses new questions; documents, indexes, and search are
    # unaffected. Orchestration only — it manages no conversation and owns no UI.
    RAG_ENABLED: bool = True
    # Passages retrieved per question. Deliberately smaller than the search
    # page size: every passage is paid for twice — once in the context window and
    # once in the model's attention — and a long tail of weak matches dilutes a
    # grounded answer rather than improving it. Must not exceed SEARCH_MAX_LIMIT,
    # because retrieval runs through the same search service a user does.
    RAG_RETRIEVAL_TOP_K: int = 8
    # Similarity floor for retrieved passages when a request does not set one.
    # 0.0 for the same reason SEARCH_MIN_SCORE is: a threshold that is right for
    # French prose is wrong for an Arabic filing, so the platform does not guess.
    RAG_MIN_SCORE: float = 0.0
    # How much retrieved text may be placed in one prompt, in *characters*.
    # Characters rather than tokens for the reason INDEX_CHUNK_SIZE records:
    # counting tokens needs the provider's tokenizer loaded, which is exactly the
    # coupling the provider abstraction exists to prevent. 24 000 characters is
    # roughly 6-8k tokens, comfortably inside every model the platform targets.
    RAG_MAX_CONTEXT_CHARACTERS: int = 24000
    # Ceiling on a single passage's contribution, so one very long chunk cannot
    # consume a budget that five passages would have used better. Retrieval
    # quality comes from breadth of evidence.
    RAG_MAX_PASSAGE_CHARACTERS: int = 4000
    # Longest accepted question, in characters. A question, not a document:
    # anything longer is a paste, and it would crowd out the evidence needed to
    # answer it. Must not exceed SEARCH_QUERY_MAX_LENGTH — the question *is* the
    # retrieval query one step later, so a longer one could be accepted here and
    # then refused by the service that has to embed it.
    RAG_QUESTION_MAX_LENGTH: int = 1000
    # Most citations attached to one answer. A bound the prompt states, so the
    # model produces a readable answer rather than a footnote per clause.
    RAG_MAX_CITATIONS: int = 10
    # Whole-request deadline, covering retrieval *and* generation. Must be at
    # least LLM_TIMEOUT_SECONDS, or the model's own deadline could never be
    # reached.
    RAG_TIMEOUT_SECONDS: int = 90
    # Whether the *text* of a user's question may be written to the application
    # log. Off by default, and separate from SEARCH_LOG_QUERIES because the two
    # carry different risk: a search query is a phrase, a question put to the
    # assistant is a sentence about a client's matter. Correlated by a salted
    # fingerprint instead — see `core/rag.py`.
    RAG_LOG_QUESTIONS: bool = False
    # Which prompt template answers a question, and which version of it. Both are
    # recorded on every answer, so two weeks of answers can be compared by prompt
    # rather than only by date.
    RAG_PROMPT_TEMPLATE: str = "rag/answer"
    RAG_PROMPT_VERSION: int = 1

    # --- AI legal assistant (conversations) ---
    # The fifth stage of the AI pipeline: the conversational surface over the RAG
    # pipeline. It persists conversations and messages, streams answers, passes
    # citations through unchanged, suggests follow-up questions, and collects
    # feedback. It generates nothing itself — every answer is the pipeline's.
    # Disabling it refuses new conversations and new messages; existing history
    # stays readable, and `POST /rag/answer` is unaffected.
    ASSISTANT_ENABLED: bool = True
    # Longest accepted conversation title, in characters. Titles are a label in a
    # list, not a summary: a long one is truncated in every surface that shows it.
    ASSISTANT_TITLE_MAX_LENGTH: int = 120
    # Conversations returned per page when a request does not ask for a number,
    # and the ceiling a request may ask for.
    ASSISTANT_PAGE_SIZE: int = 20
    ASSISTANT_MAX_PAGE_SIZE: int = 100
    # Messages returned per page of a conversation transcript. Larger than the
    # conversation page because a transcript is read as a whole: paging every ten
    # turns would make the common case several round trips.
    ASSISTANT_MESSAGE_PAGE_SIZE: int = 50
    ASSISTANT_MAX_MESSAGE_PAGE_SIZE: int = 200
    # Prior turns (a user question plus its answer counts as two messages) carried
    # into a follow-up question so it can be resolved against what came before.
    # Bounded because "avoid unnecessary history growth" is a spec requirement:
    # every carried turn widens the retrieval query, and a question resolved
    # against a conversation from an hour ago retrieves that conversation's
    # subject rather than this question's.
    ASSISTANT_CONTEXT_MESSAGES: int = 4
    # Ceiling on the carried history, in characters, applied after the turn limit.
    # Two limits rather than one because the turn count bounds *how far back* and
    # this bounds *how much* — one long answer must not fill the retrieval query.
    ASSISTANT_CONTEXT_MAX_CHARACTERS: int = 800
    # Messages one conversation may hold. A conversation is a working thread, not
    # an archive: past the ceiling the user starts a new one, which also keeps the
    # transcript loadable in one page-sized request.
    ASSISTANT_MAX_MESSAGES: int = 500
    # Whether answers may be streamed to the client when the configured provider
    # supports it. Turning it off makes every answer arrive whole, which is the
    # same fallback a provider that cannot stream produces.
    ASSISTANT_STREAMING_ENABLED: bool = True
    # Whether a successful, grounded answer is followed by suggested next
    # questions. A second, small model call per answer — so it is a switch an
    # operator can turn off on a metered or rate-limited key, and a failure to
    # produce suggestions never fails the answer they would have followed.
    ASSISTANT_SUGGESTIONS_ENABLED: bool = True
    # How many follow-up questions are suggested. Small on purpose: a list of ten
    # is a menu to read rather than a shortcut to take.
    ASSISTANT_SUGGESTION_COUNT: int = 3
    # Longest suggested follow-up, in characters. Must not exceed
    # RAG_QUESTION_MAX_LENGTH — a suggestion the user cannot send is worse than no
    # suggestion.
    ASSISTANT_SUGGESTION_MAX_LENGTH: int = 160
    # Deadline for the suggestion call. Deliberately small: suggestions are a
    # convenience that arrives after the answer, and one that takes as long as
    # the answer costs more than it saves.
    ASSISTANT_SUGGESTION_TIMEOUT_SECONDS: int = 15
    # Ceiling on the suggestion call's output. Three short questions are perhaps
    # 60 tokens, so this looks absurdly generous — and 256 was **not enough**.
    # A live run against gemini-2.5-flash returned a single suggestion cut off
    # mid-word: the model is a *reasoning* model, its internal thinking is
    # charged against this same budget, and on that call the thoughts consumed
    # roughly 250 of 256 tokens before a single visible one was emitted. The
    # ceiling therefore has to cover the model's deliberation as well as its
    # answer, and it is not a cost — nothing is billed for headroom that is not
    # used. A truncated reply is now also *detected* rather than merely made
    # unlikely (see `core.assistant.parse_suggestions`), because a provider that
    # thinks more on some prompts than on others cannot be sized around exactly.
    ASSISTANT_SUGGESTION_MAX_OUTPUT_TOKENS: int = 1024
    # Which prompt template produces those suggestions, and which version of it.
    # Versioned in the filename exactly as the answer prompt is, for the same
    # reason: a prompt change must be reviewable as a diff of the text sent.
    ASSISTANT_SUGGESTION_PROMPT_TEMPLATE: str = "assistant/followups"
    ASSISTANT_SUGGESTION_PROMPT_VERSION: int = 1

    # --- AI report generation ---
    # The sixth stage of the AI pipeline, and the first non-conversational
    # consumer of it: a Report Generation Agent produces a structured, cited
    # legal report for one case, section by section, through the RAG pipeline.
    # It retrieves nothing, builds no prompt, and calls no model of its own.
    # Disabling it refuses new generations; existing reports stay readable and
    # exportable.
    REPORTS_ENABLED: bool = True
    # Passages retrieved per *section*. Smaller than RAG_RETRIEVAL_TOP_K on
    # purpose: a report asks a dozen narrow questions rather than one broad one,
    # and a long tail of weak matches on each of them costs a dozen context
    # windows to dilute a dozen sections. Must not exceed SEARCH_MAX_LIMIT,
    # because retrieval runs through the same search service a user does.
    REPORT_SECTION_TOP_K: int = 6
    # Similarity floor for a section's passages when the request does not set
    # one. 0.0 for the same reason SEARCH_MIN_SCORE and RAG_MIN_SCORE are.
    REPORT_SECTION_MIN_SCORE: float = 0.0
    # Ceiling on one *section's* output, in provider tokens. Far larger than
    # LLM_MAX_OUTPUT_TOKENS, and the reason is a real finding rather than
    # caution: `gemini-2.5-flash` is a reasoning model and charges its internal
    # deliberation against this same budget. A live run of a report section at
    # 1024 returned **41 visible tokens** — a 151-character section cut off
    # mid-sentence — because roughly 983 of the 1024 went to thinking before a
    # word was emitted. That is the same trap `ASSISTANT_SUGGESTION_MAX_OUTPUT_TOKENS`
    # records, and it bites harder here: a section is prose, not three short
    # questions, so the budget has to cover the deliberation *and* several
    # paragraphs. It is not a cost — nothing is billed for headroom that is not
    # used — and a section that hits it is still reported as `truncated` rather
    # than presented as complete.
    #
    # Set per report rather than by raising LLM_MAX_OUTPUT_TOKENS, deliberately:
    # that setting governs the AI Assistant's answers as well, and this feature
    # has no business changing how a chat reply is bounded.
    REPORT_SECTION_MAX_OUTPUT_TOKENS: int = 4096
    # Sections one report may contain. A ceiling rather than a target: it bounds
    # what a single run can cost in model calls, and every shipped template is
    # comfortably inside it. A template that grew past it would be truncated with
    # a warning rather than failing, which is the same posture INDEX_MAX_CHUNKS
    # takes for a 900-page bundle.
    REPORT_MAX_SECTIONS: int = 20
    # Citations attached to one report, across every section after
    # de-duplication. Larger than RAG_MAX_CITATIONS because a report is a dozen
    # answers rather than one, and a reference list is read once at the end
    # rather than inline.
    REPORT_MAX_CITATIONS: int = 60
    # Whole-run deadline, covering every section's retrieval *and* generation.
    # Must be at least REPORT_SECTION_TIMEOUT_SECONDS, or a section's own
    # deadline could never be reached. Generous, because a seven-section report
    # is seven pipeline runs and this executes in the background where nobody is
    # holding a connection open.
    REPORT_TIMEOUT_SECONDS: int = 900
    # Deadline for one section, passed down to the pipeline as its whole-run
    # budget. Bounding a section is what keeps one unresponsive call from
    # consuming the entire report's budget and leaving the remaining sections
    # unwritten.
    REPORT_SECTION_TIMEOUT_SECONDS: int = 90
    # Background workers producing reports in this API process. One by default:
    # a report is a burst of model calls, and a second worker doubles the rate at
    # which a metered key is spent without making any single report faster.
    REPORT_WORKER_CONCURRENCY: int = 1
    # Reports one user may have queued or generating at once. A report costs a
    # model call per section, so an unbounded queue is a way to spend a
    # deployment's whole token budget from one browser tab.
    REPORT_MAX_ACTIVE_PER_USER: int = 3
    # Reports returned per page when a request does not ask for a number, and the
    # ceiling a request may ask for.
    REPORT_PAGE_SIZE: int = 20
    REPORT_MAX_PAGE_SIZE: int = 100
    # Absolute path to a TrueType font the PDF exporter embeds, **overriding**
    # the automatic search. Blank is the normal case and is not a degraded one:
    # `services/report_export.py` looks through ARABIC_FONT_CANDIDATES (Noto
    # Naskh, Amiri, FreeSerif, and the macOS/Windows system faces) and verifies
    # each against the font's own character map, so a Debian image with
    # fonts-noto-core and a Windows host both export Arabic unconfigured.
    #
    # Set it to pin a particular typeface, or on a host whose font lives
    # somewhere the search does not look. A path that does not exist or has no
    # Arabic coverage is logged and **falls through to the search** rather than
    # failing — a typo here should not cost a deployment an export it could
    # otherwise have produced. Only when nothing is found anywhere is an Arabic
    # PDF refused, and French and English are unaffected either way.
    REPORT_PDF_FONT_PATH: str | None = None
    # Font size and page margin of an exported PDF, in points.
    REPORT_PDF_FONT_SIZE: int = 10
    REPORT_PDF_MARGIN: int = 56

    # --- Real-time events & synchronization ---
    # The platform's event backbone: business modules publish domain events to a
    # central dispatcher, and the WebSocket layer delivers them to the connected
    # clients authorized to receive them. Disabling it refuses new WebSocket
    # connections and makes the dispatcher a no-op; **nothing else changes** —
    # every feature keeps working through its REST API, which is what
    # "graceful degradation when offline" means on the server side too.
    REALTIME_ENABLED: bool = True
    # How long an accepted socket may stay unauthenticated. The credential
    # arrives in the first frame rather than in the URL (see `core/realtime.py`
    # for why), so this window is the whole surface that choice creates —
    # generous enough for a round trip on a slow link, short enough that an idle
    # unauthenticated socket is not a resource anyone can hold.
    REALTIME_AUTH_TIMEOUT_SECONDS: int = 10
    # Connections one account may hold at once. A user legitimately has several —
    # two tabs and a phone — and past this the oldest is closed rather than the
    # newest refused, because the newest is the one the user is looking at.
    REALTIME_MAX_CONNECTIONS_PER_USER: int = 5
    # Connections this process accepts in total. A ceiling rather than a target:
    # each connection costs a task and a queue, and an unbounded count is how one
    # misbehaving client takes the API's event loop down with it.
    REALTIME_MAX_CONNECTIONS: int = 1000
    # Topics one connection may follow. A case workspace subscribes to its case
    # and the documents on screen; a list page subscribes to nothing. Past this a
    # client is following more than a person can be looking at.
    REALTIME_MAX_SUBSCRIPTIONS: int = 200
    # Events buffered per connection before it is considered a slow consumer and
    # closed. Bounded because the alternative to dropping a client that is not
    # reading is holding its backlog in the API's memory until it is.
    REALTIME_SEND_QUEUE_SIZE: int = 256
    # Seconds between server keepalive frames. Well under the 60s idle timeout
    # most reverse proxies and load balancers apply, so an open-but-quiet
    # connection is not closed underneath a user reading a case.
    REALTIME_HEARTBEAT_SECONDS: int = 25
    # How long a missed heartbeat is tolerated before the connection is dropped.
    # A multiple of the interval, so one lost frame on a flaky link does not cost
    # a reconnect. Must be greater than REALTIME_HEARTBEAT_SECONDS.
    REALTIME_IDLE_TIMEOUT_SECONDS: int = 90
    # Client frames accepted per minute per connection. A client sends an
    # `authenticate`, a handful of `subscribe`s, and a `ping` every half minute;
    # this is two orders of magnitude above that, and it is what stops a socket
    # being used to make the server do authorization lookups in a loop.
    REALTIME_MAX_FRAMES_PER_MINUTE: int = 120
    # How long a granted topic subscription is trusted before it is re-checked
    # against the database. The spec requires authorization *before every
    # delivery*, and this is how that is honoured without a query per event per
    # connection: a grant older than this is re-resolved on the next event for
    # that topic, and a caller who has since lost access stops receiving it. Zero
    # means "re-check on every event", which is correct and expensive.
    REALTIME_AUTHORIZATION_TTL_SECONDS: int = 30
    # Event identifiers remembered per connection for duplicate suppression. A
    # reconnecting client is legitimately offered events it already has; this is
    # the server's half of not sending one twice, and the client keeps its own.
    REALTIME_DEDUPE_WINDOW: int = 512

    # --- Prompt templates ---
    # Which prompt backend to use (see `services/prompts.py`). Templates live in
    # `apps/api/prompts/` as versioned `.j2` files under source control.
    PROMPT_LIBRARY: str = "jinja-files"

    # --- LLM provider ---
    # Which provider implementation to use (see `services/llm.py`). An
    # unrecognised value falls back to the default rather than failing startup:
    # an API that refuses to come up over an AI setting would take
    # authentication, cases, and documents down with it.
    LLM_PROVIDER: str = "gemini"
    # The model itself. `ai-architecture.md` names gemini-2.5-flash: strong
    # multilingual reasoning, a long context window, and a free tier. The value
    # is passed to whichever provider is configured, so switching to LiteLLM
    # means changing both this and LLM_PROVIDER.
    LLM_MODEL: str = "gemini-2.5-flash"
    # Credential for the configured provider. Deliberately provider-neutral: the
    # platform must not name a vendor in a setting every deployment sets. Absent
    # is handled rather than fatal — questions answer 503 `llm_unavailable`,
    # `GET /rag/metrics` reports `llm_available: false`, and nothing else on the
    # platform is affected.
    LLM_API_KEY: str | None = None
    # Sampling temperature. Low on purpose: this is a grounded legal assistant,
    # and creative variation between two identical questions about the same
    # filing is a defect rather than a feature.
    LLM_TEMPERATURE: float = 0.2
    # Ceiling on one answer's length, in provider tokens. An answer cut off here
    # is reported as `truncated` rather than presented as complete.
    LLM_MAX_OUTPUT_TOKENS: int = 1024
    # Deadline for a single provider call.
    LLM_TIMEOUT_SECONDS: int = 45
    # Attempts per generation, including the first. Only transient failures are
    # retried (see `services/llm.py`); a rejected credential is not.
    LLM_MAX_ATTEMPTS: int = 3
    # Base delay between retries, doubled per attempt (exponential backoff, as
    # `code-standards.md` requires).
    LLM_RETRY_BACKOFF_SECONDS: float = 1.0

    # --- Embeddings ---
    # Which embedding backend to use (see `services/embedding.py`).
    EMBEDDING_BACKEND: str = "sentence-transformers"
    # The model itself. `ai-architecture.md` names BAAI/bge-m3: multilingual,
    # with strong Arabic and French retrieval, and the same model must be used
    # for indexing and for future query embedding. Changing it requires
    # re-indexing every document, which is why each index records the model it
    # was built with.
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    # Width of that model's vectors. Declared rather than probed so the Qdrant
    # collection can be created before the model is ever loaded; a mismatch with
    # the loaded model is detected and reported rather than written.
    EMBEDDING_DIMENSIONS: int = 1024
    # Passages encoded per forward pass. Bounds peak memory during a run.
    EMBEDDING_BATCH_SIZE: int = 16
    # Torch device the model runs on ("cpu", "cuda", "cuda:0"). Blank lets
    # sentence-transformers pick, which is the right default on a host that may
    # or may not have a GPU.
    EMBEDDING_DEVICE: str | None = None

    # --- Authentication (JWT) ---
    # The signing secret MUST come from the environment. The default below is a
    # clearly-marked development placeholder and is rejected in production.
    JWT_SECRET_KEY: str = DEV_JWT_SECRET_PLACEHOLDER
    JWT_ALGORITHM: Literal["HS256", "HS384", "HS512"] = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # Audience/issuer claims are validated on every token to reject tokens that
    # were minted for a different service.
    JWT_ISSUER: str = "legal-platform-api"
    JWT_AUDIENCE: str = "legal-platform-web"

    # --- Password hashing (bcrypt) ---
    BCRYPT_ROUNDS: int = 12

    # --- Login throttling (brute-force protection) ---
    # After MAX_FAILED_LOGIN_ATTEMPTS consecutive failures inside
    # LOGIN_FAILURE_WINDOW_MINUTES, further attempts are refused with HTTP 429
    # for LOGIN_LOCKOUT_MINUTES. Counted per account and per client IP.
    MAX_FAILED_LOGIN_ATTEMPTS: int = 5
    LOGIN_FAILURE_WINDOW_MINUTES: int = 15
    LOGIN_LOCKOUT_MINUTES: int = 15
    # Whether to derive the client IP from X-Forwarded-For. Enable ONLY when the
    # app sits behind a trusted reverse proxy (e.g. Nginx) that overwrites the
    # header — otherwise clients can spoof it to evade per-IP throttling or to
    # get another user's IP blocked.
    TRUST_PROXY_HEADERS: bool = False

    # --- Refresh token cookie ---
    # The refresh token is delivered as an httpOnly cookie so browser JavaScript
    # cannot read it (XSS-resistant), with SameSite protection against CSRF.
    REFRESH_COOKIE_NAME: str = "legal_platform_refresh"
    REFRESH_COOKIE_SECURE: bool = False  # MUST be true in production (HTTPS)
    REFRESH_COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "strict"
    REFRESH_COOKIE_DOMAIN: str | None = None

    @field_validator("CORS_ORIGINS", "ALLOWED_HOSTS", "ALLOWED_DOCUMENT_EXTENSIONS", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        """Parse a comma-separated string into a list (a real list passes through)."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("ALLOWED_DOCUMENT_EXTENSIONS")
    @classmethod
    def _normalize_document_extensions(cls, value: list[str]) -> list[str]:
        """Accept ``.PDF``, ``pdf``, and ``PDF`` as the same entry.

        An empty list would disable uploads entirely, which is never the intent
        of setting the variable — far more likely a blank line in ``.env`` — so it
        is rejected rather than silently honoured.
        """
        normalized = [item.strip().lstrip(".").lower() for item in value]
        cleaned = [item for item in normalized if item]
        if not cleaned:
            raise ValueError("ALLOWED_DOCUMENT_EXTENSIONS must list at least one file type")
        return cleaned

    @field_validator("OCR_LANGUAGES")
    @classmethod
    def _normalize_ocr_languages(cls, value: str) -> str:
        """Accept ``eng, fra`` and ``eng+fra`` alike, and reject an empty list.

        Tesseract's own syntax is ``+``-joined, but a comma is the separator
        every other list-valued setting in this file uses, so both are accepted
        and normalized to what the library expects. An empty value would make
        every extraction fail with an opaque engine error, so it is rejected here
        instead.
        """
        parts = [part.strip().lower() for part in value.replace(",", "+").split("+")]
        cleaned = [part for part in parts if part]
        if not cleaned:
            raise ValueError("OCR_LANGUAGES must list at least one Tesseract language code")
        return "+".join(dict.fromkeys(cleaned))

    @field_validator(
        "REDIS_PASSWORD",
        "MINIO_REGION",
        "QDRANT_API_KEY",
        "REFRESH_COOKIE_DOMAIN",
        "TESSERACT_CMD",
        "POPPLER_PATH",
        "EMBEDDING_DEVICE",
        "LLM_API_KEY",
        "REPORT_PDF_FONT_PATH",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, value: object) -> object:
        """Treat a blank value in .env (e.g. ``REDIS_PASSWORD=``) as unset."""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("POSTGRES_PASSWORD")
    @classmethod
    def _require_strong_db_password_in_production(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("ENVIRONMENT") is Environment.PRODUCTION and value in {"", "postgres"}:
            raise ValueError("POSTGRES_PASSWORD must be set to a non-default value in production")
        return value

    @field_validator(
        "ACCESS_TOKEN_EXPIRE_MINUTES",
        "REFRESH_TOKEN_EXPIRE_DAYS",
        "BCRYPT_ROUNDS",
        "MAX_FAILED_LOGIN_ATTEMPTS",
        "LOGIN_FAILURE_WINDOW_MINUTES",
        "LOGIN_LOCKOUT_MINUTES",
        "MAX_DOCUMENT_SIZE_MB",
        "OCR_DPI",
        "OCR_TIMEOUT_SECONDS",
        "OCR_MAX_PAGES",
        "OCR_WORKER_CONCURRENCY",
        "SEARCH_DEFAULT_LIMIT",
        "SEARCH_MAX_LIMIT",
        "SEARCH_QUERY_MAX_LENGTH",
        "SEARCH_MAX_FILTER_DOCUMENTS",
        "RAG_RETRIEVAL_TOP_K",
        "RAG_MAX_CONTEXT_CHARACTERS",
        "RAG_MAX_PASSAGE_CHARACTERS",
        "RAG_QUESTION_MAX_LENGTH",
        "RAG_MAX_CITATIONS",
        "RAG_TIMEOUT_SECONDS",
        "RAG_PROMPT_VERSION",
        "LLM_MAX_OUTPUT_TOKENS",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_ATTEMPTS",
        "ASSISTANT_TITLE_MAX_LENGTH",
        "ASSISTANT_PAGE_SIZE",
        "ASSISTANT_MAX_PAGE_SIZE",
        "ASSISTANT_MESSAGE_PAGE_SIZE",
        "ASSISTANT_MAX_MESSAGE_PAGE_SIZE",
        "ASSISTANT_MAX_MESSAGES",
        "ASSISTANT_SUGGESTION_MAX_LENGTH",
        "ASSISTANT_SUGGESTION_TIMEOUT_SECONDS",
        "ASSISTANT_SUGGESTION_MAX_OUTPUT_TOKENS",
        "ASSISTANT_SUGGESTION_PROMPT_VERSION",
        "REPORT_SECTION_TOP_K",
        "REPORT_SECTION_MAX_OUTPUT_TOKENS",
        "REPORT_MAX_SECTIONS",
        "REPORT_MAX_CITATIONS",
        "REPORT_TIMEOUT_SECONDS",
        "REPORT_SECTION_TIMEOUT_SECONDS",
        "REPORT_WORKER_CONCURRENCY",
        "REPORT_MAX_ACTIVE_PER_USER",
        "REPORT_PAGE_SIZE",
        "REPORT_MAX_PAGE_SIZE",
        "REPORT_PDF_FONT_SIZE",
        "REPORT_PDF_MARGIN",
        "REALTIME_AUTH_TIMEOUT_SECONDS",
        "REALTIME_MAX_CONNECTIONS_PER_USER",
        "REALTIME_MAX_CONNECTIONS",
        "REALTIME_MAX_SUBSCRIPTIONS",
        "REALTIME_SEND_QUEUE_SIZE",
        "REALTIME_HEARTBEAT_SECONDS",
        "REALTIME_IDLE_TIMEOUT_SECONDS",
        "REALTIME_MAX_FRAMES_PER_MINUTE",
        "REALTIME_DEDUPE_WINDOW",
    )
    @classmethod
    def _require_positive(cls, value: int, info: ValidationInfo) -> int:
        if value <= 0:
            raise ValueError(f"{info.field_name} must be a positive integer")
        return value

    @model_validator(mode="after")
    def _validate_search_limits(self) -> Settings:
        """Keep the search page size and its ceiling coherent.

        A default above the maximum would make every unqualified search fail
        validation against a bound the caller never chose — the kind of
        misconfiguration that is invisible until the first request.
        """
        if self.SEARCH_DEFAULT_LIMIT > self.SEARCH_MAX_LIMIT:
            raise ValueError("SEARCH_DEFAULT_LIMIT must not exceed SEARCH_MAX_LIMIT")
        if self.SEARCH_MAX_OFFSET < 0:
            raise ValueError("SEARCH_MAX_OFFSET must not be negative")
        return self

    @model_validator(mode="after")
    def _validate_rag_limits(self) -> Settings:
        """Keep the RAG budgets coherent with the services they run through.

        Five couplings, each of which is invisible until the first question:

        * retrieval runs through the **semantic search service**, so asking for
          more passages than that service will return means every answer is
          quietly built on fewer sources than configured;
        * the question **is** the retrieval query, so one this endpoint accepts
          and that endpoint refuses would fail deep inside the pipeline with a
          message about a search;
        * a per-passage cap above the whole context budget is not a cap;
        * a model deadline longer than the whole-request deadline can never be
          reached, so the request would always fail on the outer one and the
          provider's own timeout would never take effect;
        * a similarity floor outside ``[-1, 1]`` is not a cosine similarity, so
          it would either match everything or nothing.
        """
        if self.RAG_RETRIEVAL_TOP_K > self.SEARCH_MAX_LIMIT:
            raise ValueError("RAG_RETRIEVAL_TOP_K must not exceed SEARCH_MAX_LIMIT")
        if self.RAG_QUESTION_MAX_LENGTH > self.SEARCH_QUERY_MAX_LENGTH:
            raise ValueError("RAG_QUESTION_MAX_LENGTH must not exceed SEARCH_QUERY_MAX_LENGTH")
        if self.RAG_MAX_PASSAGE_CHARACTERS > self.RAG_MAX_CONTEXT_CHARACTERS:
            raise ValueError(
                "RAG_MAX_PASSAGE_CHARACTERS must not exceed RAG_MAX_CONTEXT_CHARACTERS"
            )
        if self.LLM_TIMEOUT_SECONDS > self.RAG_TIMEOUT_SECONDS:
            raise ValueError("LLM_TIMEOUT_SECONDS must not exceed RAG_TIMEOUT_SECONDS")
        if not -1.0 <= self.RAG_MIN_SCORE <= 1.0:
            raise ValueError("RAG_MIN_SCORE must be between -1.0 and 1.0")
        if not 0.0 <= self.LLM_TEMPERATURE <= 2.0:
            raise ValueError("LLM_TEMPERATURE must be between 0.0 and 2.0")
        if self.LLM_RETRY_BACKOFF_SECONDS < 0:
            raise ValueError("LLM_RETRY_BACKOFF_SECONDS must not be negative")
        return self

    @model_validator(mode="after")
    def _validate_assistant_limits(self) -> Settings:
        """Keep the assistant's budgets coherent with the pipeline beneath it.

        Five couplings, and every one of them is invisible until a user is
        already mid-conversation:

        * a **page size above its ceiling** would make every unqualified list
          request fail against a bound the caller never chose — the same
          misconfiguration :meth:`_validate_search_limits` refuses for search;
        * a **suggestion longer than a question** is a suggestion the user
          cannot send: clicking it would be refused by the very endpoint it was
          offered by;
        * the **suggestion deadline** must fit inside the provider's own, or the
          smaller number it is supposed to impose would never take effect;
        * **carried history** must fit inside the question budget, because a
          follow-up is resolved by prefixing that history to the question and the
          result is what the retrieval query becomes;
        * a **conversation ceiling below one page** of messages would make the
          transcript endpoint unable to return a full conversation.
        """
        if self.ASSISTANT_PAGE_SIZE > self.ASSISTANT_MAX_PAGE_SIZE:
            raise ValueError("ASSISTANT_PAGE_SIZE must not exceed ASSISTANT_MAX_PAGE_SIZE")
        if self.ASSISTANT_MESSAGE_PAGE_SIZE > self.ASSISTANT_MAX_MESSAGE_PAGE_SIZE:
            raise ValueError(
                "ASSISTANT_MESSAGE_PAGE_SIZE must not exceed ASSISTANT_MAX_MESSAGE_PAGE_SIZE"
            )
        if self.ASSISTANT_SUGGESTION_MAX_LENGTH > self.RAG_QUESTION_MAX_LENGTH:
            raise ValueError(
                "ASSISTANT_SUGGESTION_MAX_LENGTH must not exceed RAG_QUESTION_MAX_LENGTH"
            )
        if self.ASSISTANT_SUGGESTION_TIMEOUT_SECONDS > self.LLM_TIMEOUT_SECONDS:
            raise ValueError(
                "ASSISTANT_SUGGESTION_TIMEOUT_SECONDS must not exceed LLM_TIMEOUT_SECONDS"
            )
        if self.ASSISTANT_CONTEXT_MAX_CHARACTERS >= self.RAG_QUESTION_MAX_LENGTH:
            raise ValueError(
                "ASSISTANT_CONTEXT_MAX_CHARACTERS must be smaller than RAG_QUESTION_MAX_LENGTH"
            )
        if self.ASSISTANT_MAX_MESSAGES < self.ASSISTANT_MESSAGE_PAGE_SIZE:
            raise ValueError(
                "ASSISTANT_MAX_MESSAGES must not be smaller than ASSISTANT_MESSAGE_PAGE_SIZE"
            )
        if self.ASSISTANT_CONTEXT_MESSAGES < 0:
            raise ValueError("ASSISTANT_CONTEXT_MESSAGES must not be negative")
        if self.ASSISTANT_SUGGESTION_COUNT < 0:
            raise ValueError("ASSISTANT_SUGGESTION_COUNT must not be negative")
        return self

    @model_validator(mode="after")
    def _validate_report_limits(self) -> Settings:
        """Keep the report budgets coherent with the pipeline they run through.

        Four couplings, and every one of them is invisible until a report is
        already being generated in the background, where nobody is watching:

        * a **section retrieves through the semantic search service**, so asking
          for more passages than that service will return means every section is
          quietly built on fewer sources than configured — the same coupling
          :meth:`_validate_rag_limits` refuses for ``RAG_RETRIEVAL_TOP_K``;
        * a **section deadline longer than the whole run's** can never be
          reached, so the smaller number the operator set for one section would
          never take effect;
        * a **page size above its ceiling** would make every unqualified history
          request fail against a bound the caller never chose;
        * a **similarity floor outside ``[-1, 1]``** is not a cosine similarity,
          so it would either match everything or nothing.
        """
        if self.REPORT_SECTION_TOP_K > self.SEARCH_MAX_LIMIT:
            raise ValueError("REPORT_SECTION_TOP_K must not exceed SEARCH_MAX_LIMIT")
        if self.REPORT_SECTION_TIMEOUT_SECONDS > self.REPORT_TIMEOUT_SECONDS:
            raise ValueError(
                "REPORT_SECTION_TIMEOUT_SECONDS must not exceed REPORT_TIMEOUT_SECONDS"
            )
        if self.REPORT_PAGE_SIZE > self.REPORT_MAX_PAGE_SIZE:
            raise ValueError("REPORT_PAGE_SIZE must not exceed REPORT_MAX_PAGE_SIZE")
        if not -1.0 <= self.REPORT_SECTION_MIN_SCORE <= 1.0:
            raise ValueError("REPORT_SECTION_MIN_SCORE must be between -1.0 and 1.0")
        return self

    @model_validator(mode="after")
    def _validate_realtime_limits(self) -> Settings:
        """Keep the connection budgets coherent with one another.

        Three couplings, and each is invisible until users are already connected:

        * an **idle timeout at or below the heartbeat interval** drops every
          connection on schedule — the server's own keepalive would arrive after
          the deadline it exists to reset;
        * a **per-user ceiling above the process ceiling** is not a ceiling: one
          account could fill the process and the narrower limit would never
          apply;
        * a **negative authorization TTL** is not a shorter re-check window, it is
          an undefined one. Zero is meaningful (re-check on every delivery) and is
          therefore allowed.
        """
        if self.REALTIME_IDLE_TIMEOUT_SECONDS <= self.REALTIME_HEARTBEAT_SECONDS:
            raise ValueError(
                "REALTIME_IDLE_TIMEOUT_SECONDS must be greater than REALTIME_HEARTBEAT_SECONDS"
            )
        if self.REALTIME_MAX_CONNECTIONS_PER_USER > self.REALTIME_MAX_CONNECTIONS:
            raise ValueError(
                "REALTIME_MAX_CONNECTIONS_PER_USER must not exceed REALTIME_MAX_CONNECTIONS"
            )
        if self.REALTIME_AUTHORIZATION_TTL_SECONDS < 0:
            raise ValueError("REALTIME_AUTHORIZATION_TTL_SECONDS must not be negative")
        return self

    @model_validator(mode="after")
    def _validate_production_invariants(self) -> Settings:
        """Enforce safe defaults for production deployments (fail fast)."""
        if self.ENVIRONMENT is Environment.PRODUCTION:
            if self.DEBUG:
                raise ValueError("DEBUG must be disabled in production")
            if "*" in self.ALLOWED_HOSTS:
                raise ValueError("ALLOWED_HOSTS must not use the '*' wildcard in production")
            if self.MINIO_SECRET_KEY == "minioadmin":
                raise ValueError("MINIO_SECRET_KEY must be set to a non-default value in production")
            if self.JWT_SECRET_KEY == DEV_JWT_SECRET_PLACEHOLDER:
                raise ValueError("JWT_SECRET_KEY must be set to a real secret in production")
            if len(self.JWT_SECRET_KEY) < MIN_JWT_SECRET_LENGTH:
                raise ValueError(
                    f"JWT_SECRET_KEY must be at least {MIN_JWT_SECRET_LENGTH} characters in production"
                )
            if not self.REFRESH_COOKIE_SECURE:
                raise ValueError("REFRESH_COOKIE_SECURE must be true in production (HTTPS only)")
            if self.REFRESH_COOKIE_SAMESITE == "none" and not self.REFRESH_COOKIE_SECURE:
                raise ValueError("REFRESH_COOKIE_SAMESITE='none' requires REFRESH_COOKIE_SECURE=true")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT is Environment.PRODUCTION

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_testing(self) -> bool:
        return self.ENVIRONMENT is Environment.TESTING

    @computed_field  # type: ignore[prop-decorator]
    @property
    def DATABASE_URL(self) -> str:
        """SQLAlchemy connection string for PostgreSQL (psycopg 3 driver)."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+psycopg",
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD,
                host=self.POSTGRES_HOST,
                port=self.POSTGRES_PORT,
                path=self.POSTGRES_DB,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def REDIS_URL(self) -> str:
        """Connection string for Redis."""
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def access_token_ttl(self) -> timedelta:
        """Lifetime of an access token."""
        return timedelta(minutes=self.ACCESS_TOKEN_EXPIRE_MINUTES)

    @property
    def refresh_token_ttl(self) -> timedelta:
        """Lifetime of a refresh token."""
        return timedelta(days=self.REFRESH_TOKEN_EXPIRE_DAYS)

    @property
    def max_document_size_bytes(self) -> int:
        """Upload ceiling in bytes, derived from the configured megabytes."""
        return self.MAX_DOCUMENT_SIZE_MB * 1024 * 1024

    @property
    def login_failure_window(self) -> timedelta:
        """How long consecutive failed login attempts are remembered."""
        return timedelta(minutes=self.LOGIN_FAILURE_WINDOW_MINUTES)

    @property
    def login_lockout_duration(self) -> timedelta:
        """How long login is refused once the failure threshold is reached."""
        return timedelta(minutes=self.LOGIN_LOCKOUT_MINUTES)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()


settings = get_settings()
