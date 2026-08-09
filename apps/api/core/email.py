"""The email channel's vocabulary, and the rules that decide what travels on it.

``17-email-delivery-channel.md`` is unusually explicit about what this feature is
*not*:

    It must not decide which application events deserve email delivery. Business
    modules and the Notification Service remain responsible for deciding when a
    notification should be created. The Email Delivery Channel only delivers
    notifications that have already been marked for email delivery.

This module is where "marked for email delivery" is written down, and the shape
of :data:`EMAIL_RULES` is what keeps that boundary honest: it is keyed by
**notification rule**, not by domain event, and every key is taken from a
:class:`~core.notifications.NotificationRule` that already exists. There is no
way to express "email this event" here, because an event is not something this
module can name — the only input it has is a notification the Notification
Service has already decided to create, for a person it has already decided is
entitled to it.

Nothing here touches a database, a socket, an SMTP connection, or a template
file. It is pure data plus five derivations, and each is a requirement of the
spec made mechanical:

* **which notifications become emails** — :data:`EMAIL_RULES`, and the seven
  entries in it are exactly the "Supported Email Types" list. Everything under
  the spec's *"Events That Must NOT Generate Emails"* is absent, and its absence
  is enforced by a test rather than left to review;
* **what a delivery may do next** — :data:`STATUS_TRANSITIONS`, the same shape
  :data:`~core.reports.STATUS_TRANSITIONS` has;
* **which failures are worth retrying** — :class:`EmailFailureCode` and
  :data:`TRANSIENT_FAILURE_CODES`. *"Automatically retry temporary failures […]
  permanent failures should be logged"* is a partition of a closed vocabulary
  rather than a judgement made at a call site;
* **how long to wait before the next attempt** — :func:`retry_delay_seconds`,
  exponential with a ceiling, as ``code-standards.md``'s Background Jobs section
  requires;
* **what a template is allowed to interpolate** — :func:`build_email_context`,
  which is built from the notification's own rendered wording and its bounded
  context and **nothing else**. That is what makes the spec's *"email templates
  should never expose information the recipient is not authorized to receive"*
  structural: the body of an email is, by construction, the notification the
  recipient can already open in the application.

**The wording is not restated here.** The subject is the notification's rendered
title and the body's lead paragraph is its rendered message, both produced by
:func:`~core.notifications.render_notification` in the recipient's language —
which is what ``code-standards.md``'s *"notification logic must never be
duplicated"* asks for across channels, and what
:mod:`models.notification` predicted when it chose to store no prose.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from core.events import DomainEventType
from core.indexing import LANGUAGE_ARABIC, LANGUAGE_ENGLISH, LANGUAGE_FRENCH
from core.notifications import (
    ANNOUNCEMENT_RULES,
    EVENT_RULES,
    RULE_CASE_ASSIGNED,
    RULE_HEARING_SCHEDULED,
    RULE_HEARING_UPDATED,
    AnnouncementKind,
    NotificationTargetType,
    render_notification,
    resolve_notification_language,
)
from models.email import EmailDeliveryStatus

# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

#: Which moves a delivery may make.
#:
#: Read-only at runtime, and the same shape :data:`~core.reports.STATUS_TRANSITIONS`
#: has. Two entries are worth their explanation:
#:
#: * ``SENDING → PENDING`` is the **retry**: a transient failure returns the row
#:   to the queue with a backoff rather than ending it, which is what makes
#:   ``pending`` mean "waiting for a worker *or* waiting out a delay" rather than
#:   needing a fifth state;
#: * ``FAILED → PENDING`` is the **recovery**: a delivery that exhausted its
#:   attempts against an SMTP server that was down for an hour is not wrong, it is
#:   stale, and an operator who fixes the relay should be able to re-queue it. It
#:   is deliberately not reachable from ``SENT`` — re-sending a message somebody
#:   has already received is the one mistake this feature must not make.
STATUS_TRANSITIONS: Mapping[EmailDeliveryStatus, frozenset[EmailDeliveryStatus]] = (
    MappingProxyType(
        {
            EmailDeliveryStatus.PENDING: frozenset(
                {EmailDeliveryStatus.SENDING, EmailDeliveryStatus.FAILED}
            ),
            EmailDeliveryStatus.SENDING: frozenset(
                {
                    EmailDeliveryStatus.SENT,
                    EmailDeliveryStatus.FAILED,
                    EmailDeliveryStatus.PENDING,
                }
            ),
            EmailDeliveryStatus.SENT: frozenset(),
            EmailDeliveryStatus.FAILED: frozenset({EmailDeliveryStatus.PENDING}),
        }
    )
)


def can_transition(current: EmailDeliveryStatus, target: EmailDeliveryStatus) -> bool:
    """Whether a delivery may move from ``current`` to ``target``."""
    return target in STATUS_TRANSITIONS.get(current, frozenset())


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class EmailFailureCode(StrEnum):
    """Why one delivery attempt did not succeed.

    A closed vocabulary rather than a provider's message, for two reasons that
    are both stronger here than anywhere else this pattern is used
    (:class:`~core.ocr.OcrFailureCode`, :class:`~core.rag.RagFailureCode`):

    * these are **grouped in a monitoring view**, and an SMTP server's rejection
      text interpolated into a metric produces one bucket per occurrence;
    * an SMTP rejection **echoes the envelope** — ``550 5.1.1 <someone@firm.example>
      user unknown`` — so storing or logging the provider's own words would put a
      recipient's address into a diagnostic the spec's Logging section keeps it
      out of.

    Every member is produced at the provider boundary
    (:mod:`services.email_provider`), which is the only module that imports an
    SMTP library — so the service records a *cause* without knowing what an
    ``SMTPException`` is.
    """

    #: No provider is configured, or the one that is cannot be reached at all.
    #: Transient: an operator fixing the configuration should not have cost the
    #: platform every email queued while it was wrong.
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    #: The connection could not be established or was dropped mid-conversation.
    CONNECTION_FAILED = "connection_failed"
    #: The provider did not answer inside ``SMTP_TIMEOUT_SECONDS``.
    TIMEOUT = "timeout"
    #: The provider refused the credentials. **Permanent**: retrying a rejected
    #: password is how an account gets locked, and no amount of waiting fixes it.
    AUTHENTICATION_FAILED = "authentication_failed"
    #: The provider refused this recipient — an unknown mailbox, a blocked
    #: address. Permanent for this delivery.
    RECIPIENT_REFUSED = "recipient_refused"
    #: The address on the account is missing or not a usable address. Permanent,
    #: and caught before the provider is ever opened.
    INVALID_RECIPIENT = "invalid_recipient"
    #: The provider refused the message itself — too large, rejected content.
    #: Permanent: the same bytes will be refused again.
    MESSAGE_REFUSED = "message_refused"
    #: The provider asked to be tried again later: a greylist, a rate limit, a
    #: temporary local error (an SMTP 4xx). The textbook transient failure.
    THROTTLED = "throttled"
    #: The template could not be rendered. Permanent, and a **deployment fault**
    #: rather than a delivery one — it is recorded on the row so it is visible
    #: instead of being a silent gap in somebody's mail.
    TEMPLATE_FAILURE = "template_failure"
    #: Anything the provider boundary did not anticipate. Treated as transient,
    #: because the alternative is discarding mail over an unrecognised error.
    UNKNOWN = "unknown"


#: Failures worth trying again.
#:
#: ``17-email-delivery-channel.md`` names *"timeout, temporary SMTP failure,
#: network interruption"* and asks that permanent failures be logged rather than
#: retried. This set is that sentence, and its **complement is the interesting
#: half**: a rejected credential, an unknown mailbox, an oversized message, and a
#: broken template are all things that will fail identically on every attempt, so
#: retrying them spends the platform's time and the provider's patience to reach
#: the same outcome more slowly.
TRANSIENT_FAILURE_CODES: frozenset[EmailFailureCode] = frozenset(
    {
        EmailFailureCode.PROVIDER_UNAVAILABLE,
        EmailFailureCode.CONNECTION_FAILED,
        EmailFailureCode.TIMEOUT,
        EmailFailureCode.THROTTLED,
        EmailFailureCode.UNKNOWN,
    }
)


def is_transient(code: EmailFailureCode) -> bool:
    """Whether a failure is worth another attempt."""
    return code in TRANSIENT_FAILURE_CODES


def failure_from_value(value: str | None) -> EmailFailureCode | None:
    """Read a stored failure code, tolerating one this version does not define.

    ``None`` rather than an exception, for the reason
    :func:`~core.notifications.preference_from_value` returns one: the column is
    an open registry, and a code written by a later version of the platform must
    not make an earlier one unable to read a delivery record.
    """
    if not value:
        return None
    try:
        return EmailFailureCode(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Retry schedule
# --------------------------------------------------------------------------- #


def retry_delay_seconds(attempt: int, *, base: float, cap: float) -> float:
    """How long to wait before attempt number ``attempt + 1``.

    Exponential backoff with a ceiling — ``code-standards.md``: *"Retry failed
    jobs using exponential backoff"* — and deliberately **deterministic**, with no
    jitter. Jitter exists to stop a fleet of clients retrying in lockstep against
    one server; this platform runs one email worker pool of two threads against
    one relay, so what jitter would actually buy is a retry schedule no test can
    assert on.

    The ceiling matters more than the base does: without it, a tenth attempt at a
    base of thirty seconds is four and a half hours, which is long enough that a
    hearing reminder arrives after the hearing.

    Args:
        attempt: attempts already made. ``0`` is "the first one has just failed".
        base: delay after the first failure, in seconds.
        cap: longest delay this schedule will ever produce, in seconds.

    Returns:
        Seconds to wait, never negative and never above ``cap``.
    """
    if attempt <= 0:
        return max(0.0, min(base, cap))
    # ``2 ** attempt`` on a large attempt count is an integer with a lot of
    # digits, not an overflow — but it is pointless work, so the exponent is
    # clamped to the first value that certainly exceeds any sane cap.
    exponent = min(attempt, 24)
    return max(0.0, min(base * (2.0**exponent), cap))


def next_attempt_at(attempt: int, *, base: float, cap: float, now: datetime | None = None) -> datetime:
    """When a delivery becomes eligible for its next attempt."""
    from datetime import timedelta

    reference = now or datetime.now(UTC)
    return reference + timedelta(seconds=retry_delay_seconds(attempt, base=base, cap=cap))


# --------------------------------------------------------------------------- #
# Which notifications travel by email
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EmailRule:
    """One notification rule's email delivery, if it has one.

    Deliberately tiny. It carries **no wording, no audience, no priority, and no
    preference key** — every one of those already lives on the
    :class:`~core.notifications.NotificationRule` this is keyed by, and a second
    copy here would be a second place for them to disagree. What is left is the
    only decision this feature actually owns: *which template renders it*.
    """

    #: The :attr:`~core.notifications.NotificationRule.key` this applies to.
    rule_key: str
    #: Template name under ``apps/api/emails/`` ("notification", "security").
    template: str
    #: Which version of that template. Pinned per rule rather than globally, so a
    #: new version of the security template can ship without re-rendering every
    #: case assignment through it.
    version: int = 1


#: The general-purpose template: a heading, a sentence, and — when the
#: notification names a resource — a link to it.
TEMPLATE_NOTIFICATION: Final[str] = "notification"

#: The account-security template. A separate file rather than a flag on the one
#: above, because a security email is genuinely a different document: it offers
#: no "open the case" button (there is nothing to open, and a link in an email
#: about a password is exactly the shape of a phishing message), and it carries
#: the "if you did not do this, report it" line that makes the notification
#: actionable.
TEMPLATE_SECURITY: Final[str] = "security"


def _rule_key(event_type: DomainEventType) -> str:
    """The notification rule key one event produces, resolved at import.

    Looked up rather than written as a string literal, so a rule renamed or
    withdrawn in :mod:`core.notifications` fails **here, at import**, rather than
    becoming an entry in :data:`EMAIL_RULES` that silently matches nothing and
    quietly stops a class of email.
    """
    return EVENT_RULES[event_type].key


#: Which notifications are delivered by email, and what renders each one.
#:
#: **This is the whole of "marked for email delivery"**, and it is exactly the
#: spec's "Supported Email Types" list:
#:
#: * *Authentication* — password reset and account activation;
#: * *Case Management* — case assigned;
#: * *Court* — hearing rescheduled and hearing awaited;
#: * *Reports* — AI report ready;
#: * *System* — critical announcements.
#:
#: What is **absent** matters at least as much, and each omission is a decision:
#:
#: * every ``document.*``, every ``ocr.*``, every ``indexing.*``, the assistant's
#:   answers, timeline updates, document views, and report opens — the spec's
#:   *"Events That Must NOT Generate Emails"* list, in full. They remain in-app
#:   notifications, and this module cannot override that because it can only
#:   *narrow* what the Notification Service already created;
#: * ``system.announcement`` — the ordinary, ``NORMAL``-priority announcement.
#:   The spec asks for *"Critical System Announcement"*, and the platform's
#:   critical announcement is ``system.maintenance``: a ``HIGH``-priority warning
#:   that asks the reader to finish and save before a deadline they might
#:   otherwise miss. Emailing every routine announcement to every account is how
#:   a platform's mail starts being filtered;
#: * ``case.created``, ``case.updated``, ``case.status_changed``,
#:   ``case.priority_changed``, ``case.archived``, ``case.restored``,
#:   ``case.unassigned``, ``ocr.failed``, ``report.failed``, and
#:   ``user.role_changed`` — all real notifications, none of them on the spec's
#:   list. Adding one is **one entry here**, which is what *"support future email
#:   types without redesign"* means concretely;
#: * **"Password Changed"**, which the spec names and the platform does not yet
#:   produce. ``AuthService.change_password`` publishes no domain event, so there
#:   is no notification to deliver — and creating one would be *notification
#:   policy*, which this spec's "Out of Scope" section explicitly forbids this
#:   feature from touching. When that rule is added by the Notifications module,
#:   the email side is one line here and nothing else.
EMAIL_RULES: Mapping[str, EmailRule] = MappingProxyType(
    {
        rule.rule_key: rule
        for rule in (
            # --- Authentication ------------------------------------------- #
            EmailRule(
                _rule_key(DomainEventType.USER_PASSWORD_RESET), template=TEMPLATE_SECURITY
            ),
            EmailRule(_rule_key(DomainEventType.USER_ACTIVATED), template=TEMPLATE_SECURITY),
            # --- Case management ------------------------------------------- #
            EmailRule(RULE_CASE_ASSIGNED.key, template=TEMPLATE_NOTIFICATION),
            # --- Court ------------------------------------------------------ #
            EmailRule(RULE_HEARING_UPDATED.key, template=TEMPLATE_NOTIFICATION),
            EmailRule(RULE_HEARING_SCHEDULED.key, template=TEMPLATE_NOTIFICATION),
            # --- Reports ---------------------------------------------------- #
            EmailRule(_rule_key(DomainEventType.REPORT_GENERATED), template=TEMPLATE_NOTIFICATION),
            # --- System ----------------------------------------------------- #
            EmailRule(
                ANNOUNCEMENT_RULES[AnnouncementKind.MAINTENANCE].key,
                template=TEMPLATE_NOTIFICATION,
            ),
        )
    }
)


def email_rule_for(rule_key: str) -> EmailRule | None:
    """The email delivery for one notification rule, or ``None`` if it has none.

    ``None`` is the ordinary answer for most of the platform's notifications and
    is never an error: the in-app feed carries everything, and this channel
    carries the subset the spec named.
    """
    return EMAIL_RULES.get(rule_key)


def is_email_eligible(rule_key: str) -> bool:
    """Whether notifications produced by this rule are delivered by email."""
    return rule_key in EMAIL_RULES


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #

#: Longest address the platform will hand to a provider. Matches
#: ``users.email``'s column width, which is the RFC 5321 maximum.
MAX_EMAIL_LENGTH: Final[int] = 320

#: A deliberately conservative syntactic check.
#:
#: **Not** a full RFC 5322 grammar, and not trying to be: an address reaching
#: this point was already validated by ``EmailStr`` when the account was created,
#: so what is being caught here is a *stored value that has become unusable* — a
#: blank column, a value edited directly in the database, an address carrying the
#: control characters :func:`sanitize_header_value` exists to refuse. A permissive
#: pattern that accepts a real address and rejects an obviously broken one is
#: exactly the right strictness for that job; a stricter one would refuse valid
#: addresses the platform already accepted, which is a bug that looks like
#: security.
_ADDRESS_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

#: Characters that must never reach a message header.
#:
#: A carriage return or a line feed in an address or a display name **ends the
#: header and starts a new one**, which is how an injected ``Bcc:`` gets into a
#: message the platform composed. Every value that reaches a header goes through
#: :func:`sanitize_header_value` first, and a value carrying one of these is
#: **refused rather than stripped** — a name silently rewritten is a name that was
#: attacked and nobody noticed.
_HEADER_FORBIDDEN = re.compile(r"[\r\n\x00]")


class InvalidEmailRecipientError(ValueError):
    """An address cannot be used as an envelope recipient.

    A plain ``ValueError`` rather than an :class:`~core.exceptions.AppException`,
    exactly as :class:`~core.events.InvalidTopicError` is: this module is pure
    vocabulary and has no HTTP opinion. The delivery service turns it into an
    :attr:`EmailFailureCode.INVALID_RECIPIENT` on the row.
    """


def normalize_email(value: str | None) -> str | None:
    """Return a usable envelope address, or ``None``.

    Trimmed and lower-cased, matching how ``users.email`` is stored, so the
    address on a delivery row and the address on the account are comparable.
    """
    if not value:
        return None

    candidate = value.strip()
    if not candidate or len(candidate) > MAX_EMAIL_LENGTH:
        return None
    if _HEADER_FORBIDDEN.search(candidate):
        return None
    if not _ADDRESS_PATTERN.match(candidate):
        return None
    return candidate.lower()


def sanitize_header_value(value: str, *, limit: int = 200) -> str:
    """Return ``value`` if it is safe to place in a message header.

    Raises:
        InvalidEmailRecipientError: the value carries a line break or a NUL. See
            :data:`_HEADER_FORBIDDEN` for why this refuses rather than strips.
    """
    if _HEADER_FORBIDDEN.search(value):
        raise InvalidEmailRecipientError("Header values may not contain line breaks.")
    return value[:limit]


# --------------------------------------------------------------------------- #
# Language and direction
# --------------------------------------------------------------------------- #

#: Text direction per language, for the ``dir`` attribute of the HTML template.
#:
#: ``ui-context.md`` and ``ai-workflow-rules.md`` both require RTL for Arabic, and
#: an email is the one surface where the application's own stylesheet is not
#: available — a mail client renders the markup it is given, so the direction has
#: to travel *in* the document.
TEXT_DIRECTIONS: Mapping[str, str] = MappingProxyType(
    {LANGUAGE_ARABIC: "rtl", LANGUAGE_FRENCH: "ltr", LANGUAGE_ENGLISH: "ltr"}
)


def text_direction(language: str) -> str:
    """``"rtl"`` for Arabic, ``"ltr"`` otherwise."""
    return TEXT_DIRECTIONS.get(language, "ltr")


def resolve_email_language(requested: str | None) -> str:
    """Decide which language one email is written in.

    Delegates to :func:`~core.notifications.resolve_notification_language`, so an
    email and the in-app notification it carries can never be rendered by two
    different resolvers.

    **There is no per-user language column yet**, so ``requested`` is the
    deployment's ``EMAIL_DEFAULT_LANGUAGE`` today rather than the recipient's own
    choice. ``architecture.md`` lists *Language Preferences* under PostgreSQL and
    nothing has built them; when they arrive, the one change here is the caller
    passing the user's value instead of the setting's.
    """
    return resolve_notification_language(requested)


# --------------------------------------------------------------------------- #
# What a template may interpolate
# --------------------------------------------------------------------------- #

#: Where each kind of notification target lives in the web application.
#:
#: **The one place on this platform that builds a URL from a notification
#: target**, and the exception is forced rather than chosen.
#: ``16-notifications.md`` requires that navigation *"remain independent from
#: frontend routing implementation"*, and the in-app feed honours that literally
#: — a notification names a ``case``/``<uuid>`` pair and the client maps it onto
#: its own route. An email has **no client**: it is read in a mail application
#: that will never know what a notification target is, so somebody has to turn
#: the pair into an address, and the honest place for that is one table in the
#: channel that needs it.
#:
#: ``report`` deliberately points at the **list** rather than at a report: the web
#: application has no report-detail route yet, and a deep link to a page that does
#: not exist is worse than a link to the page that does. It becomes a deep link
#: the day that route ships, and this is the only line that changes.
TARGET_PATHS: Mapping[NotificationTargetType, str] = MappingProxyType(
    {
        NotificationTargetType.CASE: "/cases/{id}",
        NotificationTargetType.DOCUMENT: "/documents",
        NotificationTargetType.REPORT: "/reports",
        NotificationTargetType.ACCOUNT: "/settings",
    }
)

#: Where a notification with no target sends its reader: their own feed, which
#: always exists and always contains the item the email is about.
FEED_PATH: Final[str] = "/notifications"


def target_url(
    base_url: str | None,
    *,
    target_type: str | None,
    target_id: uuid.UUID | None,
) -> str | None:
    """Build the link an email offers, or ``None`` when it should offer none.

    ``None`` when no base URL is configured, deliberately: an email carrying a
    ``/cases/…`` path with no host is a broken link, and a broken link in a legal
    notification is worse than a message that simply says what happened. A
    deployment that sets ``EMAIL_BASE_URL`` gets buttons; one that does not gets
    correct, linkless mail.
    """
    if not base_url:
        return None

    root = base_url.strip().rstrip("/")
    if not root:
        return None

    resolved = _target_type(target_type)
    if resolved is None:
        return f"{root}{FEED_PATH}"

    path = TARGET_PATHS[resolved]
    if "{id}" in path:
        if target_id is None:
            return f"{root}{FEED_PATH}"
        path = path.format(id=target_id)

    return f"{root}{path}"


def _target_type(value: str | None) -> NotificationTargetType | None:
    """Read a stored target type, tolerating one this version does not define."""
    if not value:
        return None
    try:
        return NotificationTargetType(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# The chrome around a notification
# --------------------------------------------------------------------------- #
#
# An email is not only the notification it carries: it opens with a greeting,
# offers a button, and closes with a line saying why it arrived and how to stop
# it. That wording is **user-facing text**, and `ai-workflow-rules.md` is
# unambiguous about it — *"no hardcoded UI strings, translation keys only, French
# and Arabic"*.
#
# It lives **here rather than inside the templates**, and that is what makes the
# spec's *"templates should remain independent from application logic"* true in
# both directions: the `.j2` files carry no `{% if language == 'ar' %}` branch
# and no sentence of their own, so they are structure and styling, while every
# word the platform says is in a module the rest of the platform can read. It is
# also the same shape `core/notifications.py` already uses for the notifications
# themselves, which is what stops this feature becoming a second vocabulary.


@dataclass(frozen=True, slots=True)
class EmailChrome:
    """The wording around a notification, in one language."""

    #: Interpolates ``{name}``.
    greeting: str
    #: The call-to-action button's label.
    action_label: str
    #: Why this email arrived, and where to change that.
    footer: str
    #: That nobody reads replies to this address.
    no_reply: str
    #: What to do if the reader did not expect an account-security email. Only
    #: the security template uses it; it is on every language's entry so the two
    #: templates share one type.
    security_note: str


def _chrome(*, fr: EmailChrome, ar: EmailChrome, en: EmailChrome) -> Mapping[str, EmailChrome]:
    """Build the per-language table, keyed the way every other one here is."""
    return MappingProxyType(
        {LANGUAGE_FRENCH: fr, LANGUAGE_ARABIC: ar, LANGUAGE_ENGLISH: en}
    )


#: The email chrome, per language. French is the fallback, matching
#: :meth:`~core.notifications.NotificationTemplate.title` and for the same reason.
EMAIL_CHROME: Mapping[str, EmailChrome] = _chrome(
    fr=EmailChrome(
        greeting="Bonjour {name},",
        action_label="Ouvrir dans la plateforme",
        footer=(
            "Vous recevez cet e-mail parce que les notifications par e-mail sont activées "
            "sur votre compte. Vous pouvez les désactiver dans vos paramètres."
        ),
        no_reply="Cet e-mail a été envoyé automatiquement. Merci de ne pas y répondre.",
        security_note=(
            "Si vous n'êtes pas à l'origine de cette action, contactez immédiatement "
            "votre administrateur."
        ),
    ),
    ar=EmailChrome(
        greeting="مرحبًا {name}،",
        action_label="افتح في المنصة",
        footer=(
            "تصلك هذه الرسالة لأن الإشعارات عبر البريد الإلكتروني مُفعّلة في حسابك. "
            "يمكنك تعطيلها من الإعدادات."
        ),
        no_reply="أُرسلت هذه الرسالة تلقائيًا. يرجى عدم الرد عليها.",
        security_note="إذا لم تكن أنت من قام بهذا الإجراء، فاتصل بالمسؤول فورًا.",
    ),
    en=EmailChrome(
        greeting="Hello {name},",
        action_label="Open in the platform",
        footer=(
            "You are receiving this because email notifications are switched on for your "
            "account. You can turn them off in your settings."
        ),
        no_reply="This message was sent automatically. Please do not reply to it.",
        security_note=(
            "If you did not do this, contact your administrator immediately."
        ),
    ),
)


def chrome_for(language: str, *, recipient_name: str) -> Mapping[str, str]:
    """The chrome one email renders with, greeting already interpolated."""
    chrome = EMAIL_CHROME.get(language) or EMAIL_CHROME[LANGUAGE_FRENCH]
    return MappingProxyType(
        {
            "greeting": chrome.greeting.format(name=recipient_name),
            "action_label": chrome.action_label,
            "footer": chrome.footer,
            "no_reply": chrome.no_reply,
            "security_note": chrome.security_note,
        }
    )


@dataclass(frozen=True, slots=True)
class EmailContext:
    """Everything a template is allowed to see.

    A frozen value rather than a loose dictionary, because the security property
    this feature has to hold is a statement about *this list*: an email's body is
    the notification's own rendered wording, the handful of scalars its sentence
    already interpolates, and presentation. There is no case, no document, no
    passage, no report section, and no user record here — so
    ``17-email-delivery-channel.md``'s *"email templates should never expose
    information the recipient is not authorized to receive"* is true because there
    is nothing in scope that could expose it.
    """

    #: The notification's rendered heading. Also the message's subject.
    title: str
    #: The notification's rendered sentence.
    message: str
    #: Who it is addressed to, for the greeting. A display name, never an address.
    recipient_name: str
    #: ISO 639-1 code and the direction that follows from it.
    language: str
    direction: str
    #: Which area of the platform the news came from, and how urgent it is. Used
    #: for presentation only — a colour and a label — never for a decision.
    category: str
    priority: str
    #: Where to read it in the application, when there is somewhere to send them.
    action_url: str | None
    #: The values the notification's own sentence interpolates — a case number, a
    #: page count, a failure code. Already bounded and screened by
    #: :func:`~core.notifications.normalize_context`, which is why they can be
    #: passed through without a second screen.
    values: Mapping[str, Any]
    #: The platform's own name, for the signature. Configuration, not content.
    platform_name: str
    #: The localized wording around the notification — see :data:`EMAIL_CHROME`.
    chrome: Mapping[str, str]

    def as_mapping(self) -> dict[str, Any]:
        """The template's variables, as Jinja receives them."""
        return {
            "title": self.title,
            "message": self.message,
            "recipient_name": self.recipient_name,
            "language": self.language,
            "direction": self.direction,
            "category": self.category,
            "priority": self.priority,
            "action_url": self.action_url,
            "values": dict(self.values),
            "platform_name": self.platform_name,
            "chrome": dict(self.chrome),
        }


