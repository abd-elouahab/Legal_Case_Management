"""The WhatsApp channel's vocabulary, and the rules that decide what travels on it.

``18-whatsapp-delivery-channel.md`` is unusually explicit about what this feature
is *not*:

    It must never decide which business events should generate WhatsApp messages.
    Business modules remain responsible for publishing domain events. The
    Notification Service remains responsible for creating notifications. The
    WhatsApp Delivery Channel only delivers notifications that have already been
    marked for WhatsApp delivery.

This module is where "marked for WhatsApp delivery" is written down, and the
shape of :data:`WHATSAPP_RULES` is what keeps that boundary honest: it is keyed
by **notification rule**, not by domain event, and every key is taken from a
:class:`~core.notifications.NotificationRule` that already exists. There is no
way to express "message this event" here, because an event is not something this
module can name — the only input it has is a notification the Notification
Service has already decided to create, for a person it has already decided is
entitled to it.

Nothing here touches a database, a socket, an HTTP client, or a template file. It
is pure data plus six derivations, and each is a requirement of the spec made
mechanical:

* **which notifications become messages** — :data:`WHATSAPP_RULES`, and the
  entries in it are exactly the "Supported Notification Types" list. Everything
  under the spec's *"Events That Must NOT Generate WhatsApp Messages"* is absent,
  and its absence is enforced by a test rather than left to review;
* **what a delivery may do next** — :data:`STATUS_TRANSITIONS`, the same shape
  :data:`~core.email.STATUS_TRANSITIONS` has;
* **which failures are worth retrying** — :class:`WhatsAppFailureCode` and
  :data:`TRANSIENT_FAILURE_CODES`. *"Automatically retry temporary failures […]
  permanent failures should be logged"* is a partition of a closed vocabulary
  rather than a judgement made at a call site;
* **how long to wait before the next attempt** — :func:`retry_delay_seconds`,
  exponential with a ceiling, as ``code-standards.md``'s Background Jobs section
  requires;
* **who may be written to** — :func:`normalize_phone`, which turns a stored
  ``users.phone`` into the E.164 digits the Cloud API takes, or refuses it;
* **what a template is allowed to interpolate** — :func:`build_whatsapp_context`,
  built from the notification's own rendered wording and its bounded context and
  **nothing else**. That is what makes the spec's *"messages must never expose
  information the recipient is not authorized to receive"* structural: the body
  of a message is, by construction, the notification the recipient can already
  open in the application.

**The wording is not restated here.** A WhatsApp *template* is approved and
stored on the provider's side, and the platform supplies its **parameters** — so
the temptation is to write the sentences into the approved template and let the
two drift. This channel deliberately does not: the parameters it sends are the
notification's own rendered title and message, produced by
:func:`~core.notifications.render_notification` in the reader's language, and the
approved template is a thin envelope around them. That is what
``code-standards.md``'s *"notification logic must never be duplicated"* asks for
across channels, and it is what makes a French reader's WhatsApp message and
their in-app feed say the same thing for the same reason.
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
from models.whatsapp import WhatsAppDeliveryStatus

# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

#: Which moves a delivery may make.
#:
#: Read-only at runtime, and the same shape :data:`~core.email.STATUS_TRANSITIONS`
#: has. Two entries carry their reasoning over unchanged:
#:
#: * ``SENDING → PENDING`` is the **retry**: a transient failure returns the row
#:   to the queue with a backoff rather than ending it, which is what makes
#:   ``pending`` mean "waiting for a worker *or* waiting out a delay" rather than
#:   needing a fifth state;
#: * ``FAILED → PENDING`` is the **recovery**: a delivery that exhausted its
#:   attempts against a Cloud API that was rate-limiting for an hour is not wrong,
#:   it is stale. It is deliberately not reachable from ``DELIVERED`` — sending a
#:   second message about a hearing to somebody who already has one is the one
#:   mistake this feature must not make.
STATUS_TRANSITIONS: Mapping[WhatsAppDeliveryStatus, frozenset[WhatsAppDeliveryStatus]] = (
    MappingProxyType(
        {
            WhatsAppDeliveryStatus.PENDING: frozenset(
                {WhatsAppDeliveryStatus.SENDING, WhatsAppDeliveryStatus.FAILED}
            ),
            WhatsAppDeliveryStatus.SENDING: frozenset(
                {
                    WhatsAppDeliveryStatus.DELIVERED,
                    WhatsAppDeliveryStatus.FAILED,
                    WhatsAppDeliveryStatus.PENDING,
                }
            ),
            WhatsAppDeliveryStatus.DELIVERED: frozenset(),
            WhatsAppDeliveryStatus.FAILED: frozenset({WhatsAppDeliveryStatus.PENDING}),
        }
    )
)


def can_transition(
    current: WhatsAppDeliveryStatus, target: WhatsAppDeliveryStatus
) -> bool:
    """Whether a delivery may move from ``current`` to ``target``."""
    return target in STATUS_TRANSITIONS.get(current, frozenset())


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class WhatsAppFailureCode(StrEnum):
    """Why one delivery attempt did not succeed.

    A closed vocabulary rather than a provider's message, for two reasons that
    are both as strong here as they were for :class:`~core.email.EmailFailureCode`:

    * these are **grouped in a monitoring view**, and Meta's ``error.message``
      interpolated into a metric produces one bucket per occurrence — its text
      carries a trace identifier and often the recipient's number;
    * a Cloud API error body **echoes the request**, including the number written
      to and sometimes the template parameters, so storing or logging the
      provider's own words would put a recipient's phone number and a case's
      wording into a diagnostic the spec's Logging section keeps them out of.

    Every member is produced at the provider boundary
    (:mod:`services.whatsapp_provider`), which is the only module that speaks
    HTTP to Meta — so the service records a *cause* without knowing what an
    ``HTTPError`` is.
    """

    #: No provider is configured, or the one that is cannot be reached at all.
    #: Transient: an operator fixing the configuration should not have cost the
    #: platform every message queued while it was wrong.
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    #: The connection could not be established, or TLS failed, or the response was
    #: not something this platform could read.
    CONNECTION_FAILED = "connection_failed"
    #: The provider did not answer inside ``WHATSAPP_TIMEOUT_SECONDS``.
    TIMEOUT = "timeout"
    #: The access token was rejected or has expired, or the phone number is not
    #: registered to this business account. **Permanent**: no amount of waiting
    #: fixes a credential, and retrying one is how an application gets throttled
    #: on top of being unauthorized.
    AUTHENTICATION_FAILED = "authentication_failed"
    #: The provider refused this recipient — the number is not on WhatsApp, or the
    #: message was undeliverable to it. Permanent for this delivery.
    RECIPIENT_REFUSED = "recipient_refused"
    #: The number on the account is missing or not a usable E.164 number.
    #: Permanent, and caught before the provider is ever called.
    INVALID_RECIPIENT = "invalid_recipient"
    #: The provider refused the message itself — a malformed parameter, a payload
    #: it would not accept. Permanent: the same request will be refused again.
    MESSAGE_REFUSED = "message_refused"
    #: **The template is not usable**: not approved, paused, deleted, in the wrong
    #: language, or given the wrong number of parameters. Permanent, and its own
    #: code rather than a ``message_refused`` because it is the one failure on this
    #: channel that an operator fixes in the WhatsApp Business account rather than
    #: in the platform — the spec calls the approval process out for exactly that
    #: reason, and a monitoring view that could not distinguish it would send
    #: somebody to the wrong console.
    TEMPLATE_REJECTED = "template_rejected"
    #: The provider asked to be tried again later: a messaging rate limit, a
    #: per-pair limit, a spam-rate limit, or an HTTP 429. The textbook transient
    #: failure, and the one this channel meets most: WhatsApp rate-limits per
    #: business number and per recipient pair.
    THROTTLED = "throttled"
    #: The template descriptor could not be rendered on this side. Permanent, and
    #: a **deployment fault** rather than a delivery one — recorded on the row so
    #: it is visible instead of being a silent gap.
    TEMPLATE_FAILURE = "template_failure"
    #: Anything the provider boundary did not anticipate. Treated as transient,
    #: because the alternative is discarding a hearing notice over an unrecognised
    #: error code.
    UNKNOWN = "unknown"


#: Failures worth trying again.
#:
#: ``18-whatsapp-delivery-channel.md`` names *"timeout, temporary provider
#: outage, network interruption, rate limiting"* and asks that permanent failures
#: be logged rather than retried. This set is that sentence, and its
#: **complement is the interesting half**: a rejected token, a number that is not
#: on WhatsApp, an unapproved template, and a malformed payload will all fail
#: identically on every attempt, so retrying them spends the platform's time and
#: the provider's rate limit to reach the same outcome more slowly.
TRANSIENT_FAILURE_CODES: frozenset[WhatsAppFailureCode] = frozenset(
    {
        WhatsAppFailureCode.PROVIDER_UNAVAILABLE,
        WhatsAppFailureCode.CONNECTION_FAILED,
        WhatsAppFailureCode.TIMEOUT,
        WhatsAppFailureCode.THROTTLED,
        WhatsAppFailureCode.UNKNOWN,
    }
)


def is_transient(code: WhatsAppFailureCode) -> bool:
    """Whether a failure is worth another attempt."""
    return code in TRANSIENT_FAILURE_CODES


def failure_from_value(value: str | None) -> WhatsAppFailureCode | None:
    """Read a stored failure code, tolerating one this version does not define.

    ``None`` rather than an exception, for the reason
    :func:`~core.email.failure_from_value` returns one: the column is an open
    registry, and a code written by a later version of the platform must not make
    an earlier one unable to read a delivery record.
    """
    if not value:
        return None
    try:
        return WhatsAppFailureCode(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Retry schedule
# --------------------------------------------------------------------------- #


def retry_delay_seconds(attempt: int, *, base: float, cap: float) -> float:
    """How long to wait before attempt number ``attempt + 1``.

    Exponential backoff with a ceiling — ``code-standards.md``: *"Retry failed
    jobs using exponential backoff"* — and deliberately **deterministic**, with no
    jitter, for the reason :func:`~core.email.retry_delay_seconds` records: this
    platform runs one small worker pool against one provider, so what jitter would
    actually buy is a retry schedule no test can assert on.

    The ceiling matters more than the base does, and more here than on the email
    channel: WhatsApp is the platform's *urgent* channel, so a tenth attempt four
    hours out would deliver a hearing update after the hearing — which is worse
    than not sending it at all, because the reader will act on it.

    Args:
        attempt: attempts already made. ``0`` is "the first one has just failed".
        base: delay after the first failure, in seconds.
        cap: longest delay this schedule will ever produce, in seconds.

    Returns:
        Seconds to wait, never negative and never above ``cap``.
    """
    if attempt <= 0:
        return max(0.0, min(base, cap))
    # `2 ** attempt` on a large attempt count is an integer with a lot of digits,
    # not an overflow — but it is pointless work, so the exponent is clamped to
    # the first value that certainly exceeds any sane cap.
    exponent = min(attempt, 24)
    return max(0.0, min(base * (2.0**exponent), cap))


def next_attempt_at(
    attempt: int, *, base: float, cap: float, now: datetime | None = None
) -> datetime:
    """When a delivery becomes eligible for its next attempt."""
    from datetime import timedelta

    reference = now or datetime.now(UTC)
    return reference + timedelta(
        seconds=retry_delay_seconds(attempt, base=base, cap=cap)
    )


# --------------------------------------------------------------------------- #
# Which notifications travel by WhatsApp
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WhatsAppRule:
    """One notification rule's WhatsApp delivery, if it has one.

    Deliberately tiny, exactly as :class:`~core.email.EmailRule` is. It carries
    **no wording, no audience, no priority, and no preference key** — every one of
    those already lives on the :class:`~core.notifications.NotificationRule` this
    is keyed by, and a second copy here would be a second place for them to
    disagree. What is left is the only decision this feature actually owns:
    *which template carries it*.
    """

    #: The :attr:`~core.notifications.NotificationRule.key` this applies to.
    rule_key: str
    #: The template's name. It is **both** the descriptor file under
    #: ``apps/api/whatsapp/`` and the name the template must be registered under
    #: in the WhatsApp Business account — one name rather than two, because two
    #: would be two things to keep in step across a console the platform cannot
    #: read.
    template: str
    #: Which version of that descriptor. Pinned per rule rather than globally, so
    #: a new version of the security template can ship without re-rendering every
    #: case assignment through it.
    version: int = 1


#: The general-purpose template: a greeting, the notification's heading and
#: sentence, and — when the notification names a resource — where to open it.
TEMPLATE_NOTIFICATION: Final[str] = "notification"

#: The account-security template. A separate descriptor rather than a flag on the
#: one above, for the reason :data:`~core.email.TEMPLATE_SECURITY` is separate: a
#: security message is genuinely a different document. It offers **no link at
#: all** — there is nothing to open, and a link in a WhatsApp message about a
#: password is precisely the shape of a phishing message, which matters more on a
#: channel where the platform's identity is a phone number the reader may not
#: recognise.
TEMPLATE_SECURITY: Final[str] = "security"


def _rule_key(event_type: DomainEventType) -> str:
    """The notification rule key one event produces, resolved at import.

    Looked up rather than written as a string literal, so a rule renamed or
    withdrawn in :mod:`core.notifications` fails **here, at import**, rather than
    becoming an entry in :data:`WHATSAPP_RULES` that silently matches nothing and
    quietly stops a class of message.
    """
    return EVENT_RULES[event_type].key


#: Which notifications are delivered over WhatsApp, and what carries each one.
#:
#: **This is the whole of "marked for WhatsApp delivery"**, and it is exactly the
#: spec's "Supported Notification Types" list:
#:
#: * *Authentication* — account activation, and the password reset the spec marks
#:   optional. The reset is **included**: it is the platform's only ``CRITICAL``
#:   notification, its entire value is being seen within minutes, and a phone is
#:   where somebody actually is. The link-free security template is what makes
#:   including it safe;
#: * *Case Management* — a new case assignment;
#: * *Court* — hearing rescheduled, urgent hearing update, and the nearest thing
#:   the platform has to a hearing reminder. See the note below;
#: * *Reports* — the AI report the spec marks optional, **included**: a report is
#:   minutes of generation the author is waiting on, and "it is ready" is worth a
#:   message precisely because they have gone to do something else;
#: * *System* — critical announcements.
#:
#: **On "Hearing Reminder", "Hearing Rescheduled", and "Urgent Hearing Update".**
#: The spec names three court messages and the platform produces two hearing
#: notifications, so the mapping is stated rather than guessed:
#: ``hearing.updated`` is a change to a case's court-facing fields — the court,
#: the filing date, or the next hearing date — which is *rescheduled* and *urgent
#: update* both; ``hearing.scheduled`` is a case entering "waiting for hearing",
#: which is the platform's only forward-looking hearing message and therefore the
#: closest thing it has to a reminder. **A true reminder — "your hearing is
#: tomorrow" — does not exist yet**, because it needs the scheduled reminders
#: ``16-notifications.md`` explicitly left out of scope. Building one here would
#: be *notification policy*, which this spec's "Out of Scope" section forbids this
#: feature from touching; when it arrives as a notification rule, the WhatsApp
#: side is one entry in this table.
#:
#: What is **absent** matters at least as much, and each omission is a decision:
#:
#: * every ``document.*``, every ``ocr.*``, every ``indexing.*``, the assistant's
#:   answers, timeline updates, document views, and report opens — the spec's
#:   *"Events That Must NOT Generate WhatsApp Messages"* list, in full. They
#:   remain in-app notifications, and this module cannot override that because it
#:   can only *narrow* what the Notification Service already created;
#: * ``system.announcement`` — the ordinary, ``NORMAL``-priority announcement. The
#:   spec asks for *"Critical System Announcement"*, and the platform's critical
#:   announcement is ``system.maintenance``. Messaging every routine announcement
#:   to every phone number the platform holds is how a business number gets
#:   blocked, and WhatsApp's own policy treats it as exactly that;
#: * ``case.created``, ``case.updated``, ``case.status_changed``,
#:   ``case.priority_changed``, ``case.archived``, ``case.restored``,
#:   ``case.unassigned``, ``ocr.failed``, ``report.failed``, and
#:   ``user.role_changed`` — all real notifications, none of them on the spec's
#:   list, and several of them already carried by email. Adding one is **one entry
#:   here plus one approved template**, which is what *"allow future message types
#:   without redesign"* means concretely.
WHATSAPP_RULES: Mapping[str, WhatsAppRule] = MappingProxyType(
    {
        rule.rule_key: rule
        for rule in (
            # --- Authentication ------------------------------------------- #
            WhatsAppRule(
                _rule_key(DomainEventType.USER_ACTIVATED), template=TEMPLATE_SECURITY
            ),
            WhatsAppRule(
                _rule_key(DomainEventType.USER_PASSWORD_RESET),
                template=TEMPLATE_SECURITY,
            ),
            # --- Case management ------------------------------------------- #
            WhatsAppRule(RULE_CASE_ASSIGNED.key, template=TEMPLATE_NOTIFICATION),
            # --- Court ------------------------------------------------------ #
            WhatsAppRule(RULE_HEARING_UPDATED.key, template=TEMPLATE_NOTIFICATION),
            WhatsAppRule(RULE_HEARING_SCHEDULED.key, template=TEMPLATE_NOTIFICATION),
            # --- Reports ---------------------------------------------------- #
            WhatsAppRule(
                _rule_key(DomainEventType.REPORT_GENERATED),
                template=TEMPLATE_NOTIFICATION,
            ),
            # --- System ----------------------------------------------------- #
            WhatsAppRule(
                ANNOUNCEMENT_RULES[AnnouncementKind.MAINTENANCE].key,
                template=TEMPLATE_NOTIFICATION,
            ),
        )
    }
)


def whatsapp_rule_for(rule_key: str) -> WhatsAppRule | None:
    """The WhatsApp delivery for one notification rule, or ``None`` if it has none.

    ``None`` is the ordinary answer for most of the platform's notifications and
    is never an error: the in-app feed carries everything, and this channel
    carries the subset the spec named.
    """
    return WHATSAPP_RULES.get(rule_key)


def is_whatsapp_eligible(rule_key: str) -> bool:
    """Whether notifications produced by this rule travel on this channel."""
    return rule_key in WHATSAPP_RULES


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #

#: Longest and shortest recipient the platform will hand to a provider, in digits.
#:
#: E.164 caps a subscriber number at 15 digits including the country code, and no
#: real routable number is shorter than about eight. Both bounds are here because
#: ``users.phone`` is a free-text column: it was never validated as a *messaging*
#: address, and a stored extension, a partial number, or a fax line would
#: otherwise be handed to the Cloud API to reject one message at a time.
MAX_PHONE_DIGITS: Final[int] = 15
MIN_PHONE_DIGITS: Final[int] = 8

#: Everything that is not a digit, for stripping a stored number down to E.164.
#:
#: A person types ``+212 6-12-34-56-78`` and the platform stored it verbatim,
#: because ``07-user-management`` had no reason to normalize a display field. The
#: Cloud API wants ``212612345678``: digits only, country code included, **no
#: leading ``+``**, no spaces, and no punctuation.
_NON_DIGITS = re.compile(r"\D+")

#: Prefixes that mean "an international number follows" and are not part of it.
#:
#: ``00`` is the ITU international access code used across Europe and North
#: Africa, and it is the one people actually write. It is stripped rather than
#: kept because ``00212…`` and ``212…`` are the same number, and sending one of
#: each would defeat the duplicate guard that works on notification identity by
#: making two different-looking recipients out of one person.
_INTERNATIONAL_PREFIX: Final[str] = "00"


class InvalidWhatsAppRecipientError(ValueError):
    """A number cannot be used as a WhatsApp recipient.

    A plain ``ValueError`` rather than an :class:`~core.exceptions.AppException`,
    exactly as :class:`~core.email.InvalidEmailRecipientError` is: this module is
    pure vocabulary and has no HTTP opinion. The delivery service turns it into an
    :attr:`WhatsAppFailureCode.INVALID_RECIPIENT` on the row.
    """


def normalize_phone(value: str | None, *, default_country_code: str | None = None) -> str | None:
    """Return a usable WhatsApp recipient in E.164 digits, or ``None``.

    **Deliberately conservative, and deliberately not a phone-number library.**
    Validating a national number properly means knowing every country's numbering
    plan, which is what `libphonenumber` exists for — and adding a dependency of
    that size to decide whether to send a message is not the trade this feature
    should make. What this does instead is refuse anything it cannot be *sure*
    about: an unrecognised number is one message not sent, which is the failure
    this channel is allowed to have, while a wrongly "corrected" number is a legal
    notification delivered to a stranger, which is not.

    Three inputs are accepted and everything else is refused:

    * ``+212612345678`` / ``00212612345678`` — explicitly international. The
      prefix is stripped and the rest is taken as-is;
    * ``212612345678`` — already E.164 digits;
    * ``0612345678`` **only when** ``default_country_code`` is configured. A
      leading zero is a national trunk prefix, and joining it to a country code
      would produce a different subscriber — so it is dropped, and the country
      code is prepended.

    Args:
        value: whatever is stored on the account.
        default_country_code: ``WHATSAPP_DEFAULT_COUNTRY_CODE``, digits only. A
            deployment that leaves it unset simply does not message the accounts
            whose number was entered in national format, which is the safe
            reading of an ambiguous value rather than a guess about it.

    Returns:
        Digits only, no leading ``+``, or ``None`` if this is not usable.
    """
    if not value:
        return None

    candidate = value.strip()
    if not candidate:
        return None

    explicit_international = candidate.startswith("+") or candidate.startswith(
        _INTERNATIONAL_PREFIX
    )
    digits = _NON_DIGITS.sub("", candidate)
    if not digits:
        return None

    if digits.startswith(_INTERNATIONAL_PREFIX):
        digits = digits[len(_INTERNATIONAL_PREFIX) :]
    elif not explicit_international and digits.startswith("0"):
        # A national number. Usable only if the deployment has said which country
        # its accounts are in; otherwise it is genuinely ambiguous and is refused.
        country = _NON_DIGITS.sub("", default_country_code or "")
        if not country:
            return None
        digits = f"{country}{digits.lstrip('0')}"

    if not digits or digits.startswith("0"):
        # A number that is still zero-led after all of the above is not E.164 —
        # no country code begins with zero.
        return None
    if not MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
        return None

    return digits


# --------------------------------------------------------------------------- #
# Template parameters
# --------------------------------------------------------------------------- #

#: Longest one template parameter may be, in characters.
#:
#: The Cloud API rejects a body parameter over 1024 characters outright, and a
#: notification's own bounds (``MAX_TITLE_LENGTH``, ``MAX_MESSAGE_LENGTH``) are
#: far below that already — so this is a bound that is only ever reached by a bug,
#: which is exactly the bound worth having.
MAX_PARAMETER_LENGTH: Final[int] = 1024

#: Characters the Cloud API refuses inside a template body parameter.
#:
#: **A real provider constraint rather than a defensive habit**: Meta rejects any
#: body parameter containing a newline, a tab, or four or more consecutive
#: spaces, because a parameter is substituted into an approved layout and those
#: would let a sender restructure it. The platform's own wording never contains
#: one — but an administrator's announcement reaches ``{message}``, and a
#: notification's context carries a case number somebody typed, so this is
#: enforced rather than assumed.
_PARAMETER_WHITESPACE = re.compile(r"\s+")


def sanitize_parameter(value: object, *, limit: int = MAX_PARAMETER_LENGTH) -> str:
    """Return ``value`` in a form the Cloud API will accept in a template body.

    Whitespace is **collapsed rather than refused**, which is the opposite of what
    :func:`~core.email.sanitize_header_value` does with a line break, and the
    asymmetry is deliberate. A newline in a mail header is an *injection* — it
    ends the header and starts another — so refusing is the only safe answer. A
    newline in a WhatsApp parameter is a formatting rule of the provider's, and an
    administrator who pressed Enter in an announcement has not attacked anything;
    refusing would drop their message, while collapsing delivers it with its
    paragraph breaks flattened. The structure of the message is the approved
    template's, not the parameter's, so nothing is lost that the parameter was
    entitled to.
    """
    collapsed = _PARAMETER_WHITESPACE.sub(" ", str(value)).strip()
    return collapsed[:limit]


# --------------------------------------------------------------------------- #
# Language
# --------------------------------------------------------------------------- #

#: The provider's language tag per platform language.
#:
#: WhatsApp templates are approved **per language**, and the tag is part of the
#: message: a template approved as ``fr`` cannot be sent as ``fr_FR``. The
#: platform speaks ISO 639-1 everywhere else, so the translation happens once,
#: here, rather than at the call site that happens to be talking to Meta.
#:
#: ``en`` is deliberately ``en_US`` and the other two are bare: those are the tags
#: WhatsApp actually lists for these languages. Getting one wrong is a
#: :attr:`WhatsAppFailureCode.TEMPLATE_REJECTED`, which is recoverable and
#: visible — but it is recoverable and visible *per message*, so it is worth being
#: a table rather than a convention.
PROVIDER_LANGUAGE_CODES: Mapping[str, str] = MappingProxyType(
    {LANGUAGE_FRENCH: "fr", LANGUAGE_ARABIC: "ar", LANGUAGE_ENGLISH: "en_US"}
)


def provider_language_code(language: str) -> str:
    """The provider's tag for a platform language, falling back to French.

    French rather than English, matching
    :func:`~core.notifications.resolve_notification_language` and every other
    fallback on this platform.
    """
    return PROVIDER_LANGUAGE_CODES.get(
        language, PROVIDER_LANGUAGE_CODES[LANGUAGE_FRENCH]
    )


def resolve_whatsapp_language(requested: str | None) -> str:
    """Decide which language one message is written in.

    Delegates to :func:`~core.notifications.resolve_notification_language`, so a
    WhatsApp message and the in-app notification it carries can never be rendered
    by two different resolvers.

    Since ``21-localization.md`` shipped, ``requested`` is the **recipient's own**
    ``user_settings.language``, read by
    :class:`~services.localization.SettingsLanguageDirectory` before the delivery
    row is queued and snapshotted onto it — which matters more here than one
    channel over, because the message goes to a device in somebody's pocket and
    :func:`provider_language_code` has to hand Meta a language tag that names an
    **approved template**. ``WHATSAPP_DEFAULT_LANGUAGE`` remains the last resort
    for an account that has chosen nothing.
    """
    return resolve_notification_language(requested)


# --------------------------------------------------------------------------- #
# What a template may interpolate
# --------------------------------------------------------------------------- #

#: Where each kind of notification target lives in the web application.
#:
#: The same table :data:`~core.email.TARGET_PATHS` holds, and it is duplicated
#: rather than imported **deliberately**: importing it would make this channel
#: depend on the email channel, and two delivery channels that import each other
#: are one channel with two exits. The paths are the *web application's* routes,
#: which neither channel owns; if a third consumer appears, the honest move is to
#: lift them into :mod:`core.notifications` beside the targets they are keyed by,
#: not to make one channel the other's library.
TARGET_PATHS: Mapping[NotificationTargetType, str] = MappingProxyType(
    {
        NotificationTargetType.CASE: "/cases/{id}",
        NotificationTargetType.DOCUMENT: "/documents",
        NotificationTargetType.REPORT: "/reports",
        NotificationTargetType.ACCOUNT: "/settings",
    }
)

#: Where a notification with no target sends its reader: their own feed, which
#: always exists and always contains the item the message is about.
FEED_PATH: Final[str] = "/notifications"


def target_url(
    base_url: str | None,
    *,
    target_type: str | None,
    target_id: uuid.UUID | None,
) -> str | None:
    """Build the link a message offers, or ``None`` when it should offer none.

    ``None`` when no base URL is configured, deliberately: a message carrying a
    ``/cases/…`` path with no host is a broken link, and a broken link in a
    WhatsApp message is worse than a broken one in an email — it is unclickable
    text in a thread the reader cannot fix. A deployment that sets a base URL gets
    links; one that does not gets correct, linkless messages.
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
# A message is not only the notification it carries: it opens with a greeting and
# closes with a line saying where to read the rest. That wording is **user-facing
# text**, and `ai-workflow-rules.md` is unambiguous about it — *"no hardcoded UI
# strings, translation keys only, French and Arabic"*.
#
# It lives **here rather than inside the approved WhatsApp template**, and that
# is the whole reason this channel composes parameters instead of letting Meta
# hold the sentences: a template approved in a console is text this repository
# cannot review, cannot diff, and cannot keep in step with `core/notifications.py`.
# Putting the platform's words on this side means the approved template is
# structure — a greeting slot, a heading slot, a body slot — and every word in it
# came from a module the rest of the platform can read.


