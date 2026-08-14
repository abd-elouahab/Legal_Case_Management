"""The WhatsApp provider boundary.

``18-whatsapp-delivery-channel.md``: *"The application must never depend directly
on the Meta SDK or HTTP endpoints. Implement the first provider using Meta
WhatsApp Cloud API. The implementation should allow future providers such as
Twilio, Vonage, future providers — without changing application logic."*

This module is that boundary, and it is the **only** place in the platform that
knows Meta's URL shape, its JSON, or its error codes. The shape mirrors every
other seam in this codebase (:mod:`services.ocr_engine`, :mod:`services.chunking`,
:mod:`services.embedding`, :mod:`services.vector_store`, :mod:`services.prompts`,
:mod:`services.llm`, :mod:`services.email_provider`):

* :class:`WhatsAppProvider` is the protocol :mod:`services.whatsapp_delivery`
  depends on. It has four members, and none of them mentions a notification, a
  user, a case, or a rule — a provider is handed an addressed message and returns
  whether it was accepted;
* :class:`MetaWhatsAppProvider` is the implementation that ships;
* :class:`NullWhatsAppProvider` accepts and discards, which is what a test wants
  and what a staging deployment with no business account falls back to;
* :func:`get_whatsapp_provider` resolves one, so **Twilio or Vonage is one class
  plus one registry entry** — no service, repository, template, worker, or
  endpoint changes.

**No SDK, and no HTTP dependency either.** The Cloud API is one JSON ``POST`` with
a bearer token, and :mod:`urllib.request` in the standard library sends it — the
same outcome :class:`~services.email_provider.SmtpEmailProvider` reached with
``smtplib`` and the report exporter reached for Markdown. The spec's *"must never
depend directly on the Meta SDK"* is therefore satisfied twice over: there is no
SDK to depend on, and the one module that does speak to Meta is behind a protocol.

**Every library and provider failure is translated here**, into a
:class:`~core.whatsapp.WhatsAppFailureCode`. That is what lets the delivery
service record a *cause*, decide whether to retry, and group failures in a
monitoring view without knowing what a ``URLError`` or a Meta error subcode is.

**The provider's own message never escapes this module.** A Cloud API error body
quotes the request — including the recipient's number, and frequently the template
parameters — so its text is neither raised, nor stored, nor logged: what leaves is
a code, an HTTP status, and Meta's numeric error code, all of which are stable
vocabulary rather than anybody's data. That is the same rule the SMTP boundary
follows, and it matters at least as much here: an SMTP rejection leaks an address,
and a Cloud API rejection leaks a personal phone number.

**Credentials come from the environment and are never logged.**
``WHATSAPP_ACCESS_TOKEN`` is read from :mod:`core.config`, placed in an
``Authorization`` header, and appears in no log line, no exception, and no metric
— the same posture ``SMTP_PASSWORD``, ``LLM_API_KEY``, and ``MINIO_SECRET_KEY``
take. :meth:`MetaWhatsAppProvider.configuration_errors` reports *which* settings
are missing by **name**, never by value, which is how the spec's *"fail
gracefully when configuration is missing, provide meaningful error messages"* is
met without the meaningful message being a credential.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

import structlog

from core.config import settings
from core.whatsapp import (
    WhatsAppFailureCode,
    sanitize_parameter,
)

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# What goes in and what comes out
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OutgoingWhatsAppMessage:
    """One fully-resolved message, ready for a provider.

    Frozen, and deliberately **already rendered**: a provider receives a template
    name, a language tag, and an ordered list of substituted strings — not a
    descriptor name and a context. That is what keeps rendering, localization, and
    authorization on the application side of this boundary, where they are
    testable without a network — and it is what makes a second provider a class
    that formats a different HTTP request rather than one that has to learn what a
    notification is.

    **A template message rather than a free-text one, and that is not a style
    choice.** WhatsApp only permits free-form text inside a 24-hour window opened
    by the *recipient* messaging the business first. This platform sends
    unsolicited notifications to lawyers about their cases, so that window is
    never open — a text message would be refused every time. Every message this
    channel sends is therefore a template message, which is also why
    ``18-whatsapp-delivery-channel.md`` requires templates rather than suggesting
    them.
    """

    #: E.164 digits, no leading ``+``. Produced by
    #: :func:`~core.whatsapp.normalize_phone` and never by a provider.
    to_number: str
    #: The template's name as registered in the WhatsApp Business account.
    template_name: str
    #: The provider's language tag for that template's approved localization —
    #: ``fr``, ``ar``, ``en_US``. Produced by
    #: :func:`~core.whatsapp.provider_language_code`.
    language_code: str
    #: The ordered body parameters, already substituted and already screened by
    #: :func:`~core.whatsapp.sanitize_parameter`. Their **count and order must
    #: match the approved template**, which is the contract the descriptor files
    #: in ``apps/api/whatsapp/`` exist to make reviewable.
    parameters: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class WhatsAppSendResult:
    """What one provider call did.

    A value rather than an exception on the failure path, for the reason
    :class:`~services.email_provider.EmailSendResult` is one: a refused message is
    an ordinary, recordable *outcome* of a background job, and forcing every caller
    into a ``try`` block around a routine result is how a failure ends up logged
    twice and recorded once.
    """

    accepted: bool
    provider: str
    #: How long the provider call took, in milliseconds. Only the call — not
    #: rendering, not the database write around it.
    duration_ms: float = 0.0
    #: The provider's own identifier for the message — a ``wamid`` on Meta.
    #: ``None`` on failure, and on a provider that issues none.
    message_id: str | None = None
    #: Why it was refused. ``None`` on success.
    failure: WhatsAppFailureCode | None = None

    @classmethod
    def success(
        cls, *, provider: str, duration_ms: float, message_id: str | None = None
    ) -> WhatsAppSendResult:
        """A message the provider accepted."""
        return cls(
            accepted=True,
            provider=provider,
            duration_ms=duration_ms,
            message_id=message_id,
        )

    @classmethod
    def refused(
        cls,
        *,
        provider: str,
        failure: WhatsAppFailureCode,
        duration_ms: float = 0.0,
    ) -> WhatsAppSendResult:
        """A message the provider did not accept."""
        return cls(
            accepted=False,
            provider=provider,
            failure=failure,
            duration_ms=duration_ms,
        )


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


class WhatsAppProvider(Protocol):
    """What the WhatsApp Delivery Service requires of a provider.

    Four members. The narrowness is the point: a delivery service cannot
    accidentally depend on Cloud API semantics, and a replacement has four things
    to implement.
    """

    @property
    def name(self) -> str:
        """Stable identifier of the backend ("meta"). Recorded on every delivery."""
        ...

    def is_available(self) -> bool:
        """Whether this provider is configured well enough to be worth calling.

        A **configuration** check rather than a health check: it must not make a
        request, because it is consulted before every dispatch and a network round
        trip per notification would be a cost the feature cannot justify. An
        unreachable-but-configured API is discovered by :meth:`send`, which is
        where a transient failure belongs.
        """
        ...

    def configuration_errors(self) -> list[str]:
        """Which required settings are missing or unusable, **by name**.

        The spec asks that configuration be validated at startup, that a missing
        one fail gracefully, and that the failure carry a *meaningful* message.
        This is that message, and it is a list of setting names rather than a
        sentence so the startup log can print them and a monitoring endpoint can
        report them without either one interpolating a credential.

        Empty when the provider is fully configured, which makes it exactly the
        inverse of :meth:`is_available` for providers that need configuration at
        all — and legitimately empty for one that needs none.
        """
        ...

    def send(self, message: OutgoingWhatsAppMessage) -> WhatsAppSendResult:
        """Hand one message to the provider.

        **Never raises.** Every failure is translated into a
        :class:`~core.whatsapp.WhatsAppFailureCode` on the returned result — see
        the module docstring for why the provider's own message never escapes.
        """
        ...


# --------------------------------------------------------------------------- #
# Meta WhatsApp Cloud API
# --------------------------------------------------------------------------- #

#: Meta error codes this platform recognises, mapped onto its own vocabulary.
#:
#: **The whole of the transient/permanent decision for the Cloud API**, and the
#: reason it is a table rather than a chain of ``if`` statements: these are
#: documented, numeric, and stable, so the mapping is data that can be read
#: against Meta's published list rather than logic that has to be traced. An
#: unlisted code falls back to the HTTP status, and an unrecognised status falls
#: back to :attr:`~core.whatsapp.WhatsAppFailureCode.UNKNOWN`, which is transient —
#: because the alternative is discarding a hearing notice over a code nobody has
#: seen before.
#:
#: The template codes are the interesting group. They are *permanent* and they are
#: their own failure class, because every one of them is fixed in the WhatsApp
#: Business account rather than in this repository: a template that was never
#: approved, was paused for quality, was deleted, does not exist in the language
#: asked for, or was sent the wrong number of parameters. An operator reading
#: ``template_rejected`` in the metrics knows to open Business Manager; the same
#: failure filed under ``message_refused`` would send them to read this code.
META_ERROR_CODES: Mapping[int, WhatsAppFailureCode] = MappingProxyType(
    {
        # --- Authentication and account state ------------------------------- #
        0: WhatsAppFailureCode.AUTHENTICATION_FAILED,  # AuthException
        3: WhatsAppFailureCode.AUTHENTICATION_FAILED,  # no permission for this call
        10: WhatsAppFailureCode.AUTHENTICATION_FAILED,  # permission denied
        33: WhatsAppFailureCode.AUTHENTICATION_FAILED,  # object not visible to this app
        190: WhatsAppFailureCode.AUTHENTICATION_FAILED,  # access token expired
        200: WhatsAppFailureCode.AUTHENTICATION_FAILED,  # permission error
        # --- Rate limiting --------------------------------------------------- #
        4: WhatsAppFailureCode.THROTTLED,  # too many API calls
        80007: WhatsAppFailureCode.THROTTLED,  # rate limit issues
        130429: WhatsAppFailureCode.THROTTLED,  # cloud API message throughput
        131048: WhatsAppFailureCode.THROTTLED,  # spam rate limit hit
        131056: WhatsAppFailureCode.THROTTLED,  # pair rate limit hit
        133016: WhatsAppFailureCode.THROTTLED,  # rate limit on the business number
        # --- Temporary ------------------------------------------------------- #
        1: WhatsAppFailureCode.UNKNOWN,  # unknown API error
        2: WhatsAppFailureCode.PROVIDER_UNAVAILABLE,  # temporary service outage
        131000: WhatsAppFailureCode.UNKNOWN,  # something went wrong
        # --- The recipient ---------------------------------------------------- #
        131021: WhatsAppFailureCode.INVALID_RECIPIENT,  # recipient is the sender
        131026: WhatsAppFailureCode.RECIPIENT_REFUSED,  # message undeliverable
        131052: WhatsAppFailureCode.RECIPIENT_REFUSED,  # media/download unreachable
        # --- The message ------------------------------------------------------ #
        131008: WhatsAppFailureCode.MESSAGE_REFUSED,  # required parameter missing
        131009: WhatsAppFailureCode.MESSAGE_REFUSED,  # parameter value invalid
        131047: WhatsAppFailureCode.MESSAGE_REFUSED,  # re-engagement required
        # --- The template ------------------------------------------------------ #
        132000: WhatsAppFailureCode.TEMPLATE_REJECTED,  # parameter count mismatch
        132001: WhatsAppFailureCode.TEMPLATE_REJECTED,  # template does not exist
        132005: WhatsAppFailureCode.TEMPLATE_REJECTED,  # translated text too long
        132007: WhatsAppFailureCode.TEMPLATE_REJECTED,  # format character policy
        132012: WhatsAppFailureCode.TEMPLATE_REJECTED,  # parameter format mismatch
        132015: WhatsAppFailureCode.TEMPLATE_REJECTED,  # template is paused
        132016: WhatsAppFailureCode.TEMPLATE_REJECTED,  # template is disabled
        132068: WhatsAppFailureCode.TEMPLATE_REJECTED,  # flow is blocked
        132069: WhatsAppFailureCode.TEMPLATE_REJECTED,  # flow is throttled
    }
)

#: HTTP statuses this platform recognises, for a Meta error code it does not.
#:
#: The second half of the classification and the coarser one. A ``4xx`` the table
#: above did not name is permanent — the request is wrong and repeating it will be
#: wrong the same way — with ``408``, ``429``, and every ``5xx`` the exceptions,
#: because those are the server saying "not now" rather than "not ever".
_STATUS_FAILURES: Mapping[int, WhatsAppFailureCode] = MappingProxyType(
    {
        400: WhatsAppFailureCode.MESSAGE_REFUSED,
        401: WhatsAppFailureCode.AUTHENTICATION_FAILED,
        403: WhatsAppFailureCode.AUTHENTICATION_FAILED,
        404: WhatsAppFailureCode.MESSAGE_REFUSED,
        408: WhatsAppFailureCode.TIMEOUT,
        429: WhatsAppFailureCode.THROTTLED,
    }
)


@dataclass(frozen=True, slots=True)
class _MetaError:
    """The two fields of a Cloud API error body this platform is willing to keep.

    Deliberately **not** the ``message``, the ``error_data.details``, or the
    ``fbtrace_id``: the first two quote the request (the recipient's number, the
    template parameters) and the third is a per-request identifier that would give
    a monitoring breakdown one bucket per occurrence. A numeric code and subcode
    are stable platform vocabulary and are safe to record.
    """

    code: int | None = None
    subcode: int | None = None


class MetaWhatsAppProvider:
    """Delivery over the Meta WhatsApp Cloud API, which is what the spec names.

    Uses the standard library, so this feature adds **no dependency** — the same
    outcome the SMTP provider reached, and a genuine property of the Cloud API
    being one JSON ``POST`` rather than a protocol needing a client.

    **A request per message, not a pooled connection.** That looks wasteful and is
    the right trade at this volume: the platform's WhatsApp traffic is a handful of
    messages per case event, a pooled HTTPS connection held open across a quiet
    period is closed by the far end anyway, and a pool would need its own health
    checking, its own locking, and its own failure mode inside a class whose whole
    purpose is to be replaceable. ``WHATSAPP_WORKER_CONCURRENCY`` bounds how many
    exist at once. The spec's *"minimize provider requests"* is met where it
    actually costs — one message per notification, guaranteed by a unique
    constraint — rather than by reusing sockets.

    **Rate limits are prepared for rather than modelled.** The spec asks the
    implementation to *"prepare for provider rate limits"*, and the preparation is
    three things that are already here: a small bounded worker pool, so the
    platform never bursts; ``429`` and Meta's five rate-limit codes classified as
    :attr:`~core.whatsapp.WhatsAppFailureCode.THROTTLED`, so they are retried
    rather than discarded; and an exponential backoff with a ceiling, so a
    throttled deployment slows down instead of hammering. A token bucket on this
    side would be a fourth mechanism guessing at a limit the provider publishes per
    account and changes without notice.
    """

    #: The identifier recorded for this backend.
    name = "meta"

    def __init__(
        self,
        *,
        access_token: str | None = None,
        phone_number_id: str | None = None,
        business_account_id: str | None = None,
        api_version: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._access_token = (
            access_token if access_token is not None else settings.WHATSAPP_ACCESS_TOKEN
        ) or None
        self._phone_number_id = (
            phone_number_id
            if phone_number_id is not None
            else settings.WHATSAPP_PHONE_NUMBER_ID
        ) or None
        self._business_account_id = (
            business_account_id
            if business_account_id is not None
            else settings.WHATSAPP_BUSINESS_ACCOUNT_ID
        ) or None
        self._api_version = (
            api_version if api_version is not None else settings.WHATSAPP_API_VERSION
        )
        self._base_url = (
            base_url if base_url is not None else settings.WHATSAPP_API_BASE_URL
        ).rstrip("/")
        self._timeout = (
            timeout if timeout is not None else float(settings.WHATSAPP_TIMEOUT_SECONDS)
        )

    # ------------------------------------------------------------ identity #

    def is_available(self) -> bool:
        """Whether a token and a sending number are configured.

        The absence of either is handled rather than fatal, exactly as a missing
        Tesseract and a missing ``LLM_API_KEY`` are: the API starts, every other
        feature works, ``GET /notifications/whatsapp/metrics`` reports
        ``provider_available: false`` **with the missing setting names**, and no
        delivery row is written at all — which is better than a backlog of
        failures nobody asked for.
        """
        return not self.configuration_errors()

    def configuration_errors(self) -> list[str]:
        """Which required settings are missing, by name.

        ``WHATSAPP_BUSINESS_ACCOUNT_ID`` is deliberately **not** required for
        sending: the message endpoint is addressed by phone number identifier
        alone, and the business account identifier is what a future
        template-management or webhook-subscription call would need. The spec lists
        it among the required configuration and it is therefore a setting; making
        an unset one block delivery would refuse to send messages over a value
        nothing in the send path reads. It is reported by
        :meth:`missing_optional_configuration` instead, so an operator still sees
        that it is unset.
        """
        missing: list[str] = []
        if not self._access_token:
            missing.append("WHATSAPP_ACCESS_TOKEN")
        if not self._phone_number_id:
            missing.append("WHATSAPP_PHONE_NUMBER_ID")
        if not self._api_version:
            missing.append("WHATSAPP_API_VERSION")
        return missing

    def missing_optional_configuration(self) -> list[str]:
        """Configured-by-the-spec settings the send path does not need.

        Reported separately from :meth:`configuration_errors` so that "you cannot
        send" and "you have not finished configuring" are two different statements
        in a startup log, rather than one list an operator has to rank.
        """
        return [] if self._business_account_id else ["WHATSAPP_BUSINESS_ACCOUNT_ID"]

    @property
    def endpoint(self) -> str:
        """The messages endpoint this provider posts to.

        A property rather than a constant because the API version and the host are
        both configuration: ``WHATSAPP_API_VERSION`` is how a deployment moves to a
        newer Graph API without a release, and ``WHATSAPP_API_BASE_URL`` is what
        points an integration test at a local stub instead of at Meta.
        """
        return f"{self._base_url}/{self._api_version}/{self._phone_number_id}/messages"

    # ---------------------------------------------------------------- send #

    def send(self, message: OutgoingWhatsAppMessage) -> WhatsAppSendResult:
        """Compose and post one template message. Never raises."""
        if not self.is_available():
            return WhatsAppSendResult.refused(
                provider=self.name, failure=WhatsAppFailureCode.PROVIDER_UNAVAILABLE
            )

        payload = self._compose(message)

        started = time.perf_counter()
        try:
            body = self._post(payload)
        except _MetaResponseError as exc:
            elapsed = (time.perf_counter() - started) * 1000.0
            failure = self._classify_response(exc.status, exc.error)
            # The HTTP status and Meta's numeric code — **never the response
            # body**, which quotes the recipient's number and the template
            # parameters. See the module docstring.
            logger.warning(
                "whatsapp_send_refused",
                provider=self.name,
                error_code=failure.value,
                http_status=exc.status,
                meta_code=exc.error.code,
                meta_subcode=exc.error.subcode,
                duration_ms=round(elapsed, 2),
            )
            return WhatsAppSendResult.refused(
                provider=self.name, failure=failure, duration_ms=elapsed
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000.0
            failure = self._classify_transport(exc)
            # The exception **type** and the code, never `str(exc)`: a urllib error
            # interpolates the URL, which carries the phone number identifier.
            logger.warning(
                "whatsapp_send_failed",
                provider=self.name,
                error_code=failure.value,
                error_type=type(exc).__name__,
                duration_ms=round(elapsed, 2),
            )
            return WhatsAppSendResult.refused(
                provider=self.name, failure=failure, duration_ms=elapsed
            )

        return WhatsAppSendResult.success(
            provider=self.name,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            message_id=_message_id(body),
        )

    # ------------------------------------------------------------- helpers #

    def _compose(self, message: OutgoingWhatsAppMessage) -> dict[str, Any]:
        """Build the Cloud API request body for one template message.

        Every parameter passes through :func:`~core.whatsapp.sanitize_parameter`
        **again** here, at the last point before it becomes a request. The
        delivery service already did it; doing it twice is deliberate, and it is
        the same reasoning ``SmtpEmailProvider._address`` re-validates an address
        it was handed: this is the boundary that would be blamed for a malformed
        payload, and a check at the boundary cannot be skipped by a future caller
        who did not know about the first one.
        """
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.to_number,
            "type": "template",
            "template": {
                "name": message.template_name,
                "language": {"code": message.language_code},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": sanitize_parameter(value)}
                            for value in message.parameters
                        ],
                    }
                ],
            },
        }

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Send one request and return the decoded response body.

        Raises:
            _MetaResponseError: the API answered with an error status.
            Exception: anything the transport itself raised — a DNS failure, a
                refused connection, a timeout, a TLS error, an unreadable body.
                Classified by :meth:`_classify_transport`; none of them escapes
                :meth:`send`.
        """
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                # The credential. Never logged, never in an exception, never in a
                # metric — the same posture SMTP_PASSWORD and LLM_API_KEY take.
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout
            ) as response:
                return _decode(response.read())
        except urllib.error.HTTPError as exc:
            # An error *status* rather than a transport failure: the API answered,
            # and its answer is the classification. The body is read for its
            # numeric code and then discarded — see `_MetaError`.
            raise _MetaResponseError(
                status=int(exc.code), error=_error_of(_safe_read(exc))
            ) from exc

    @staticmethod
    def _classify_response(
        status: int, error: _MetaError
    ) -> WhatsAppFailureCode:
        """Translate one Cloud API error answer into a platform failure code.

        Meta's **numeric code first**, because it is the specific answer — a
        ``400`` carrying ``132001`` is an unapproved template and a ``400``
        carrying ``131009`` is a bad parameter, and those send an operator to two
        different places. The HTTP status is the fallback for a code this platform
        has not seen, and ``UNKNOWN`` is the fallback for a status it does not
        recognise — transient in both cases, deliberately, because discarding a
        notification over an unfamiliar answer is the worse error.
        """
        if error.code is not None and error.code in META_ERROR_CODES:
            return META_ERROR_CODES[error.code]
        if status in _STATUS_FAILURES:
            return _STATUS_FAILURES[status]
        if status >= 500:
            # The provider is having a bad time. Transient by definition.
            return WhatsAppFailureCode.PROVIDER_UNAVAILABLE
        if 400 <= status < 500:
            return WhatsAppFailureCode.MESSAGE_REFUSED
        return WhatsAppFailureCode.UNKNOWN

    @staticmethod
    def _classify_transport(exc: Exception) -> WhatsAppFailureCode:
        """Translate one transport exception into a platform failure code.

        Everything here is transient, and that is not an oversight: a DNS failure,
        a refused connection, a dropped TLS handshake, and a read timeout are all
        statements about the network at one instant rather than about the message.
        The one non-network case — a response this platform could not decode — is
        classified as a connection failure for the same reason: something between
        here and Meta answered with something that was not the API.
        """
        import socket
        import ssl
        import urllib.error

        if isinstance(exc, TimeoutError | socket.timeout):
            return WhatsAppFailureCode.TIMEOUT
        if isinstance(exc, urllib.error.URLError):
            # `URLError.reason` is commonly a socket error, and a timeout arrives
            # wrapped in one rather than raised directly.
            if isinstance(exc.reason, TimeoutError | socket.timeout):
                return WhatsAppFailureCode.TIMEOUT
            return WhatsAppFailureCode.CONNECTION_FAILED
        if isinstance(exc, ssl.SSLError | OSError):
            # `OSError` covers `ConnectionRefusedError` and every DNS failure, and
            # it is after `TimeoutError` because that is a subclass of it.
            return WhatsAppFailureCode.CONNECTION_FAILED
        if isinstance(exc, ValueError):
            # A body that was not JSON, or was JSON of the wrong shape.
            return WhatsAppFailureCode.CONNECTION_FAILED
        return WhatsAppFailureCode.UNKNOWN


