"""Unit tests for ``services/whatsapp_provider.py`` — the provider boundary.

The Cloud API is replaced at the **transport** rather than at the class: these
tests drive the real :class:`~services.whatsapp_provider.MetaWhatsAppProvider`
with ``urllib.request.urlopen`` patched, so the request it composes, the response
it reads, and — the part that actually matters — the way it **classifies** a
failure are all the production ones.

Three properties are what these tests exist for, and each is a requirement of
``18-whatsapp-delivery-channel.md`` rather than a nicety:

* the request is a **template message** with the parameters in order, because a
  free-text message would be refused outside a 24-hour window that never opens
  for outbound notifications;
* every failure becomes a :class:`~core.whatsapp.WhatsAppFailureCode`, and the
  transient/permanent split is what the retry policy is built on;
* the provider **never raises**, so a background worker always has an outcome to
  record.
"""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Iterator
from types import TracebackType
from typing import Any

import pytest

from core.whatsapp import WhatsAppFailureCode
from services.whatsapp_provider import (
    MetaWhatsAppProvider,
    NullWhatsAppProvider,
    OutgoingWhatsAppMessage,
    available_whatsapp_providers,
    get_whatsapp_provider,
    reset_whatsapp_provider_cache,
)

MESSAGE = OutgoingWhatsAppMessage(
    to_number="212612345678",
    template_name="notification",
    language_code="fr",
    parameters=("Bonjour Amina", "Dossier attribué", "Vous avez été affecté(e).", "Ouvrir"),
)


# --------------------------------------------------------------------------- #
# Transport doubles
# --------------------------------------------------------------------------- #