@dataclass(frozen=True, slots=True)
class WhatsAppChrome:
    """The wording around a notification, in one language."""

    #: Interpolates ``{name}``.
    greeting: str
    #: Introduces the link, when there is one.
    action_label: str
    #: Where to read it when there is no link to offer.
    no_link: str
    #: What to do if the reader did not expect an account-security message. Only
    #: the security template uses it; it is on every language's entry so the two
    #: templates share one type.
    security_note: str


def _chrome(
    *, fr: WhatsAppChrome, ar: WhatsAppChrome, en: WhatsAppChrome
) -> Mapping[str, WhatsAppChrome]:
    """Build the per-language table, keyed the way every other one here is."""
    return MappingProxyType(
        {LANGUAGE_FRENCH: fr, LANGUAGE_ARABIC: ar, LANGUAGE_ENGLISH: en}
    )


#: The message chrome, per language. French is the fallback, matching
#: :meth:`~core.notifications.NotificationTemplate.title` and for the same reason.
#:
#: Shorter than :data:`~core.email.EMAIL_CHROME`'s, and not by accident: a
#: WhatsApp message is read on a phone in a thread, so the "you are receiving
#: this because…" paragraph an email closes with would be most of the message.
#: The unsubscribe story is told by the settings page, which the reader reaches
#: from the platform they are already signed in to.
WHATSAPP_CHROME: Mapping[str, WhatsAppChrome] = _chrome(
    fr=WhatsAppChrome(
        greeting="Bonjour {name}",
        action_label="Ouvrir",
        no_link="Consultez la plateforme pour les détails.",
        security_note=(
            "Si vous n'êtes pas à l'origine de cette action, contactez immédiatement "
            "votre administrateur."
        ),
    ),
    ar=WhatsAppChrome(
        greeting="مرحبًا {name}",
        action_label="افتح",
        no_link="اطّلع على التفاصيل في المنصة.",
        security_note="إذا لم تكن أنت من قام بهذا الإجراء، فاتصل بالمسؤول فورًا.",
    ),
    en=WhatsAppChrome(
        greeting="Hello {name}",
        action_label="Open",
        no_link="Open the platform for the details.",
        security_note="If you did not do this, contact your administrator immediately.",
    ),
)