class _MetaResponseError(Exception):
    """The Cloud API answered with an error status.

    Private, and carries **only** the status and the numeric codes — constructing
    it is where the response body is dropped, which is what makes "the provider's
    message never escapes this module" true at a single point rather than at every
    ``raise``.
    """

    def __init__(self, *, status: int, error: _MetaError) -> None:
        # No message, deliberately: the string form of this exception is what
        # would end up in a log if anything ever interpolated it.
        super().__init__(f"WhatsApp provider returned HTTP {status}")
        self.status = status
        self.error = error


def _safe_read(response: Any) -> bytes:
    """Read an error response's body, tolerating one that cannot be read."""
    try:
        return bytes(response.read())
    except Exception:  # pragma: no cover - a body that is already gone
        return b""


def _decode(raw: bytes) -> Mapping[str, Any]:
    """Decode a JSON response body.

    Raises:
        ValueError: the body was not JSON, or was not a JSON object. Classified
            as a connection failure — see
            :meth:`MetaWhatsAppProvider._classify_transport`.
    """
    decoded = json.loads(raw.decode("utf-8") or "{}")
    if not isinstance(decoded, dict):
        raise ValueError("The provider returned a body that was not a JSON object.")
    return decoded


def _error_of(raw: bytes) -> _MetaError:
    """Pull the numeric codes out of an error body, and nothing else.

    Never raises: this runs on a path that is already reporting a failure, and a
    malformed error body must not replace a classified refusal with an unhandled
    exception. An unreadable body simply produces an empty
    :class:`_MetaError`, which falls the classification back to the HTTP status.
    """
    try:
        body = _decode(raw)
    except Exception:
        return _MetaError()

    error = body.get("error")
    if not isinstance(error, dict):
        return _MetaError()

    return _MetaError(
        code=_as_int(error.get("code")), subcode=_as_int(error.get("error_subcode"))
    )