class _Response:
    """The context manager ``urlopen`` returns on success."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _ErrorBody:
    """Just enough of an ``HTTPError`` body for the provider to read a code off.

    ``close`` is present because ``HTTPError`` inherits from
    ``tempfile._TemporaryFileWrapper``'s cleanup path and calls it on collection;
    without it every one of these tests emits an unraisable ``AttributeError``
    from a finalizer, which is noise that would eventually hide a real one.
    """

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._raw = b"" if payload is None else json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def close(self) -> None:
        return None


def _http_error(status: int, *, code: int | None = None) -> urllib.error.HTTPError:
    payload = (
        None
        if code is None
        else {
            "error": {
                # The provider must keep the numeric code and drop everything
                # else — the message quotes the recipient's number.
                "message": "(#131026) Message undeliverable to 212612345678",
                "type": "OAuthException",
                "code": code,
                "error_subcode": 2494010,
                "fbtrace_id": "AbC123",
            }
        }
    )
    return urllib.error.HTTPError(
        url="https://graph.facebook.com/v23.0/1/messages",
        code=status,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=_ErrorBody(payload),  # type: ignore[arg-type]
    )


@pytest.fixture
def provider() -> MetaWhatsAppProvider:
    return MetaWhatsAppProvider(
        access_token="token-value",
        phone_number_id="1234567890",
        business_account_id="9876543210",
        api_version="v23.0",
        base_url="https://graph.facebook.com",
        timeout=2.0,
    )


@pytest.fixture(autouse=True)
def _clear_shared_provider() -> Iterator[None]:
    reset_whatsapp_provider_cache()
    yield
    reset_whatsapp_provider_cache()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


class TestConfiguration:
    def test_a_configured_provider_is_available(
        self, provider: MetaWhatsAppProvider
    ) -> None:
        assert provider.is_available()
        assert provider.configuration_errors() == []

    def test_missing_settings_are_reported_by_name(self) -> None:
        """The spec's *"provide meaningful error messages"* — and by **name**, so
        the meaningful message is never a credential."""
        bare = MetaWhatsAppProvider(
            access_token=None, phone_number_id=None, business_account_id=None
        )
        assert not bare.is_available()
        assert set(bare.configuration_errors()) == {
            "WHATSAPP_ACCESS_TOKEN",
            "WHATSAPP_PHONE_NUMBER_ID",
        }

    def test_the_business_account_id_does_not_block_sending(self) -> None:
        """The messages endpoint is addressed by phone number identifier alone, so
        refusing to send over a value the send path never reads would be a
        self-inflicted outage. It is reported as *incomplete* instead."""
        partial = MetaWhatsAppProvider(
            access_token="token", phone_number_id="1", business_account_id=None
        )
        assert partial.is_available()
        assert partial.missing_optional_configuration() == [
            "WHATSAPP_BUSINESS_ACCOUNT_ID"
        ]

    def test_an_unconfigured_provider_refuses_rather_than_raising(self) -> None:
        bare = MetaWhatsAppProvider(access_token=None, phone_number_id=None)
        result = bare.send(MESSAGE)
        assert result.accepted is False
        assert result.failure is WhatsAppFailureCode.PROVIDER_UNAVAILABLE

    def test_the_endpoint_is_built_from_configuration(
        self, provider: MetaWhatsAppProvider
    ) -> None:
        """The API version is a setting so a deployment can move to a newer Graph
        API without a release."""
        assert (
            provider.endpoint
            == "https://graph.facebook.com/v23.0/1234567890/messages"
        )


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


class TestRequest:
    def test_it_sends_a_template_message_with_ordered_parameters(
        self, provider: MetaWhatsAppProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A *template* message, because WhatsApp only permits free text inside a
        24-hour window opened by the recipient — which never opens for an outbound
        notification about somebody's case."""
        captured: dict[str, Any] = {}

        def _urlopen(request: Any, timeout: float | None = None) -> _Response:
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _Response({"messages": [{"id": "wamid.ABC"}]})

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        result = provider.send(MESSAGE)

        assert result.accepted is True
        assert result.message_id == "wamid.ABC"
        body = captured["body"]
        assert body["messaging_product"] == "whatsapp"
        assert body["to"] == "212612345678"
        assert body["type"] == "template"
        assert body["template"]["name"] == "notification"
        assert body["template"]["language"] == {"code": "fr"}
        assert [
            parameter["text"]
            for parameter in body["template"]["components"][0]["parameters"]
        ] == list(MESSAGE.parameters)

    def test_the_token_travels_as_a_bearer_credential(
        self, provider: MetaWhatsAppProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _urlopen(request: Any, timeout: float | None = None) -> _Response:
            captured.update(dict(request.header_items()))
            return _Response({"messages": [{"id": "wamid.ABC"}]})

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        provider.send(MESSAGE)

        assert captured["Authorization"] == "Bearer token-value"

    def test_parameters_are_screened_at_this_boundary_too(
        self, provider: MetaWhatsAppProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The delivery service screens them already. Doing it again here is the
        same reasoning `SmtpEmailProvider._address` re-validates an address: this
        is the boundary that would be blamed for a malformed payload, and a check
        here cannot be skipped by a future caller who did not know about the
        first."""
        captured: dict[str, Any] = {}

        def _urlopen(request: Any, timeout: float | None = None) -> _Response:
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Response({"messages": [{"id": "wamid.ABC"}]})

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        provider.send(
            OutgoingWhatsAppMessage(
                to_number="212612345678",
                template_name="notification",
                language_code="fr",
                parameters=("line one\nline two",),
            )
        )

        parameters = captured["body"]["template"]["components"][0]["parameters"]
        assert parameters[0]["text"] == "line one line two"

    def test_a_response_without_an_identifier_is_still_a_success(
        self, provider: MetaWhatsAppProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 200 *is* the acceptance. Failing the delivery because the platform
        could not find an identifier it only records for troubleshooting would
        turn a success into a retry, which is how one message becomes two."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda request, timeout=None: _Response({"messages": []}),
        )
        result = provider.send(MESSAGE)
        assert result.accepted is True
        assert result.message_id is None


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


class TestFailureClassification:
    @pytest.mark.parametrize(
        ("meta_code", "expected"),
        [
            (190, WhatsAppFailureCode.AUTHENTICATION_FAILED),
            (130429, WhatsAppFailureCode.THROTTLED),
            (131048, WhatsAppFailureCode.THROTTLED),
            (131026, WhatsAppFailureCode.RECIPIENT_REFUSED),
            (131009, WhatsAppFailureCode.MESSAGE_REFUSED),
            (132000, WhatsAppFailureCode.TEMPLATE_REJECTED),
            (132001, WhatsAppFailureCode.TEMPLATE_REJECTED),
            (132015, WhatsAppFailureCode.TEMPLATE_REJECTED),
        ],
    )
    def test_metas_numeric_code_decides(
        self,
        provider: MetaWhatsAppProvider,
        monkeypatch: pytest.MonkeyPatch,
        meta_code: int,
        expected: WhatsAppFailureCode,
    ) -> None:
        """The **specific** answer, checked before the HTTP status: a `400`
        carrying `132001` is an unapproved template and a `400` carrying `131009`
        is a bad parameter, and those send an operator to two different consoles."""

        def _urlopen(request: Any, timeout: float | None = None) -> _Response:
            raise _http_error(400, code=meta_code)

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        result = provider.send(MESSAGE)

        assert result.accepted is False
        assert result.failure is expected

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, WhatsAppFailureCode.AUTHENTICATION_FAILED),
            (403, WhatsAppFailureCode.AUTHENTICATION_FAILED),
            (429, WhatsAppFailureCode.THROTTLED),
            (400, WhatsAppFailureCode.MESSAGE_REFUSED),
            (500, WhatsAppFailureCode.PROVIDER_UNAVAILABLE),
            (503, WhatsAppFailureCode.PROVIDER_UNAVAILABLE),
        ],
    )
    def test_the_http_status_is_the_fallback(
        self,
        provider: MetaWhatsAppProvider,
        monkeypatch: pytest.MonkeyPatch,
        status: int,
        expected: WhatsAppFailureCode,
    ) -> None:
        """For a code this platform has not seen. A `5xx` is transient because it
        is the server saying "not now" rather than "not ever"."""

        def _urlopen(request: Any, timeout: float | None = None) -> _Response:
            raise _http_error(status)

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        assert provider.send(MESSAGE).failure is expected

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (TimeoutError("slow"), WhatsAppFailureCode.TIMEOUT),
            (TimeoutError("slow"), WhatsAppFailureCode.TIMEOUT),
            (urllib.error.URLError("no route"), WhatsAppFailureCode.CONNECTION_FAILED),
            (
                urllib.error.URLError(TimeoutError("slow")),
                WhatsAppFailureCode.TIMEOUT,
            ),
            (ConnectionRefusedError("refused"), WhatsAppFailureCode.CONNECTION_FAILED),
        ],
    )
    def test_transport_failures_are_all_transient(
        self,
        provider: MetaWhatsAppProvider,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
        expected: WhatsAppFailureCode,
    ) -> None:
        """Each is a statement about the network at one instant rather than about
        the message."""

        def _urlopen(request: Any, timeout: float | None = None) -> _Response:
            raise error

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        assert provider.send(MESSAGE).failure is expected

    def test_an_undecodable_body_is_a_connection_failure(
        self, provider: MetaWhatsAppProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Something between here and Meta answered with something that was not
        the API."""

        class _Garbage:
            def __enter__(self) -> _Garbage:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"<html>proxy error</html>"

        monkeypatch.setattr(
            "urllib.request.urlopen", lambda request, timeout=None: _Garbage()
        )
        assert provider.send(MESSAGE).failure is WhatsAppFailureCode.CONNECTION_FAILED

    def test_an_error_body_that_cannot_be_read_falls_back_to_the_status(
        self, provider: MetaWhatsAppProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed error body must not replace a classified refusal with an
        unhandled exception on a path that is already reporting a failure."""

        def _urlopen(request: Any, timeout: float | None = None) -> _Response:
            raise _http_error(429)

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        assert provider.send(MESSAGE).failure is WhatsAppFailureCode.THROTTLED

    def test_send_never_raises(
        self, provider: MetaWhatsAppProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The contract a background worker depends on: there is always an outcome
        to record."""

        def _urlopen(request: Any, timeout: float | None = None) -> _Response:
            raise RuntimeError("something nobody anticipated")

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        result = provider.send(MESSAGE)
        assert result.accepted is False
        assert result.failure is WhatsAppFailureCode.UNKNOWN

    def test_the_provider_message_never_escapes(
        self, provider: MetaWhatsAppProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Cloud API error body quotes the recipient's number. What leaves this
        module is a code, a status, and a numeric error code — the result carries
        no text at all."""

        def _urlopen(request: Any, timeout: float | None = None) -> _Response:
            raise _http_error(400, code=131026)

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        result = provider.send(MESSAGE)

        assert "212612345678" not in repr(result)
        assert "undeliverable" not in repr(result).lower()


# --------------------------------------------------------------------------- #
# The no-op, and resolution
# --------------------------------------------------------------------------- #


class TestNullProvider:
    def test_it_accepts_and_records(self) -> None:
        null = NullWhatsAppProvider()
        result = null.send(MESSAGE)

        assert result.accepted is True
        assert null.sent == [MESSAGE]

    def test_it_issues_an_identifier(self) -> None:
        """So a test can assert that `provider_message_id` is written at all."""
        assert NullWhatsAppProvider().send(MESSAGE).message_id is not None

    def test_it_reports_itself_available(self) -> None:
        """Deliberately: a provider that claimed otherwise would make the delivery
        service skip dispatch entirely, and a test asserting on delivery rows
        would then be asserting on nothing."""
        assert NullWhatsAppProvider().is_available()
        assert NullWhatsAppProvider().configuration_errors() == []


class TestResolution:
    def test_both_backends_are_offered(self) -> None:
        assert available_whatsapp_providers() == ["meta", "null"]

    def test_an_unknown_identifier_falls_back_rather_than_failing(self) -> None:
        """An API that refused to come up over a messaging setting would take
        authentication, cases, and documents down with it."""
        assert isinstance(get_whatsapp_provider("invented"), MetaWhatsAppProvider)

    def test_the_provider_is_shared_across_the_process(self) -> None:
        assert get_whatsapp_provider("null") is get_whatsapp_provider("null")