def chrome_for(language: str, *, recipient_name: str) -> Mapping[str, str]:
    """The chrome one message renders with, greeting already interpolated."""
    chrome = WHATSAPP_CHROME.get(language) or WHATSAPP_CHROME[LANGUAGE_FRENCH]
    return MappingProxyType(
        {
            "greeting": chrome.greeting.format(name=recipient_name),
            "action_label": chrome.action_label,
            "no_link": chrome.no_link,
            "security_note": chrome.security_note,
        }
    )


@dataclass(frozen=True, slots=True)
class WhatsAppContext:
    """Everything a template descriptor is allowed to see.

    A frozen value rather than a loose dictionary, because the security property
    this feature has to hold is a statement about *this list*: a message's body is
    the notification's own rendered wording, the handful of scalars its sentence
    already interpolates, and presentation. There is no case, no document, no
    passage, no report section, and no user record here — so
    ``18-whatsapp-delivery-channel.md``'s *"messages must never expose information
    the recipient is not authorized to receive"* is true because there is nothing
    in scope that could expose it.
    """

    #: The notification's rendered heading.
    title: str
    #: The notification's rendered sentence.
    message: str
    #: Who it is addressed to, for the greeting. A display name, never a number.
    recipient_name: str
    #: ISO 639-1 code. The provider's own tag is derived from it at the boundary.
    language: str
    #: Which area of the platform the news came from, and how urgent it is. Used
    #: for presentation only — never for a decision.
    category: str
    priority: str
    #: Where to read it in the application, when there is somewhere to send them.
    action_url: str | None
    #: The values the notification's own sentence interpolates — a case number, a
    #: page count, a failure code. Already bounded and screened by
    #: :func:`~core.notifications.normalize_context`, which is why they can be
    #: passed through without a second screen. **This is where the spec's "case
    #: title", "hearing date", and "report name" variables come from**: a template
    #: that wants one reads it here, and gets whatever the notification actually
    #: carried rather than a second lookup this channel is not allowed to make.
    values: Mapping[str, Any]
    #: The platform's own name, for the signature. Configuration, not content.
    platform_name: str
    #: The localized wording around the notification — see :data:`WHATSAPP_CHROME`.
    chrome: Mapping[str, str]

    def as_mapping(self) -> dict[str, Any]:
        """The descriptor's variables, as Jinja receives them."""
        return {
            "title": self.title,
            "message": self.message,
            "recipient_name": self.recipient_name,
            "language": self.language,
            "category": self.category,
            "priority": self.priority,
            "action_url": self.action_url,
            "values": dict(self.values),
            "platform_name": self.platform_name,
            "chrome": dict(self.chrome),
        }


