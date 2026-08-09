"""The email template boundary.

``17-email-delivery-channel.md``: *"Templates should remain independent from
application logic. Support HTML and Plain Text. Templates should support variable
substitution."*

This module is that boundary. It is deliberately a near-twin of
:mod:`services.prompts` — a protocol, a Jinja implementation over versioned files
on disk, and a registry — because the requirement is the same one: text that is
*sent out* belongs in a reviewable file rather than in a triple-quoted string
buried in a service. Templates live in ``apps/api/emails/`` as
``<name>.v<version>.<part>.j2``, so a wording change is a diff of the message
that was actually delivered.

**Two differences from the prompt library, and both are load-bearing.**

*Autoescaping is on for the HTML part and off for the text part.*
:mod:`services.prompts` turns escaping off everywhere, because its output is
plain text for a model and escaping would rewrite the punctuation of a French or
Arabic passage into entities the model has to read through. Here the HTML part is
**markup rendered in somebody's mail client**, and a case number, a filename, or
an administrator's announcement is interpolated into it — so an apostrophe must
stay an apostrophe in the text part and a ``<`` must become ``&lt;`` in the HTML
one. That is not a theoretical concern: ``POST /notifications/announcements``
puts a human's words into ``{message}``, which reaches both parts.

*Rendering is strict.* ``StrictUndefined``, exactly as the prompt library uses it
and for a stronger version of the same reason: a template that silently lost its
``action_url`` would send a correct-looking email with no way to act on it, and
nobody would find out from the outside.

**Three parts rather than two.** A prompt has a system half and a user half; an
email has a **subject**, an **HTML body**, and a **plain-text body**. The subject
is rendered from the same context as the other two so a deployment can prefix,
suffix, or restructure it without touching Python — and all three get the *same*
context, deliberately, because a variable meaning one thing in the subject and
another in the body is a bug nobody could see from either file alone.
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

#: Where the templates live: ``apps/api/emails``.
#:
#: Resolved from this file rather than from the working directory, because the
#: API runs from ``apps/api``, the tests from the repo root, and a worker thread
#: from wherever the process was started — the same reason
#: :data:`~services.prompts.DEFAULT_PROMPT_ROOT` is resolved from ``__file__``.
DEFAULT_EMAIL_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "emails"

#: The three parts every email in this platform has.
SUBJECT_PART = "subject"
HTML_PART = "html"
TEXT_PART = "text"

#: Parts that are rendered as markup, and therefore autoescaped.
#:
#: A set rather than a boolean on the renderer, because the distinction is
#: **per part** rather than per template: the same context reaches an escaped
#: document and an unescaped one, and getting that backwards in either direction
#: is a defect (entities in the plain text, or unescaped input in the HTML).
ESCAPED_PARTS: frozenset[str] = frozenset({HTML_PART})

#: Filename suffix. Jinja's own convention.
TEMPLATE_SUFFIX = ".j2"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class EmailTemplateError(Exception):
    """An email could not be rendered.

    Deliberately **not** an :class:`~core.exceptions.AppException`, exactly as
    :class:`~services.prompts.PromptError` is not: this module is a library
    boundary, and turning its failure into an outcome is the delivery service's
    job — which records it as
    :attr:`~core.email.EmailFailureCode.TEMPLATE_FAILURE` on the row.

    Always a *deployment* fault — a missing file, a template referring to a
    variable the caller did not supply — never something a client can provoke.
    """


class EmailTemplateNotFoundError(EmailTemplateError):
    """No template exists under that name and version."""


class EmailTemplateRenderError(EmailTemplateError):
    """The template exists but could not be rendered with the given context."""


# --------------------------------------------------------------------------- #
# What comes out
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """One composed message's three parts, and which template produced them.

    The identity travels with the text rather than being read from configuration
    afterwards, for the reason :class:`~services.prompts.RenderedPrompt` carries
    its own: configuration is *current* and a delivery is *historical*, so "was
    this sent with the template we fixed on Tuesday?" is only answerable if the
    row says which one produced it.
    """

    name: str
    version: int
    subject: str
    html: str
    text: str


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class EmailTemplateRenderer(Protocol):
    """What the Email Delivery Service requires of a template store.

    Four members, and none of them mentions a notification, a user, or a mail
    server. A renderer is handed a name, a version, and a mapping of values, and
    returns three strings — which is what lets the files be replaced by a
    database-backed or remotely-managed template store without the delivery
    service noticing.
    """

    @property
    def name(self) -> str:
        """Stable identifier of the backend ("jinja-files")."""
        ...

    def is_available(self) -> bool:
        """Whether templates can actually be loaded here, right now."""
        ...

    def versions(self, name: str) -> list[int]:
        """Every version of a template, ascending. Empty when there are none."""
        ...

    def render(
        self, name: str, *, version: int | None = None, context: Mapping[str, Any]
    ) -> RenderedEmail:
        """Render all three parts of a template.

        Raises:
            EmailTemplateNotFoundError: no such template, or no such version.
            EmailTemplateRenderError: the template could not be rendered.
        """
        ...


# --------------------------------------------------------------------------- #
# Jinja2 over the filesystem
# --------------------------------------------------------------------------- #


class JinjaEmailTemplateRenderer:
    """Versioned ``.j2`` templates read from ``apps/api/emails``.

    Two environments rather than one, built lazily and behind a lock: an escaping
    one for the HTML part and a non-escaping one for the subject and the text
    part. Jinja decides autoescaping per *environment*, and the alternative —
    one environment plus ``|e`` on every interpolation in the HTML template — puts
    a security property in the hands of whoever edits the file next.

    Templates are compiled once and cached by Jinja's own loader cache, which is
    what makes rendering a per-message operation rather than a per-message disk
    read.
    """

    #: The identifier recorded for this backend.
    name = "jinja-files"

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or DEFAULT_EMAIL_TEMPLATE_ROOT
        self._environments: dict[bool, Any] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ identity #

    @property
    def root(self) -> Path:
        """Directory the templates are read from."""
        return self._root

    def is_available(self) -> bool:
        """Whether Jinja2 is installed and the template directory exists.

        Probed rather than assumed, so a deployment missing its templates reports
        ``false`` on the monitoring endpoint instead of surfacing as a failed
        delivery for every notification — the same posture a missing Tesseract
        and a missing embedding model take.
        """
        try:
            self._env(escape=True)
        except EmailTemplateError:
            return False
        return self._root.is_dir()

    # ------------------------------------------------------------ discovery #

    def versions(self, name: str) -> list[int]:
        """Every version of ``name`` present on disk, ascending.

        Derived from the filenames rather than from a manifest, so adding a
        version is adding three files and nothing else.

        A version counts only when **all three** parts are present. A ``v2`` with
        a new HTML body and last year's plain text is a half-finished edit, and
        offering it as available would let ``version=None`` select a message whose
        two halves say different things — which is exactly the failure a
        plain-text reader would be the only one to see.
        """
        directory = (self._root / name).parent
        stem = Path(name).name
        if not directory.is_dir():
            return []

        found: set[int] = set()
        for path in directory.glob(f"{stem}.v*.{SUBJECT_PART}{TEMPLATE_SUFFIX}"):
            version = _version_of(path.name, stem=stem, part=SUBJECT_PART)
            if version is None:
                continue
            if all(
                (directory / f"{stem}.v{version}.{part}{TEMPLATE_SUFFIX}").is_file()
                for part in (HTML_PART, TEXT_PART)
            ):
                found.add(version)

        return sorted(found)

    # -------------------------------------------------------------- render #

    def render(
        self, name: str, *, version: int | None = None, context: Mapping[str, Any]
    ) -> RenderedEmail:
        """Render a template's subject, HTML body, and plain-text body.

        Raises:
            EmailTemplateNotFoundError: no such template, or no such version.
            EmailTemplateRenderError: the template referred to something not
                supplied, or failed to render.
        """
        resolved = self._resolve_version(name, version)

        return RenderedEmail(
            name=name,
            version=resolved,
            # The subject is collapsed to a single line here rather than in the
            # template: a `.j2` file ends with a newline, and a `Subject` header
            # containing one is a header-injection attempt as far as every mail
            # library is concerned.
            subject=" ".join(
                self._render_part(name, resolved, SUBJECT_PART, context).split()
            ),
            html=self._render_part(name, resolved, HTML_PART, context),
            text=self._render_part(name, resolved, TEXT_PART, context),
        )

    # ------------------------------------------------------------- helpers #

    def _resolve_version(self, name: str, version: int | None) -> int:
        """Pin a version, or take the highest available.

        Raises:
            EmailTemplateNotFoundError: the template has no versions at all, or
                not the one asked for.
        """
        available = self.versions(name)
        if not available:
            logger.error("email_template_missing", template=name, root=str(self._root))
            raise EmailTemplateNotFoundError(f"No email template named {name!r}.")

        if version is None:
            return available[-1]

        if version not in available:
            logger.error(
                "email_template_version_missing",
                template=name,
                requested=version,
                available=available,
            )
            raise EmailTemplateNotFoundError(
                f"Email template {name!r} has no version {version}."
            )

        return version

    def _render_part(
        self, name: str, version: int, part: str, context: Mapping[str, Any]
    ) -> str:
        """Render one file, translating every Jinja failure at this boundary.

        The library's own exception text quotes the template **and the values it
        was substituting** — which here include a case number and a recipient's
        name. It is therefore never carried into the raised error and never
        logged; the template's identity is logged instead, which is what an
        operator needs to find the file.
        """
        from jinja2 import TemplateError as JinjaTemplateError
        from jinja2 import TemplateNotFound

        relative = f"{name}.v{version}.{part}{TEMPLATE_SUFFIX}"

        try:
            template = self._env(escape=part in ESCAPED_PARTS).get_template(
                relative.replace("\\", "/")
            )
            return str(template.render(**context)).strip()
        except TemplateNotFound as exc:
            logger.error("email_template_missing", template=relative, root=str(self._root))
            raise EmailTemplateNotFoundError(
                f"Email template {relative!r} was not found."
            ) from exc
        except JinjaTemplateError as exc:
            logger.error(
                "email_template_render_failed",
                template=relative,
                error_type=type(exc).__name__,
            )
            raise EmailTemplateRenderError(
                f"Email template {relative!r} could not be rendered."
            ) from exc

    def _env(self, *, escape: bool) -> Any:
        """Build (and cache) one of the two Jinja environments.

        Raises:
            EmailTemplateError: Jinja2 is not installed.
        """
        cached = self._environments.get(escape)
        if cached is not None:
            return cached

        with self._lock:
            # Re-checked inside the lock, exactly as the prompt library's is: two
            # threads can both pass the check above.
            existing = self._environments.get(escape)
            if existing is not None:
                return existing

            try:
                from jinja2 import Environment, FileSystemLoader, StrictUndefined
            except ImportError as exc:  # pragma: no cover - declared dependency
                logger.error("email_templates_unavailable", reason="library_missing")
                raise EmailTemplateError(
                    "The email template library is not installed."
                ) from exc

            built = Environment(
                loader=FileSystemLoader(str(self._root), encoding="utf-8"),
                # The whole reason there are two environments. See the module
                # docstring: markup for a mail client, not text for a model.
                autoescape=escape,
                # A mistyped variable must fail loudly, not send a correct-looking
                # email with a missing link.
                undefined=StrictUndefined,
                trim_blocks=True,
                lstrip_blocks=True,
                keep_trailing_newline=False,
            )
            self._environments[escape] = built
            return built


def _version_of(filename: str, *, stem: str, part: str) -> int | None:
    """Pull the version out of ``notification.v1.subject.j2``, or ``None``."""
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

#: Every renderer this build can be configured to use.
EMAIL_TEMPLATE_FACTORIES: Mapping[str, type[JinjaEmailTemplateRenderer]] = MappingProxyType(
    {JinjaEmailTemplateRenderer.name: JinjaEmailTemplateRenderer}
)

#: The one renderer the process shares, built on first use.
#:
#: Shared rather than per message because it owns Jinja's compiled-template
#: cache, and a per-message environment would recompile every template for every
#: notification — the same reasoning that makes the prompt library process-wide.
_shared: dict[str, EmailTemplateRenderer] = {}
_shared_lock = threading.Lock()


def available_email_template_renderers() -> list[str]:
    """Every renderer identifier this build can be configured to use."""
    return sorted(EMAIL_TEMPLATE_FACTORIES)


def get_email_template_renderer(identifier: str | None = None) -> EmailTemplateRenderer:
    """Return the configured email template renderer, shared across the process.

    Falls back to the default backend for an unrecognised identifier rather than
    failing startup, with the fallback logged — the same posture every other
    ``get_*`` resolver on this platform takes.
    """
    wanted = (identifier or settings.EMAIL_TEMPLATE_RENDERER).strip().lower()

    factory = EMAIL_TEMPLATE_FACTORIES.get(wanted)
    if factory is None:
        logger.warning(
            "email_template_renderer_unknown",
            requested=wanted,
            fallback=JinjaEmailTemplateRenderer.name,
        )
        wanted = JinjaEmailTemplateRenderer.name
        factory = JinjaEmailTemplateRenderer

    cached = _shared.get(wanted)
    if cached is not None:
        return cached

    with _shared_lock:
        existing = _shared.get(wanted)
        if existing is not None:
            return existing
        built: EmailTemplateRenderer = factory()
        _shared[wanted] = built
        return built


def reset_email_template_cache() -> None:
    """Discard the shared renderer. For tests, and for a runtime template edit."""
    with _shared_lock:
        _shared.clear()


__all__ = [
    "DEFAULT_EMAIL_TEMPLATE_ROOT",
    "EMAIL_TEMPLATE_FACTORIES",
    "ESCAPED_PARTS",
    "HTML_PART",
    "SUBJECT_PART",
    "TEMPLATE_SUFFIX",
    "TEXT_PART",
    "EmailTemplateError",
    "EmailTemplateNotFoundError",
    "EmailTemplateRenderError",
    "EmailTemplateRenderer",
    "JinjaEmailTemplateRenderer",
    "RenderedEmail",
    "available_email_template_renderers",
    "get_email_template_renderer",
    "reset_email_template_cache",
]
