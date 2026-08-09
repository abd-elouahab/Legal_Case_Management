"""Unit tests for ``services/email_provider.py`` — the provider abstraction.

No mail server anywhere in this file. What is under test is the boundary itself:
that a message is composed as a two-part MIME document, that every SMTP failure
is translated into a platform failure code on the right side of the
transient/permanent line, that a header-injection attempt is refused rather than
sent, and that swapping the provider is a registry entry rather than a change to
anything above it.

The composition tests reach into the private ``_compose`` deliberately: it is the
only place the platform decides what a message *looks like*, and asserting on it
through a fake SMTP server would test the standard library instead.
"""

from __future__ import annotations

import smtplib
import ssl
from typing import Any

import pytest

from core.email import EmailFailureCode, InvalidEmailRecipientError, is_transient
from services.email_provider import (
    EMAIL_PROVIDER_FACTORIES,
    EmailSendResult,
    NullEmailProvider,
    OutgoingEmail,
    SmtpEmailProvider,
    available_email_providers,
    get_email_provider,
    reset_email_provider_cache,
)


@pytest.fixture
def message() -> OutgoingEmail:
    return OutgoingEmail(
        to_address="amina@firm.example",
        to_name="Amina Benali",
        subject="Dossier attribué",
        html_body="<html><body><p>Bonjour</p></body></html>",
        text_body="Bonjour",
        from_address="notifications@legal.example",
        from_name="Legal Platform",
    )


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #


class TestComposition:
    def test_both_bodies_are_carried(self, message: OutgoingEmail) -> None:
        """`17-email-delivery-channel.md` requires HTML *and* plain text, and one
        `multipart/alternative` message carrying both is how a screen reader and a
        plain-text client see anything at all."""
        composed = SmtpEmailProvider(host="localhost")._compose(message)
        parts = [part.get_content_type() for part in composed.walk()]
        assert "text/plain" in parts
        assert "text/html" in parts

    def test_the_plain_text_part_comes_first(self, message: OutgoingEmail) -> None:
        """`multipart/alternative` is ordered least-preferred to most-preferred,
        so reversing these makes every rich client show the plain text."""
        composed = SmtpEmailProvider(host="localhost")._compose(message)
        bodies = [
            part.get_content_type()
            for part in composed.walk()
            if part.get_content_type().startswith("text/")
        ]
        assert bodies == ["text/plain", "text/html"]

    def test_the_envelope_headers_are_set(self, message: OutgoingEmail) -> None:
        composed = SmtpEmailProvider(host="localhost")._compose(message)
        assert composed["Subject"] == "Dossier attribué"
        assert "amina@firm.example" in composed["To"]
        assert "notifications@legal.example" in composed["From"]

    def test_generated_mail_says_so(self, message: OutgoingEmail) -> None:
        """RFC 3834: what stops an out-of-office reply bouncing back into the
        relay for every notification the platform sends."""
        composed = SmtpEmailProvider(host="localhost")._compose(message)
        assert composed["Auto-Submitted"] == "auto-generated"

    def test_a_non_ascii_display_name_is_encoded(self) -> None:
        """An Arabic or French name must render as itself rather than as
        mojibake."""
        composed = SmtpEmailProvider(host="localhost")._compose(
            OutgoingEmail(
                to_address="amina@firm.example",
                to_name="أمينة بنعلي",
                subject="مرحبًا",
                html_body="<p>x</p>",
                text_body="x",
                from_address="notifications@legal.example",
                from_name="منصة",
            )
        )
        assert composed["To"] is not None
        assert "amina@firm.example" in str(composed["To"])

    def test_a_reply_to_is_set_only_when_configured(self, message: OutgoingEmail) -> None:
        provider = SmtpEmailProvider(host="localhost")
        assert provider._compose(message)["Reply-To"] is None

        with_reply = provider._compose(
            OutgoingEmail(
                to_address=message.to_address,
                to_name=message.to_name,
                subject=message.subject,
                html_body=message.html_body,
                text_body=message.text_body,
                from_address=message.from_address,
                from_name=message.from_name,
                reply_to="support@legal.example",
            )
        )
        assert with_reply["Reply-To"] == "support@legal.example"