def _as_int(value: Any) -> int | None:
    """Read a JSON number that should be an integer, tolerating anything else."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _message_id(body: Mapping[str, Any]) -> str | None:
    """The ``wamid`` from a successful response, or ``None``.

    ``None`` rather than an exception on an unexpected shape: the message **was
    accepted** — that is what the 200 means — and failing the delivery because the
    platform could not find an identifier it only records for troubleshooting would
    turn a success into a retry, which is how one message becomes two.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    first = messages[0]
    if not isinstance(first, dict):
        return None
    identifier = first.get("id")
    return str(identifier)[:128] if identifier else None


# --------------------------------------------------------------------------- #
# The no-op
# --------------------------------------------------------------------------- #


class NullWhatsAppProvider:
    """A provider that accepts every message and sends nothing.

    Two legitimate uses, and neither is a shortcut:

    * an **integration test**, which asserts that a delivery reached ``delivered``
      without a Cloud API in the loop — and, more importantly here than for email,
      without a WhatsApp Business account, an approved template, and a real phone
      number, none of which a test suite can have;
    * a **deployment that wants the delivery pipeline switched on without a
      business account** — a staging environment, a demonstration — where writing
      delivery rows and exercising the descriptors is the point and actually
      messaging anybody is not.

    It reports itself as available, deliberately: a provider that claimed to be
    unavailable would make the delivery service skip dispatch entirely, and a test
    asserting on delivery rows would then be asserting on nothing.
    """

    name = "null"

    def __init__(self) -> None:
        self.sent: list[OutgoingWhatsAppMessage] = []
        self._counter = 0
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        """Always. See the class docstring."""
        return True

    def configuration_errors(self) -> list[str]:
        """None: this provider needs no configuration at all."""
        return []

    def send(self, message: OutgoingWhatsAppMessage) -> WhatsAppSendResult:
        """Record the message and report success.

        Logged at **debug**, and by template rather than by recipient: even a
        discarding provider must not be the one place a phone number ends up in the
        application log. The synthetic identifier is what lets a test assert that
        the ``provider_message_id`` column is written at all.
        """
        with self._lock:
            self._counter += 1
            sequence = self._counter
            self.sent.append(message)

        logger.debug(
            "whatsapp_discarded", provider=self.name, template=message.template_name
        )
        return WhatsAppSendResult.success(
            provider=self.name, duration_ms=0.0, message_id=f"null-{sequence}"
        )


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