def build_whatsapp_context(
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
) -> WhatsAppContext:
    """Assemble what one message's template descriptor renders from.

    The title and the message come from
    :func:`~core.notifications.render_notification` — the **same** function the
    in-app feed and the email channel render from, called with the same rule key
    and the same stored context. Three consequences, and all three are
    requirements rather than conveniences: the three channels can never say
    different things about the same event, an Arabic recipient's message is Arabic
    for the same reason their history is, and the approved WhatsApp template never
    becomes a fourth place the platform's wording lives.
    """
    rendered = render_notification(
        rule_key=rule_key, category=category, context=context, language=language
    )

    return WhatsAppContext(
        title=rendered.title,
        message=rendered.message,
        recipient_name=recipient_name,
        language=language,
        category=category,
        priority=priority,
        action_url=target_url(base_url, target_type=target_type, target_id=target_id),
        values=dict(context or {}),
        platform_name=platform_name,
        chrome=chrome_for(language, recipient_name=recipient_name),
    )


__all__ = [
    "FEED_PATH",
    "MAX_PARAMETER_LENGTH",
    "MAX_PHONE_DIGITS",
    "MIN_PHONE_DIGITS",
    "PROVIDER_LANGUAGE_CODES",
    "STATUS_TRANSITIONS",
    "TARGET_PATHS",
    "TEMPLATE_NOTIFICATION",
    "TEMPLATE_SECURITY",
    "TRANSIENT_FAILURE_CODES",
    "WHATSAPP_CHROME",
    "WHATSAPP_RULES",
    "InvalidWhatsAppRecipientError",
    "WhatsAppChrome",
    "WhatsAppContext",
    "WhatsAppDeliveryStatus",
    "WhatsAppFailureCode",
    "WhatsAppRule",
    "build_whatsapp_context",
    "can_transition",
    "chrome_for",
    "failure_from_value",
    "is_transient",
    "is_whatsapp_eligible",
    "next_attempt_at",
    "normalize_phone",
    "provider_language_code",
    "resolve_whatsapp_language",
    "retry_delay_seconds",
    "sanitize_parameter",
    "target_url",
    "whatsapp_rule_for",
]