def build_email_context(
    *,
    rule_key: str,
    category: str,
    priority: str,
    context: Mapping[str, Any] | None,
    recipient_name: str,
    language: str,
    base_url: str | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    platform_name: str,
) -> EmailContext:
    """Assemble what one email's template renders from.

    The title and the message come from
    :func:`~core.notifications.render_notification` — the **same** function the
    in-app feed renders from, called with the same rule key and the same stored
    context. Two consequences, and both are requirements rather than
    conveniences: the email and the feed can never say different things about the
    same event, and an Arabic recipient's email is Arabic for the same reason
    their history is.
    """
    rendered = render_notification(
        rule_key=rule_key, category=category, context=context, language=language
    )

    return EmailContext(
        title=rendered.title,
        message=rendered.message,
        recipient_name=recipient_name,
        language=language,
        direction=text_direction(language),
        category=category,
        priority=priority,
        action_url=target_url(base_url, target_type=target_type, target_id=target_id),
        values=dict(context or {}),
        platform_name=platform_name,
        chrome=chrome_for(language, recipient_name=recipient_name),
    )


def subject_for(title: str, *, prefix: str | None = None) -> str:
    """The message's ``Subject`` header.

    The notification's own title, optionally prefixed — which is what a
    deployment running several environments against one mailbox needs so a test
    message is distinguishable from a real one.

    Raises:
        InvalidEmailRecipientError: the title or prefix carries a line break. It
            cannot in practice — a title is rendered from a template in
            :mod:`core.notifications` and bounded by ``MAX_TITLE_LENGTH`` — and
            the check is here because *"a bound that is only ever reached by a bug
            is exactly the bound worth having"*.
    """
    heading = sanitize_header_value(title)
    if not prefix:
        return heading
    return f"{sanitize_header_value(prefix, limit=40)} {heading}".strip()


__all__ = [
    "EMAIL_CHROME",
    "EMAIL_RULES",
    "FEED_PATH",
    "MAX_EMAIL_LENGTH",
    "STATUS_TRANSITIONS",
    "TARGET_PATHS",
    "TEMPLATE_NOTIFICATION",
    "TEMPLATE_SECURITY",
    "TEXT_DIRECTIONS",
    "TRANSIENT_FAILURE_CODES",
    "EmailChrome",
    "EmailContext",
    "EmailDeliveryStatus",
    "EmailFailureCode",
    "EmailRule",
    "InvalidEmailRecipientError",
    "build_email_context",
    "can_transition",
    "chrome_for",
    "email_rule_for",
    "failure_from_value",
    "is_email_eligible",
    "is_transient",
    "next_attempt_at",
    "normalize_email",
    "resolve_email_language",
    "retry_delay_seconds",
    "sanitize_header_value",
    "subject_for",
    "target_url",
    "text_direction",
]