#: Every provider this build can be configured to use.
#:
#: Adding one is a class implementing :class:`WhatsAppProvider` plus an entry here
#: — the same shape as :data:`~services.email_provider.EMAIL_PROVIDER_FACTORIES`
#: and :data:`~services.embedding.EMBEDDER_FACTORIES`. **That is the whole of what
#: "the implementation should allow future providers such as Twilio, Vonage,
#: future providers without changing application logic" costs**: each formats an
#: HTTP request from an :class:`OutgoingWhatsAppMessage` and maps its status codes
#: onto :class:`~core.whatsapp.WhatsAppFailureCode`, and nothing above this line
#: moves.
WHATSAPP_PROVIDER_FACTORIES: Mapping[str, type[Any]] = MappingProxyType(
    {
        MetaWhatsAppProvider.name: MetaWhatsAppProvider,
        NullWhatsAppProvider.name: NullWhatsAppProvider,
    }
)

#: The one provider the process shares, built on first use.
#:
#: Shared rather than per request because it holds configuration and, for a future
#: provider, would hold a connection pool — exactly as
#: :func:`~services.email_provider.get_email_provider` and
#: :func:`~services.llm.get_llm_provider` are — and because the delivery worker has
#: no request to build one from.
_shared: dict[str, WhatsAppProvider] = {}
_shared_lock = threading.Lock()