class TestHeaderInjection:
    def test_a_subject_carrying_a_line_break_is_refused(
        self, message: OutgoingEmail
    ) -> None:
        """Not composed and not sent: a CR ends the header and starts a new one,
        which is how an injected `Bcc:` reaches a message the platform wrote."""
        provider = SmtpEmailProvider(host="localhost")
        hostile = OutgoingEmail(
            to_address=message.to_address,
            to_name=message.to_name,
            subject="Dossier\r\nBcc: attacker@evil.example",
            html_body=message.html_body,
            text_body=message.text_body,
            from_address=message.from_address,
            from_name=message.from_name,
        )
        with pytest.raises(InvalidEmailRecipientError):
            provider._compose(hostile)

        result = provider.send(hostile)
        assert result.accepted is False
        assert result.failure is EmailFailureCode.INVALID_RECIPIENT

    def test_a_hostile_display_name_is_refused(self, message: OutgoingEmail) -> None:
        provider = SmtpEmailProvider(host="localhost")
        hostile = OutgoingEmail(
            to_address=message.to_address,
            to_name="Amina\nBcc: attacker@evil.example",
            subject=message.subject,
            html_body=message.html_body,
            text_body=message.text_body,
            from_address=message.from_address,
            from_name=message.from_name,
        )
        assert provider.send(hostile).failure is EmailFailureCode.INVALID_RECIPIENT

    def test_an_unusable_recipient_is_refused_before_a_connection(self) -> None:
        provider = SmtpEmailProvider(host="localhost")
        result = provider.send(
            OutgoingEmail(
                to_address="not-an-address",
                to_name="X",
                subject="s",
                html_body="<p>x</p>",
                text_body="x",
                from_address="notifications@legal.example",
                from_name="Legal Platform",
            )
        )
        assert result.failure is EmailFailureCode.INVALID_RECIPIENT


# --------------------------------------------------------------------------- #
# Failure translation
# --------------------------------------------------------------------------- #


