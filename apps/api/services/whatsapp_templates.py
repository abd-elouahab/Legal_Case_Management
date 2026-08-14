"""The WhatsApp template boundary.

``18-whatsapp-delivery-channel.md``: *"Use WhatsApp message templates. Templates
should remain independent from application logic. Support variables such as user
name, case title, hearing date, report name. The implementation should prepare
for localization."*

**A WhatsApp template is not an email template, and understanding the difference
is the whole design of this module.** An email template is a document this
repository owns end to end: it holds the layout *and* the words, and rendering
produces the bytes that are sent. A WhatsApp template is a document held **by the
provider** — submitted to Meta, reviewed, approved, and versioned in a console
this repository cannot read — and what the platform sends is its *name*, its
*language*, and an ordered list of **parameters** substituted into its
``{{1}}``, ``{{2}}``, ``{{3}}`` slots.

That leaves two ways to build this feature, and only one of them is honest:

* put the sentences in the approved template ("Your hearing on case {{1}} has
  been rescheduled") and send a case number. It reads better in a console, and it
  puts the platform's wording somewhere no test can assert on, no reviewer can
  diff, and no one can keep in step with ``core/notifications.py``. The first time
  the in-app wording is improved, WhatsApp keeps saying the old thing, and nobody
  finds out;
* keep the sentences in :mod:`core.notifications`, where every other channel gets
  them, and make the approved template a **thin envelope** around them — a
  greeting slot, a heading slot, a body slot. The wording is reviewable, the three
  channels cannot drift, and an Arabic recipient's message is Arabic for the same
  reason their feed is.

This module implements the second. What lives in ``apps/api/whatsapp/`` is
therefore a **descriptor** rather than a message: a versioned ``.j2`` file that
says, in order, which values fill the approved template's slots. It is the
contract between this repository and a template registered in the WhatsApp
Business account, and it is reviewable as a diff — which is what the spec's
*"templates should remain independent from application logic"* is worth in
practice.

**One part, not three.** The email renderer produces a subject, an HTML body, and
a plain-text body; a WhatsApp template message has none of those. It has an
ordered parameter list, so a descriptor is one file — ``<name>.v<version>.params.j2``
— rendering **one parameter per line**. Blank lines are dropped, so a descriptor
can use Jinja control flow without its whitespace becoming an empty parameter.

**Rendering is strict**, exactly as :mod:`services.prompts` and
:mod:`services.email_templates` are: ``StrictUndefined``, because a descriptor
that silently lost a variable would send a parameter list of the wrong length —
and the Cloud API answers that with ``132000``, one message at a time, on a
channel where a missed hearing update matters.

**Autoescaping is off**, and here that is the *safe* setting rather than the
dangerous one — the inverse of the email renderer's HTML part. A parameter is
delivered as text into a slot in an approved template; it is never markup, and it
is never concatenated into one. Escaping would put ``&#39;`` into a French case
description and ``&amp;`` into a firm's name on somebody's phone. The injection
risk a parameter actually carries — newlines and runs of spaces, which the Cloud
API refuses because they would let a sender restructure an approved layout — is
handled by :func:`~core.whatsapp.sanitize_parameter`, which is applied to every
parameter here *and* again at the provider boundary.
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
from core.whatsapp import sanitize_parameter

logger = structlog.get_logger(__name__)

#: Where the descriptors live: ``apps/api/whatsapp``.
#:
#: Resolved from this file rather than from the working directory, because the API
#: runs from ``apps/api``, the tests from the repo root, and a worker thread from
#: wherever the process was started — the same reason
#: :data:`~services.email_templates.DEFAULT_EMAIL_TEMPLATE_ROOT` is resolved from
#: ``__file__``.
DEFAULT_WHATSAPP_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "whatsapp"

#: The one part a WhatsApp descriptor has. See the module docstring.
PARAMS_PART = "params"

#: Filename suffix. Jinja's own convention.
TEMPLATE_SUFFIX = ".j2"

#: Most parameters one message may carry.
#:
#: The Cloud API's own ceiling is far higher, and this is deliberately not it: a
#: template with more than a handful of slots is one nobody can read on a phone,
#: and a descriptor that produced fifty parameters would be a runaway loop rather
#: than a design. A bound only ever reached by a bug is the bound worth having.
MAX_PARAMETERS = 12


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class WhatsAppTemplateError(Exception):
    """A WhatsApp message could not be rendered.

    Deliberately **not** an :class:`~core.exceptions.AppException`, exactly as
    :class:`~services.email_templates.EmailTemplateError` is not: this module is a
    library boundary, and turning its failure into an outcome is the delivery
    service's job — which records it as
    :attr:`~core.whatsapp.WhatsAppFailureCode.TEMPLATE_FAILURE` on the row.

    Always a *deployment* fault — a missing descriptor, a descriptor referring to
    a variable the caller did not supply — never something a client can provoke.
    Note the deliberate distinction from
    :attr:`~core.whatsapp.WhatsAppFailureCode.TEMPLATE_REJECTED`, which is the
    *provider* refusing an approved template: this one is fixed in the repository,
    that one in the WhatsApp Business account.
    """


class WhatsAppTemplateNotFoundError(WhatsAppTemplateError):
    """No descriptor exists under that name and version."""


class WhatsAppTemplateRenderError(WhatsAppTemplateError):
    """The descriptor exists but could not be rendered with the given context."""


# --------------------------------------------------------------------------- #
# What comes out
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RenderedWhatsAppTemplate:
    """One message's parameters, and which descriptor produced them.

    The identity travels with the values rather than being read from configuration
    afterwards, for the reason :class:`~services.email_templates.RenderedEmail`
    carries its own: configuration is *current* and a delivery is *historical*, so
    "was this sent through the descriptor we fixed on Tuesday?" is only answerable
    if the row says which one produced it.
    """

    name: str
    version: int
    #: The ordered body parameters, already substituted and already screened.
    parameters: tuple[str, ...]


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class WhatsAppTemplateRenderer(Protocol):
    """What the WhatsApp Delivery Service requires of a descriptor store.

    Four members, and none of them mentions a notification, a user, or a provider.
    A renderer is handed a name, a version, and a mapping of values, and returns an
    ordered tuple of strings — which is what lets the files be replaced by a
    database-backed or remotely-managed store without the delivery service
    noticing.
    """

    @property
    def name(self) -> str:
        """Stable identifier of the backend ("jinja-files")."""
        ...

    def is_available(self) -> bool:
        """Whether descriptors can actually be loaded here, right now."""
        ...

    def versions(self, name: str) -> list[int]:
        """Every version of a descriptor, ascending. Empty when there are none."""
        ...

    def render(
        self, name: str, *, version: int | None = None, context: Mapping[str, Any]
    ) -> RenderedWhatsAppTemplate:
        """Render one descriptor into an ordered parameter list.

        Raises:
            WhatsAppTemplateNotFoundError: no such descriptor, or no such version.
            WhatsAppTemplateRenderError: the descriptor could not be rendered, or
                produced no parameters at all.
        """
        ...


# --------------------------------------------------------------------------- #
# Jinja2 over the filesystem
# --------------------------------------------------------------------------- #


class JinjaWhatsAppTemplateRenderer:
    """Versioned ``.j2`` descriptors read from ``apps/api/whatsapp``.

    One environment rather than the email renderer's two, because there is no
    markup part to escape — see the module docstring for why autoescaping being
    *off* is the safe setting for a template parameter.

    Descriptors are compiled once and cached by Jinja's own loader cache, which is
    what makes rendering a per-message operation rather than a per-message disk
    read.
    """

    #: The identifier recorded for this backend.
    name = "jinja-files"

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or DEFAULT_WHATSAPP_TEMPLATE_ROOT
        self._environment: Any | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ identity #

    @property
    def root(self) -> Path:
        """Directory the descriptors are read from."""
        return self._root

    def is_available(self) -> bool:
        """Whether Jinja2 is installed and the descriptor directory exists.

        Probed rather than assumed, so a deployment missing its descriptors
        reports ``false`` on the monitoring endpoint instead of surfacing as a
        failed delivery for every notification — the same posture a missing
        Tesseract and a missing embedding model take.
        """
        try:
            self._env()
        except WhatsAppTemplateError:
            return False
        return self._root.is_dir()

    # ------------------------------------------------------------ discovery #

    def versions(self, name: str) -> list[int]:
        """Every version of ``name`` present on disk, ascending.

        Derived from the filenames rather than from a manifest, so adding a
        version is adding one file and nothing else.
        """
        directory = (self._root / name).parent
        stem = Path(name).name
        if not directory.is_dir():
            return []

        found: set[int] = set()
        for path in directory.glob(f"{stem}.v*.{PARAMS_PART}{TEMPLATE_SUFFIX}"):
            version = _version_of(path.name, stem=stem, part=PARAMS_PART)
            if version is not None:
                found.add(version)

        return sorted(found)

    # -------------------------------------------------------------- render #

    def render(
        self, name: str, *, version: int | None = None, context: Mapping[str, Any]
    ) -> RenderedWhatsAppTemplate:
        """Render a descriptor into the ordered parameters one message carries.

        Each non-blank line of the rendered descriptor becomes **one parameter**,
        in order, screened by :func:`~core.whatsapp.sanitize_parameter`. Blank
        lines are dropped so a descriptor can use ``{% if %}`` without its
        whitespace becoming an empty slot — and an *empty* parameter is worth
        avoiding specifically, because the Cloud API accepts it and the reader sees
        a sentence with a hole in it.

        Raises:
            WhatsAppTemplateNotFoundError: no such descriptor, or no such version.
            WhatsAppTemplateRenderError: the descriptor referred to something not
                supplied, failed to render, produced no parameters, or produced
                more than :data:`MAX_PARAMETERS`.
        """
        resolved = self._resolve_version(name, version)
        rendered = self._render_part(name, resolved, PARAMS_PART, context)

        parameters = tuple(
            screened
            for line in rendered.splitlines()
            if (screened := sanitize_parameter(line))
        )

        if not parameters:
            # A descriptor that rendered to nothing would send a template message
            # with an empty body component, which the Cloud API answers with
            # `132000` — a per-message failure for what is actually a deployment
            # fault. Caught here so it is recorded as one.
            logger.error("whatsapp_template_empty", template=name, version=resolved)
            raise WhatsAppTemplateRenderError(
                f"WhatsApp descriptor {name!r} v{resolved} produced no parameters."
            )
        if len(parameters) > MAX_PARAMETERS:
            logger.error(
                "whatsapp_template_too_many_parameters",
                template=name,
                version=resolved,
                count=len(parameters),
                maximum=MAX_PARAMETERS,
            )
            raise WhatsAppTemplateRenderError(
                f"WhatsApp descriptor {name!r} v{resolved} produced "
                f"{len(parameters)} parameters."
            )

        return RenderedWhatsAppTemplate(
            name=name, version=resolved, parameters=parameters
        )

    # ------------------------------------------------------------- helpers #

    def _resolve_version(self, name: str, version: int | None) -> int:
        """Pin a version, or take the highest available.

        Raises:
            WhatsAppTemplateNotFoundError: the descriptor has no versions at all,
                or not the one asked for.
        """
        available = self.versions(name)
        if not available:
            logger.error(
                "whatsapp_template_missing", template=name, root=str(self._root)
            )
            raise WhatsAppTemplateNotFoundError(
                f"No WhatsApp descriptor named {name!r}."
            )

        if version is None:
            return available[-1]

        if version not in available:
            logger.error(
                "whatsapp_template_version_missing",
                template=name,
                requested=version,
                available=available,
            )
            raise WhatsAppTemplateNotFoundError(
                f"WhatsApp descriptor {name!r} has no version {version}."
            )

        return version

    def _render_part(
        self, name: str, version: int, part: str, context: Mapping[str, Any]
    ) -> str:
        """Render one file, translating every Jinja failure at this boundary.

        The library's own exception text quotes the template **and the values it
        was substituting** — which here include a case number and a recipient's
        name. It is therefore never carried into the raised error and never logged;
        the descriptor's identity is logged instead, which is what an operator
        needs to find the file.
        """
        from jinja2 import TemplateError as JinjaTemplateError
        from jinja2 import TemplateNotFound

        relative = f"{name}.v{version}.{part}{TEMPLATE_SUFFIX}"

        try:
            template = self._env().get_template(relative.replace("\\", "/"))
            return str(template.render(**context))
        except TemplateNotFound as exc:
            logger.error(
                "whatsapp_template_missing", template=relative, root=str(self._root)
            )
            raise WhatsAppTemplateNotFoundError(
                f"WhatsApp descriptor {relative!r} was not found."
            ) from exc
        except JinjaTemplateError as exc:
            logger.error(
                "whatsapp_template_render_failed",
                template=relative,
                error_type=type(exc).__name__,
            )
            raise WhatsAppTemplateRenderError(
                f"WhatsApp descriptor {relative!r} could not be rendered."
            ) from exc

    def _env(self) -> Any:
        """Build (and cache) the Jinja environment.

        Raises:
            WhatsAppTemplateError: Jinja2 is not installed.
        """
        cached = self._environment
        if cached is not None:
            return cached

        with self._lock:
            # Re-checked inside the lock, exactly as the prompt library's and the
            # email renderer's are: two threads can both pass the check above.
            if self._environment is not None:
                return self._environment

            try:
                from jinja2 import Environment, FileSystemLoader, StrictUndefined
            except ImportError as exc:  # pragma: no cover - declared dependency
                logger.error("whatsapp_templates_unavailable", reason="library_missing")
                raise WhatsAppTemplateError(
                    "The template library is not installed."
                ) from exc

            built = Environment(
                loader=FileSystemLoader(str(self._root), encoding="utf-8"),
                # Off, and that is the safe setting here. See the module
                # docstring: a parameter is text into an approved slot, never
                # markup, and escaping would put entities on somebody's phone.
                autoescape=False,
                # A mistyped variable must fail loudly, not send a parameter list
                # of the wrong length that the provider refuses one message at a
                # time.
                undefined=StrictUndefined,
                trim_blocks=True,
                lstrip_blocks=True,
                keep_trailing_newline=False,
            )
            self._environment = built
            return built


def _version_of(filename: str, *, stem: str, part: str) -> int | None:
    """Pull the version out of ``notification.v1.params.j2``, or ``None``."""
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
WHATSAPP_TEMPLATE_FACTORIES: Mapping[str, type[JinjaWhatsAppTemplateRenderer]] = (
    MappingProxyType({JinjaWhatsAppTemplateRenderer.name: JinjaWhatsAppTemplateRenderer})
)

#: The one renderer the process shares, built on first use.
#:
#: Shared rather than per message because it owns Jinja's compiled-template cache,
#: and a per-message environment would recompile every descriptor for every
#: notification — the same reasoning that makes the prompt library and the email
#: renderer process-wide.
_shared: dict[str, WhatsAppTemplateRenderer] = {}
_shared_lock = threading.Lock()


def available_whatsapp_template_renderers() -> list[str]:
    """Every renderer identifier this build can be configured to use."""
    return sorted(WHATSAPP_TEMPLATE_FACTORIES)


def get_whatsapp_template_renderer(
    identifier: str | None = None,
) -> WhatsAppTemplateRenderer:
    """Return the configured renderer, shared across the process.

    Falls back to the default backend for an unrecognised identifier rather than
    failing startup, with the fallback logged — the same posture every other
    ``get_*`` resolver on this platform takes.
    """
    wanted = (identifier or settings.WHATSAPP_TEMPLATE_RENDERER).strip().lower()

    factory = WHATSAPP_TEMPLATE_FACTORIES.get(wanted)
    if factory is None:
        logger.warning(
            "whatsapp_template_renderer_unknown",
            requested=wanted,
            fallback=JinjaWhatsAppTemplateRenderer.name,
        )
        wanted = JinjaWhatsAppTemplateRenderer.name
        factory = JinjaWhatsAppTemplateRenderer

    cached = _shared.get(wanted)
    if cached is not None:
        return cached

    with _shared_lock:
        existing = _shared.get(wanted)
        if existing is not None:
            return existing
        built: WhatsAppTemplateRenderer = factory()
        _shared[wanted] = built
        return built


def reset_whatsapp_template_cache() -> None:
    """Discard the shared renderer. For tests, and for a runtime descriptor edit."""
    with _shared_lock:
        _shared.clear()


__all__ = [
    "DEFAULT_WHATSAPP_TEMPLATE_ROOT",
    "MAX_PARAMETERS",
    "PARAMS_PART",
    "TEMPLATE_SUFFIX",
    "WHATSAPP_TEMPLATE_FACTORIES",
    "JinjaWhatsAppTemplateRenderer",
    "RenderedWhatsAppTemplate",
    "WhatsAppTemplateError",
    "WhatsAppTemplateNotFoundError",
    "WhatsAppTemplateRenderError",
    "WhatsAppTemplateRenderer",
    "available_whatsapp_template_renderers",
    "get_whatsapp_template_renderer",
    "reset_whatsapp_template_cache",
]