def available_whatsapp_providers() -> list[str]:
    """Every provider identifier this build can be configured to use."""
    return sorted(WHATSAPP_PROVIDER_FACTORIES)


def get_whatsapp_provider(identifier: str | None = None) -> WhatsAppProvider:
    """Return the configured WhatsApp provider, shared across the process.

    Falls back to the default backend for an unrecognised identifier rather than
    failing startup, with the fallback logged so the misconfiguration is visible —
    the same posture :func:`~services.email_provider.get_email_provider` and
    :func:`~services.llm.get_llm_provider` take, and for the same reason: an API
    that refuses to come up over a messaging setting would take authentication,
    cases, and documents down with it.
    """
    wanted = (identifier or settings.WHATSAPP_PROVIDER).strip().lower()

    factory = WHATSAPP_PROVIDER_FACTORIES.get(wanted)
    if factory is None:
        logger.warning(
            "whatsapp_provider_unknown",
            requested=wanted,
            fallback=MetaWhatsAppProvider.name,
        )
        wanted = MetaWhatsAppProvider.name
        factory = MetaWhatsAppProvider

    cached = _shared.get(wanted)
    if cached is not None:
        return cached

    with _shared_lock:
        existing = _shared.get(wanted)
        if existing is not None:
            return existing
        built: WhatsAppProvider = factory()
        _shared[wanted] = built
        return built


def reset_whatsapp_provider_cache() -> None:
    """Discard the shared provider. For tests, and after a configuration change."""
    with _shared_lock:
        _shared.clear()


__all__ = [
    "META_ERROR_CODES",
    "WHATSAPP_PROVIDER_FACTORIES",
    "MetaWhatsAppProvider",
    "NullWhatsAppProvider",
    "OutgoingWhatsAppMessage",
    "WhatsAppProvider",
    "WhatsAppSendResult",
    "available_whatsapp_providers",
    "get_whatsapp_provider",
    "reset_whatsapp_provider_cache",
]