class TestFailureTranslation:
    @pytest.mark.parametrize(
        ("exception", "expected"),
        [
            (
                smtplib.SMTPAuthenticationError(535, b"bad credentials"),
                EmailFailureCode.AUTHENTICATION_FAILED,
            ),
            (
                smtplib.SMTPRecipientsRefused({"a@b.example": (550, b"unknown")}),
                EmailFailureCode.RECIPIENT_REFUSED,
            ),
            (
                smtplib.SMTPSenderRefused(550, b"denied", "from@x.example"),
                EmailFailureCode.MESSAGE_REFUSED,
            ),
            (smtplib.SMTPResponseException(451, b"try later"), EmailFailureCode.THROTTLED),
            (
                smtplib.SMTPResponseException(552, b"too large"),
                EmailFailureCode.MESSAGE_REFUSED,
            ),
            (smtplib.SMTPServerDisconnected("gone"), EmailFailureCode.CONNECTION_FAILED),
            (smtplib.SMTPConnectError(421, b"busy"), EmailFailureCode.CONNECTION_FAILED),
            (TimeoutError(), EmailFailureCode.TIMEOUT),
            (TimeoutError(), EmailFailureCode.TIMEOUT),
            (ssl.SSLError("handshake"), EmailFailureCode.CONNECTION_FAILED),
            (ConnectionRefusedError(), EmailFailureCode.CONNECTION_FAILED),
            (ValueError("something else"), EmailFailureCode.UNKNOWN),
        ],
    )
    def test_every_library_failure_becomes_a_platform_code(
        self, exception: Exception, expected: EmailFailureCode
    ) -> None:
        assert SmtpEmailProvider._classify(exception) is expected

    def test_a_4xx_is_retried_and_a_5xx_is_not(self) -> None:
        """The whole transient/permanent decision for SMTP turns on the response
        class: retrying a 5xx is how a platform gets a relay to stop accepting its
        mail altogether."""
        assert is_transient(
            SmtpEmailProvider._classify(smtplib.SMTPResponseException(450, b"greylisted"))
        )
        assert not is_transient(
            SmtpEmailProvider._classify(smtplib.SMTPResponseException(553, b"no"))
        )

    def test_a_send_failure_is_a_result_rather_than_an_exception(
        self, message: OutgoingEmail, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused message is an ordinary, recordable outcome of a background
        job — the caller records it on a row rather than catching it."""
        provider = SmtpEmailProvider(host="localhost")

        def explode(*_args: Any, **_kwargs: Any) -> None:
            raise smtplib.SMTPServerDisconnected("gone")

        monkeypatch.setattr(provider, "_deliver", explode)
        result = provider.send(message)
        assert result.accepted is False
        assert result.failure is EmailFailureCode.CONNECTION_FAILED

    def test_a_send_records_its_duration(
        self, message: OutgoingEmail, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = SmtpEmailProvider(host="localhost")
        monkeypatch.setattr(provider, "_deliver", lambda *a, **k: None)
        result = provider.send(message)
        assert result.accepted is True
        assert result.provider == "smtp"
        assert result.duration_ms >= 0.0


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #


class TestAvailability:
    def test_no_host_means_unavailable(self) -> None:
        """Handled rather than fatal, exactly as a missing Tesseract and a missing
        LLM_API_KEY are."""
        assert SmtpEmailProvider(host=None).is_available() is False

    def test_a_configured_host_is_available_without_connecting(self) -> None:
        """A *configuration* check: it is consulted before every dispatch, and a
        network round trip per notification is a cost the feature cannot justify."""
        assert SmtpEmailProvider(host="mail.example").is_available() is True

    def test_an_unavailable_provider_refuses_rather_than_raising(
        self, message: OutgoingEmail
    ) -> None:
        result = SmtpEmailProvider(host=None).send(message)
        assert result.failure is EmailFailureCode.PROVIDER_UNAVAILABLE
        assert is_transient(EmailFailureCode.PROVIDER_UNAVAILABLE)


# --------------------------------------------------------------------------- #
# The null provider and the registry
# --------------------------------------------------------------------------- #


class TestNullProvider:
    def test_it_accepts_and_records(self, message: OutgoingEmail) -> None:
        provider = NullEmailProvider()
        assert provider.send(message).accepted is True
        assert provider.sent == [message]

    def test_it_reports_itself_available(self) -> None:
        """Deliberately: one that claimed otherwise would make the delivery
        service skip dispatch, and a test asserting on delivery rows would then be
        asserting on nothing."""
        assert NullEmailProvider().is_available() is True


class TestRegistry:
    def test_both_providers_are_resolvable(self) -> None:
        assert set(available_email_providers()) == {"smtp", "null"}
        assert set(EMAIL_PROVIDER_FACTORIES) == {"smtp", "null"}

    def test_an_unknown_identifier_falls_back_rather_than_failing(self) -> None:
        """An API that refused to come up over a mail setting would take
        authentication, cases, and documents down with it."""
        reset_email_provider_cache()
        assert get_email_provider("resend-someday").name == "smtp"

    def test_the_provider_is_shared(self) -> None:
        reset_email_provider_cache()
        assert get_email_provider("null") is get_email_provider("null")

    def test_the_cache_can_be_cleared(self) -> None:
        first = get_email_provider("null")
        reset_email_provider_cache()
        assert get_email_provider("null") is not first


class TestSendResult:
    def test_success_and_refusal_are_distinguishable(self) -> None:
        ok = EmailSendResult.success(provider="smtp", duration_ms=12.0)
        bad = EmailSendResult.refused(provider="smtp", failure=EmailFailureCode.TIMEOUT)
        assert (ok.accepted, ok.failure) == (True, None)
        assert (bad.accepted, bad.failure) == (False, EmailFailureCode.TIMEOUT)
