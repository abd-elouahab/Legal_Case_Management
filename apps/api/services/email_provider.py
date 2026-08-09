"""The email provider boundary.

``17-email-delivery-channel.md``: *"The application must never depend directly on
a specific provider. Implement the first provider using SMTP. The design should
allow future providers such as Resend, SendGrid, Amazon SES, Mailgun without
changing application logic."*

This module is that boundary, and it is the **only** place in the platform that
imports ``smtplib`` or ``email``. The shape mirrors every other seam in this
codebase (:mod:`services.ocr_engine`, :mod:`services.chunking`,
:mod:`services.embedding`, :mod:`services.vector_store`, :mod:`services.prompts`,
:mod:`services.llm`):

* :class:`EmailProvider` is the protocol :mod:`services.email_delivery` depends
  on. It has three members, and none of them mentions a notification, a user, a
  case, or a template — a provider is handed an addressed message and returns
  whether it was accepted;
* :class:`SmtpEmailProvider` is the implementation that ships;
* :class:`NullEmailProvider` accepts and discards, which is what a test wants and
  what a deployment with no relay configured falls back to;
* :func:`get_email_provider` resolves one, so Resend, SendGrid, SES, or Mailgun
  is **one class plus one registry entry** — no service, repository, template,
  worker, or endpoint changes.

**Every library failure is translated here**, into an
:class:`~core.email.EmailFailureCode`. That is what lets the delivery service
record a *cause*, decide whether to retry, and group failures in a monitoring
view without knowing what an ``SMTPResponseException`` is — the same contract
:mod:`services.ocr_engine` keeps for Tesseract and :mod:`services.llm` keeps for
a model SDK.

**The provider's own message never escapes this module.** An SMTP rejection
echoes the envelope — ``550 5.1.1 <someone@firm.example>: user unknown`` — so the
text is neither raised, nor stored, nor logged: what leaves is a code, a status
class, and identifiers. That is a stricter rule than the one the OCR boundary
follows, and it is stricter for a reason: an OCR library's message can leak a
document's *text*, while an SMTP server's leaks a *person's address*, which is
precisely what the spec's "do not log confidential information" is protecting on
this channel.

**Credentials come from the environment and are never logged.** ``SMTP_PASSWORD``
is read from :mod:`core.config`, handed to the library, and appears in no log
line, no exception, and no metric — the same posture ``LLM_API_KEY`` and
``MINIO_SECRET_KEY`` take.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

import structlog

from core.config import settings
from core.email import (
    EmailFailureCode,
    InvalidEmailRecipientError,
    normalize_email,
    sanitize_header_value,
)

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# What goes in and what comes out
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    """One fully-composed message, ready for a provider.

    Frozen, and deliberately **already rendered**: a provider receives text, not a
    template name and a context. That is what keeps template rendering,
    localization, and authorization on the application side of this boundary,
    where they are testable without a mail server — and it is what makes a second
    provider a class that formats an HTTP request rather than one that has to
    learn what a notification is.

    Both bodies are always present. ``17-email-delivery-channel.md`` requires
    support for **HTML and plain text**, and the right way to satisfy that is one
    ``multipart/alternative`` message carrying both rather than a setting that
    picks one: a mail client that cannot render HTML, a screen reader, and a
    plain-text digest all read the second part, and sending HTML alone would make
    those readers see nothing.
    """

    to_address: str
    to_name: str
    subject: str
    html_body: str
    text_body: str

    from_address: str
    from_name: str
    reply_to: str | None = None

    #: Extra headers the provider should set. Bounded and screened by
    #: :func:`~core.email.sanitize_header_value` before they are applied.
    headers: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    """What one provider call did.

    A value rather than an exception on the failure path, for the reason
    :class:`~services.ocr_engine.OcrEngine` returns one: a refused message is an
    ordinary, recordable *outcome* of a background job, and forcing every caller
    into a ``try`` block around a routine result is how a failure ends up logged
    twice and recorded once.
    """

    accepted: bool
    provider: str
    #: How long the provider call took, in milliseconds. Only the call — not
    #: rendering, not the database write around it.
    duration_ms: float = 0.0
    #: Why it was refused. ``None`` on success.
    failure: EmailFailureCode | None = None

    @classmethod
    def success(cls, *, provider: str, duration_ms: float) -> EmailSendResult:
        """A message the provider accepted."""
        return cls(accepted=True, provider=provider, duration_ms=duration_ms)

    @classmethod
    def refused(
        cls, *, provider: str, failure: EmailFailureCode, duration_ms: float = 0.0
    ) -> EmailSendResult:
        """A message the provider did not accept."""
        return cls(
            accepted=False, provider=provider, failure=failure, duration_ms=duration_ms
        )


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class EmailProvider(Protocol):
    """What the Email Delivery Service requires of a provider.

    Three members. The narrowness is the point: a delivery service cannot
    accidentally depend on SMTP semantics, and a replacement has three things to
    implement.
    """

    @property
    def name(self) -> str:
        """Stable identifier of the backend ("smtp"). Recorded on every delivery."""
        ...

    def is_available(self) -> bool:
        """Whether this provider is configured well enough to be worth calling.

        A **configuration** check rather than a health check: it must not open a
        connection, because it is consulted before every dispatch and a network
        round trip per notification would be a cost the feature cannot justify.
        An unreachable-but-configured relay is discovered by :meth:`send`, which
        is where a transient failure belongs.
        """
        ...

    def send(self, message: OutgoingEmail) -> EmailSendResult:
        """Hand one message to the provider.

        **Never raises.** Every library failure is translated into an
        :class:`~core.email.EmailFailureCode` on the returned result — see the
        module docstring for why the provider's own message never escapes.
        """
        ...


# --------------------------------------------------------------------------- #
# SMTP
# --------------------------------------------------------------------------- #


class SmtpEmailProvider:
    """Delivery over SMTP, which is what ``architecture.md`` names.

    Uses the standard library, so this feature adds **no dependency** — the same
    outcome ``14-ai-report-agent.md`` reached for Markdown export, and a genuine
    property of choosing the protocol every relay speaks rather than one vendor's
    HTTP API.

    **A connection per message, not a pooled one.** That looks wasteful and is the
    right trade at this volume: the platform's email is a handful of messages per
    case event, an SMTP connection held open across a quiet period is dropped by
    the relay anyway, and a pool would need its own health checking, its own
    locking, and its own failure mode inside a class whose whole purpose is to be
    replaceable. ``EMAIL_WORKER_CONCURRENCY`` bounds how many exist at once. The
    spec's *"minimize provider requests"* is met where it actually costs — one
    message per notification, guaranteed by a unique constraint — rather than by
    reusing sockets.

    **Transport security is explicit rather than inferred.** ``SMTP_USE_SSL``
    opens an implicit-TLS connection (the historical port 465); ``SMTP_USE_TLS``
    upgrades a plaintext connection with ``STARTTLS`` (port 587, the modern
    default). They are separate settings because a relay supports one or the
    other and guessing from the port number is how a deployment silently sends
    credentials in the clear.
    """

    #: The identifier recorded for this backend.
    name = "smtp"

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool | None = None,
        use_ssl: bool | None = None,
        timeout: float | None = None,
    ) -> None:
        self._host = (host if host is not None else settings.SMTP_HOST) or None
        self._port = port if port is not None else settings.SMTP_PORT
        self._username = username if username is not None else settings.SMTP_USERNAME
        self._password = password if password is not None else settings.SMTP_PASSWORD
        self._use_tls = use_tls if use_tls is not None else settings.SMTP_USE_TLS
        self._use_ssl = use_ssl if use_ssl is not None else settings.SMTP_USE_SSL
        self._timeout = (
            timeout if timeout is not None else float(settings.SMTP_TIMEOUT_SECONDS)
        )

    # ------------------------------------------------------------ identity #

    def is_available(self) -> bool:
        """Whether a relay host is configured.

        The absence of one is handled rather than fatal, exactly as a missing
        Tesseract and a missing ``LLM_API_KEY`` are: the API starts, every other
        feature works, ``GET /notifications/email/metrics`` reports
        ``provider_available: false``, and no delivery row is written at all —
        which is better than a backlog of failures nobody asked for.
        """
        return bool(self._host)

    # ---------------------------------------------------------------- send #

    def send(self, message: OutgoingEmail) -> EmailSendResult:
        """Compose and hand one message to the relay. Never raises."""
        if not self.is_available():
            return EmailSendResult.refused(
                provider=self.name, failure=EmailFailureCode.PROVIDER_UNAVAILABLE
            )

        try:
            composed = self._compose(message)
        except InvalidEmailRecipientError:
            # A header value carrying a line break. Refused rather than sent with
            # the offending part stripped — see `core.email.sanitize_header_value`.
            logger.warning("email_header_rejected", provider=self.name)
            return EmailSendResult.refused(
                provider=self.name, failure=EmailFailureCode.INVALID_RECIPIENT
            )
        except Exception:
            logger.exception("email_compose_failed", provider=self.name)
            return EmailSendResult.refused(
                provider=self.name, failure=EmailFailureCode.MESSAGE_REFUSED
            )

        started = time.perf_counter()
        try:
            self._deliver(composed, sender=message.from_address, recipient=message.to_address)
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000.0
            failure = self._classify(exc)
            # The exception **type** and the code, never `str(exc)`: an SMTP
            # rejection quotes the envelope. See the module docstring.
            logger.warning(
                "email_send_failed",
                provider=self.name,
                error_code=failure.value,
                error_type=type(exc).__name__,
                duration_ms=round(elapsed, 2),
            )
            return EmailSendResult.refused(
                provider=self.name, failure=failure, duration_ms=elapsed
            )

        return EmailSendResult.success(
            provider=self.name, duration_ms=(time.perf_counter() - started) * 1000.0
        )

    # ------------------------------------------------------------- helpers #

    def _compose(self, message: OutgoingEmail) -> Any:
        """Build a ``multipart/alternative`` message carrying both bodies.

        The plain-text part is added **first** and the HTML second, which is not
        cosmetic: ``multipart/alternative`` is ordered least-preferred to
        most-preferred, so a client that can render HTML picks the last part and
        one that cannot falls back to the first. Reversing them makes every rich
        client show the plain text.

        Raises:
            InvalidEmailRecipientError: a header value carries a line break.
        """
        from email.message import EmailMessage

        composed = EmailMessage()
        composed["Subject"] = sanitize_header_value(message.subject)
        composed["From"] = self._address(message.from_name, message.from_address)
        composed["To"] = self._address(message.to_name, message.to_address)
        if message.reply_to:
            composed["Reply-To"] = sanitize_header_value(message.reply_to, limit=320)

        # `Auto-Submitted` tells other mail systems this was generated rather than
        # typed, which is what stops an out-of-office reply bouncing back into a
        # relay for every notification the platform sends (RFC 3834).
        composed["Auto-Submitted"] = "auto-generated"

        for key, value in (message.headers or {}).items():
            composed[sanitize_header_value(key, limit=64)] = sanitize_header_value(
                value, limit=320
            )

        composed.set_content(message.text_body, subtype="plain", charset="utf-8")
        composed.add_alternative(message.html_body, subtype="html", charset="utf-8")
        return composed

    @staticmethod
    def _address(display_name: str, address: str) -> str:
        """Render one ``Name <address>`` header value.

        Both halves pass through :func:`~core.email.sanitize_header_value` first.
        The address is additionally re-validated, because this is the last point
        before it reaches a header and a malformed one is a
        :attr:`~core.email.EmailFailureCode.INVALID_RECIPIENT` rather than
        something to hand to a relay.

        Raises:
            InvalidEmailRecipientError: either half is unusable.
        """
        from email.headerregistry import Address

        normalized = normalize_email(address)
        if normalized is None:
            raise InvalidEmailRecipientError("Not a usable email address.")

        name = sanitize_header_value(display_name, limit=120)
        local, _, domain = normalized.partition("@")
        # `Address` does the RFC 2047 encoding of a non-ASCII display name, which
        # is what makes an Arabic or French name render correctly in a client
        # rather than as mojibake.
        return str(Address(display_name=name, username=local, domain=domain))

    def _deliver(self, composed: Any, *, sender: str, recipient: str) -> None:
        """Open a connection, authenticate if configured, and send. Raises on failure."""
        import smtplib

        # Narrowed rather than asserted: `send` checks `is_available()` first, so
        # this is unreachable with no host — and an empty string reaching smtplib
        # is a connection error, which is a `connection_failed` on the row rather
        # than an exception the type checker was talked out of.
        host = self._host or ""

        if self._use_ssl:
            client: Any = smtplib.SMTP_SSL(host, self._port, timeout=self._timeout)
        else:
            client = smtplib.SMTP(host, self._port, timeout=self._timeout)

        try:
            client.ehlo()
            if self._use_tls and not self._use_ssl:
                client.starttls()
                # Re-introduced after the upgrade: the server's capability list
                # is renegotiated over the encrypted channel, and AUTH is
                # commonly only advertised there.
                client.ehlo()
            if self._username and self._password:
                client.login(self._username, self._password)
            client.send_message(composed, from_addr=sender, to_addrs=[recipient])
        finally:
            try:
                client.quit()
            except Exception:  # pragma: no cover - the connection is already going away
                client.close()

    @staticmethod
    def _classify(exc: Exception) -> EmailFailureCode:
        """Translate one library exception into a platform failure code.

        **The whole of the transient/permanent decision for SMTP**, and it turns
        on the response class: a ``4xx`` reply means "try again later" (greylist,
        rate limit, temporary local error) and a ``5xx`` means "this will never
        work". Retrying a ``5xx`` is how a platform gets a relay to stop
        accepting its mail altogether.
        """
        import smtplib
        import socket
        import ssl

        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return EmailFailureCode.AUTHENTICATION_FAILED
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            return EmailFailureCode.RECIPIENT_REFUSED
        if isinstance(exc, smtplib.SMTPSenderRefused):
            # The platform's own `EMAIL_FROM_ADDRESS` was refused. Permanent and a
            # configuration fault: every message will be refused identically.
            return EmailFailureCode.MESSAGE_REFUSED
        # **Before** the generic response check below, and the order is
        # load-bearing rather than stylistic: `SMTPConnectError` is a *subclass*
        # of `SMTPResponseException`, so testing the general case first would
        # classify a failed connection by its response code and report a relay
        # that could not be reached as a throttled one. Both are transient, so
        # nothing behaves differently — but the code lands in a monitoring
        # breakdown, and "the relay is rate-limiting us" and "the relay is
        # unreachable" are different operational problems.
        if isinstance(exc, smtplib.SMTPServerDisconnected | smtplib.SMTPConnectError):
            return EmailFailureCode.CONNECTION_FAILED
        if isinstance(exc, smtplib.SMTPResponseException):
            return (
                EmailFailureCode.THROTTLED
                if 400 <= int(exc.smtp_code) < 500
                else EmailFailureCode.MESSAGE_REFUSED
            )
        if isinstance(exc, TimeoutError | socket.timeout):
            return EmailFailureCode.TIMEOUT
        if isinstance(exc, ssl.SSLError | OSError):
            # `OSError` covers `ConnectionRefusedError` and every DNS failure,
            # and it is last because `TimeoutError` is a subclass of it.
            return EmailFailureCode.CONNECTION_FAILED
        return EmailFailureCode.UNKNOWN


# --------------------------------------------------------------------------- #
# The no-op
# --------------------------------------------------------------------------- #


class NullEmailProvider:
    """A provider that accepts every message and sends nothing.

    Two legitimate uses, and neither is a shortcut:

    * an **integration test**, which asserts that a delivery reached ``sent``
      without a mail server in the loop;
    * a **deployment that wants the delivery pipeline switched on without a
      relay** — a staging environment, a demonstration — where writing delivery
      rows and exercising the templates is the point and actually mailing anybody
      is not.

    It reports itself as available, deliberately: a provider that claimed to be
    unavailable would make the delivery service skip dispatch entirely, and a
    test asserting on delivery rows would then be asserting on nothing.
    """

    name = "null"

    def __init__(self) -> None:
        self.sent: list[OutgoingEmail] = []

    def is_available(self) -> bool:
        """Always. See the class docstring."""
        return True

    def send(self, message: OutgoingEmail) -> EmailSendResult:
        """Record the message and report success.

        Logged at **debug** and by recipient *domain* rather than address: even a
        discarding provider must not be the one place a mailbox ends up in the
        application log.
        """
        self.sent.append(message)
        logger.debug(
            "email_discarded",
            provider=self.name,
            domain=message.to_address.rpartition("@")[2],
        )
        return EmailSendResult.success(provider=self.name, duration_ms=0.0)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

#: Every provider this build can be configured to use.
#:
#: Adding one is a class implementing :class:`EmailProvider` plus an entry here —
#: the same shape as :data:`~services.embedding.EMBEDDER_FACTORIES` and
#: :data:`~services.prompts.PROMPT_LIBRARY_FACTORIES`. **That is the whole of
#: what "the design should allow future providers without changing application
#: logic" costs**: Resend, SendGrid, SES, and Mailgun each format an HTTP request
#: from an :class:`OutgoingEmail` and map their status codes onto
#: :class:`~core.email.EmailFailureCode`, and nothing above this line moves.
EMAIL_PROVIDER_FACTORIES: Mapping[str, type[Any]] = MappingProxyType(
    {SmtpEmailProvider.name: SmtpEmailProvider, NullEmailProvider.name: NullEmailProvider}
)

#: The one provider the process shares, built on first use.
#:
#: Shared rather than per request because a future HTTP-based provider will own a
#: connection pool, exactly as :func:`~services.llm.get_llm_provider` does — and
#: because the delivery worker has no request to build one from.
_shared: dict[str, EmailProvider] = {}
_shared_lock = threading.Lock()


def available_email_providers() -> list[str]:
    """Every provider identifier this build can be configured to use."""
    return sorted(EMAIL_PROVIDER_FACTORIES)


def get_email_provider(identifier: str | None = None) -> EmailProvider:
    """Return the configured email provider, shared across the process.

    Falls back to the default backend for an unrecognised identifier rather than
    failing startup, with the fallback logged so the misconfiguration is visible
    — the same posture :func:`~services.prompts.get_prompt_library` and
    :func:`~services.llm.get_llm_provider` take, and for the same reason: an API
    that refuses to come up over a mail setting would take authentication, cases,
    and documents down with it.
    """
    wanted = (identifier or settings.EMAIL_PROVIDER).strip().lower()

    factory = EMAIL_PROVIDER_FACTORIES.get(wanted)
    if factory is None:
        logger.warning(
            "email_provider_unknown", requested=wanted, fallback=SmtpEmailProvider.name
        )
        wanted = SmtpEmailProvider.name
        factory = SmtpEmailProvider

    cached = _shared.get(wanted)
    if cached is not None:
        return cached

    with _shared_lock:
        existing = _shared.get(wanted)
        if existing is not None:
            return existing
        built: EmailProvider = factory()
        _shared[wanted] = built
        return built


def reset_email_provider_cache() -> None:
    """Discard the shared provider. For tests, and after a configuration change."""
    with _shared_lock:
        _shared.clear()


__all__ = [
    "EMAIL_PROVIDER_FACTORIES",
    "EmailProvider",
    "EmailSendResult",
    "NullEmailProvider",
    "OutgoingEmail",
    "SmtpEmailProvider",
    "available_email_providers",
    "get_email_provider",
    "reset_email_provider_cache",
]
