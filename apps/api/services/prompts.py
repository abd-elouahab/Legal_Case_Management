"""The prompt-management boundary.

``ai-architecture.md``: *"Prompt templates should be stored separately from
application logic. Templates should be reusable and versioned."*
``12-rag-pipeline.md``: *"Prompt templates should be reusable and configurable
[…] Do not hardcode prompts throughout the codebase."*
``code-standards.md``: *"Prompt templates must be version-controlled. AI prompts
must remain independent from application logic."*

This module is that boundary, and it is the **only** place in the platform that
imports Jinja2 or reads a template file. The shape mirrors every other seam in
this codebase (:mod:`services.ocr_engine`, :mod:`services.chunking`,
:mod:`services.embedding`, :mod:`services.vector_store`,
:mod:`services.vector_search`, :mod:`services.search_ranking`):

* :class:`PromptLibrary` is the protocol :mod:`services.rag` depends on;
* :class:`JinjaPromptLibrary` is the implementation;
* :func:`get_prompt_library` resolves it, so a database-backed or
  remotely-managed prompt store is one class plus one registry entry.

**Templates live on disk, not in Python.** ``apps/api/prompts/`` is a directory
of ``.j2`` files under version control, so a prompt change is reviewable as a
diff of the text that was actually sent to the model — which is the entire point
of "version-controlled" and is not true of a triple-quoted string buried in a
service.

**Versioning is in the filename, not in a comment.** ``answer.v1.system.j2`` and
``answer.v2.system.j2`` coexist; ``RAG_PROMPT_VERSION`` pins a deployment to one,
and every answer records the version it was produced with. That last part is what
makes an evaluation run (`architecture.md` names Ragas and DeepEval) able to
compare two prompts rather than merely two days.

**Rendering is strict and unescaped, and both halves are deliberate.**
``StrictUndefined`` turns a mistyped variable into a loud failure instead of a
silently empty section of the prompt — a prompt that quietly lost its context
block would produce ungrounded answers that look completely normal. Autoescaping
is **off** because the output is plain text for a model, not HTML for a browser:
escaping would rewrite the apostrophes, quotation marks, and angle brackets of a
French or Arabic legal passage into entities the model then has to read through.

**Untrusted text is delimited, not escaped.** The user's question and the
retrieved passages are both attacker-influenceable in principle, so the templates
fence them inside explicit markers and the system prompt states that everything
inside those markers is *data to be read*, never instructions to be followed.
That is a prompt-level control rather than a rendering-level one, because there
is no character-escaping scheme that makes a sentence stop being a sentence.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

import structlog

from core.config import settings

logger = structlog.get_logger(__name__)

#: Where the templates live: ``apps/api/prompts``.
#:
#: Resolved from this file rather than from the working directory, because the
#: API runs from ``apps/api``, the tests from the repo root, and Alembic from
#: somewhere else again — the same reason :mod:`core.config` resolves ``.env``
#: from ``__file__``.
DEFAULT_PROMPT_ROOT = Path(__file__).resolve().parents[1] / "prompts"

#: The two parts every prompt in this platform has.
#:
#: Separate files rather than one file with a separator, because they are sent
#: to the provider as two different things — a system instruction and a user
#: turn — and a provider that accepts only one would have to *join* them, which
#: is a decision for :mod:`services.llm` rather than for a text file.
SYSTEM_PART = "system"
USER_PART = "user"

#: Filename suffix. Jinja's own convention, and it is what makes editors, syntax
#: highlighting, and ``ruff``'s exclusion of non-Python files all behave.
TEMPLATE_SUFFIX = ".j2"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class PromptError(Exception):
    """A prompt could not be produced.

    Deliberately **not** an :class:`~core.exceptions.AppException`, exactly as
    :class:`~services.embedding.EmbeddingError` is not: this module is a library
    boundary, and turning its failure into a status line is the service's job.

    A prompt failure is always a *deployment* fault — a missing file, a template
    referring to a variable the caller did not supply — never something a client
    can provoke, which is why it carries no user-facing message of its own.
    """


class PromptNotFoundError(PromptError):
    """No template exists under that name and version."""


class PromptRenderError(PromptError):
    """The template exists but could not be rendered with the given context."""


# --------------------------------------------------------------------------- #
# What comes out
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """One assembled prompt: the two parts, and which template produced them.

    The identity travels with the text rather than being read from configuration
    afterwards, for the same reason an index records the embedding model it was
    built with: configuration is *current* and an answer is *historical*, so
    comparing two answers is only possible if each says which prompt produced it.
    """

    name: str
    version: int
    system: str
    user: str

    @property
    def character_count(self) -> int:
        """Total size of the prompt, in characters.

        Reported on the answer and used by the metrics recorder. In characters
        rather than tokens because the platform's budgets are in characters (see
        :func:`~core.rag.fit_to_budget`, and the same decision
        ``INDEX_CHUNK_SIZE`` records) — a token count needs the provider's
        tokenizer, which is exactly the coupling the provider abstraction exists
        to prevent.
        """
        return len(self.system) + len(self.user)


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class PromptLibrary(Protocol):
    """What the RAG pipeline requires of a prompt store.

    Four members, and none of them mentions a document, a passage, a user, or a
    model. A prompt library is handed a name, a version, and a mapping of values,
    and returns text — that narrowness is what lets the templates be replaced by a
    database, a config service, or an experiment framework without the pipeline
    noticing.
    """

    @property
    def name(self) -> str:
        """Stable identifier of the backend ("jinja-files")."""
        ...

    def is_available(self) -> bool:
        """Whether prompts can actually be loaded here, right now."""
        ...

    def versions(self, name: str) -> list[int]:
        """Every version of a template, ascending. Empty when there are none."""
        ...

    def render(
        self, name: str, *, version: int | None = None, context: Mapping[str, Any]
    ) -> RenderedPrompt:
        """Render both parts of a template.

        Args:
            name: template name, as a path under the root ("rag/answer").
            version: which version to use. ``None`` takes the highest available,
                which is the right default for a script and the wrong one for a
                deployment — so the pipeline always passes an explicit version.
            context: values the template substitutes.

        Raises:
            PromptNotFoundError: no such template, or no such version.
            PromptRenderError: the template could not be rendered.
        """
        ...


# --------------------------------------------------------------------------- #
# Jinja2 over the filesystem
# --------------------------------------------------------------------------- #


class JinjaPromptLibrary:
    """Versioned ``.j2`` templates read from ``apps/api/prompts``.

    Templates are compiled once and cached by Jinja's own loader cache, which is
    what makes rendering a per-request operation rather than a per-request disk
    read. The environment is built lazily and behind a lock for the same reason
    the embedding model is: the first request pays for it, startup does not
    depend on it, and two concurrent first requests must not build two.
    """

    #: The identifier recorded for this backend.
    name = "jinja-files"

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or DEFAULT_PROMPT_ROOT
        self._environment: Any | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ identity #

    @property
    def root(self) -> Path:
        """Directory the templates are read from."""
        return self._root

    def is_available(self) -> bool:
        """Whether Jinja2 is installed and the template directory exists.

        Probed rather than assumed, so a deployment missing its prompts reports
        ``false`` on the monitoring endpoint and answers 503 with a cause,
        instead of surfacing as a stack trace on a user's first question — the
        same posture a missing Tesseract and a missing embedding model take.
        """
        try:
            self._env()
        except PromptError:
            return False
        return self._root.is_dir()

    # ------------------------------------------------------------ discovery #

    def versions(self, name: str) -> list[int]:
        """Every version of ``name`` present on disk, ascending.

        Derived from the filenames rather than from a manifest, so adding a
        version is adding two files and nothing else — a manifest would be a
        second place to forget.

        A version counts only when **both** parts are present: a ``v2`` with a
        system prompt and no user prompt is a half-finished edit, and offering it
        as available would let ``version=None`` silently select it.
        """
        directory = (self._root / name).parent
        stem = Path(name).name
        if not directory.is_dir():
            return []

        found: set[int] = set()
        for path in directory.glob(f"{stem}.v*.{SYSTEM_PART}{TEMPLATE_SUFFIX}"):
            version = _version_of(path.name, stem=stem, part=SYSTEM_PART)
            if version is None:
                continue
            partner = directory / f"{stem}.v{version}.{USER_PART}{TEMPLATE_SUFFIX}"
            if partner.is_file():
                found.add(version)

        return sorted(found)

    # -------------------------------------------------------------- render #

    def render(
        self, name: str, *, version: int | None = None, context: Mapping[str, Any]
    ) -> RenderedPrompt:
        """Render a template's system and user parts with one set of values.

        Both parts get the **same** context, deliberately: a variable that means
        one thing in the instructions and another in the question would be a bug
        nobody could see from either file alone.

        Raises:
            PromptNotFoundError: no such template, or no such version.
            PromptRenderError: the template referred to something not supplied,
                or failed to render.
        """
        resolved = self._resolve_version(name, version)

        return RenderedPrompt(
            name=name,
            version=resolved,
            system=self._render_part(name, resolved, SYSTEM_PART, context),
            user=self._render_part(name, resolved, USER_PART, context),
        )

    # ------------------------------------------------------------- helpers #

    def _resolve_version(self, name: str, version: int | None) -> int:
        """Pin a version, or take the highest available.

        Raises:
            PromptNotFoundError: the template has no versions at all, or not the
                one asked for.
        """
        available = self.versions(name)
        if not available:
            logger.error("prompt_template_missing", template=name, root=str(self._root))
            raise PromptNotFoundError(f"No prompt template named {name!r}.")

        if version is None:
            return available[-1]

        if version not in available:
            logger.error(
                "prompt_version_missing",
                template=name,
                requested=version,
                available=available,
            )
            raise PromptNotFoundError(f"Prompt template {name!r} has no version {version}.")

        return version

    def _render_part(
        self, name: str, version: int, part: str, context: Mapping[str, Any]
    ) -> str:
        """Render one file, translating every Jinja failure at this boundary.

        The library's own exception text quotes the template *and the values it
        was substituting* — which here are the user's question and passages of a
        legal document. It is therefore never carried into the raised error and
        never logged; the template's identity is logged instead, which is what an
        operator needs to find the file.
        """
        from jinja2 import TemplateError as JinjaTemplateError
        from jinja2 import TemplateNotFound

        relative = f"{name}.v{version}.{part}{TEMPLATE_SUFFIX}"

        try:
            template = self._env().get_template(relative.replace("\\", "/"))
            return str(template.render(**context)).strip()
        except TemplateNotFound as exc:
            logger.error("prompt_template_missing", template=relative, root=str(self._root))
            raise PromptNotFoundError(f"Prompt template {relative!r} was not found.") from exc
        except JinjaTemplateError as exc:
            logger.error(
                "prompt_render_failed",
                template=relative,
                error_type=type(exc).__name__,
            )
            raise PromptRenderError(f"Prompt template {relative!r} could not be rendered.") from exc

    def _env(self) -> Any:
        """Build (and cache) the Jinja environment.

        Raises:
            PromptError: Jinja2 is not installed.
        """
        if self._environment is not None:
            return self._environment

        with self._lock:
            # Re-checked inside the lock, exactly as the embedder's model load
            # is: two threads can both pass the check above.
            if self._environment is not None:
                return self._environment

            try:
                from jinja2 import Environment, FileSystemLoader, StrictUndefined
            except ImportError as exc:  # pragma: no cover - declared dependency
                logger.error("prompt_library_unavailable", reason="library_missing")
                raise PromptError("The prompt template library is not installed.") from exc

            self._environment = Environment(
                loader=FileSystemLoader(str(self._root), encoding="utf-8"),
                # Plain text for a model, not markup for a browser. See the
                # module docstring for why escaping would be actively harmful.
                autoescape=False,
                # A mistyped variable must fail loudly. The alternative is a
                # prompt that silently lost its context block and produces
                # ungrounded answers that look entirely normal.
                undefined=StrictUndefined,
                trim_blocks=True,
                lstrip_blocks=True,
                keep_trailing_newline=False,
            )
            return self._environment


def _version_of(filename: str, *, stem: str, part: str) -> int | None:
    """Pull the version out of ``answer.v3.system.j2``, or ``None`` if it is not one."""
    prefix = f"{stem}.v"
    suffix = f".{part}{TEMPLATE_SUFFIX}"
    if not filename.startswith(prefix) or not filename.endswith(suffix):
        return None

    digits = filename[len(prefix) : -len(suffix)]
    if not digits.isdigit():
        return None
    return int(digits)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

#: Every prompt backend this build can be configured to use.
#:
#: Adding one is a class implementing :class:`PromptLibrary` plus an entry here —
#: in the same shape as :data:`~services.embedding.EMBEDDER_FACTORIES`.
PROMPT_LIBRARY_FACTORIES: Mapping[str, type[JinjaPromptLibrary]] = MappingProxyType(
    {JinjaPromptLibrary.name: JinjaPromptLibrary}
)

#: The one library the process shares, built on first use.
#:
#: Shared rather than per request because it owns Jinja's compiled-template
#: cache, and a per-request environment would recompile every template on every
#: question — the same reasoning that makes the embedder process-wide, at a much
#: smaller scale.
_shared: dict[str, PromptLibrary] = {}
_shared_lock = threading.Lock()


def available_prompt_libraries() -> list[str]:
    """Every prompt-library identifier this build can be configured to use."""
    return sorted(PROMPT_LIBRARY_FACTORIES)


def get_prompt_library(identifier: str | None = None) -> PromptLibrary:
    """Return the configured prompt library, shared across the process.

    Falls back to the default backend for an unrecognised identifier rather than
    failing startup, with the fallback logged so the misconfiguration is visible
    — the same posture :func:`~services.embedding.get_embedder` and
    :func:`~services.search_ranking.get_ranker` take.
    """
    wanted = (identifier or settings.PROMPT_LIBRARY).strip().lower()

    factory = PROMPT_LIBRARY_FACTORIES.get(wanted)
    if factory is None:
        logger.warning(
            "prompt_library_unknown", requested=wanted, fallback=JinjaPromptLibrary.name
        )
        wanted = JinjaPromptLibrary.name
        factory = JinjaPromptLibrary

    cached = _shared.get(wanted)
    if cached is not None:
        return cached

    with _shared_lock:
        existing = _shared.get(wanted)
        if existing is not None:
            return existing
        built: PromptLibrary = factory()
        _shared[wanted] = built
        return built


def reset_prompt_library_cache() -> None:
    """Discard the shared prompt library.

    For tests, and for a deployment that edits templates at runtime. Not called
    by the application: templates are read-only in a running deployment, so
    there is nothing to invalidate in normal operation.
    """
    with _shared_lock:
        _shared.clear()


__all__ = [
    "DEFAULT_PROMPT_ROOT",
    "PROMPT_LIBRARY_FACTORIES",
    "SYSTEM_PART",
    "TEMPLATE_SUFFIX",
    "USER_PART",
    "JinjaPromptLibrary",
    "PromptError",
    "PromptLibrary",
    "PromptNotFoundError",
    "PromptRenderError",
    "RenderedPrompt",
    "available_prompt_libraries",
    "get_prompt_library",
    "reset_prompt_library_cache",
]
