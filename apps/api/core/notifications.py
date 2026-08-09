"""The platform's notification vocabulary, and the rules that derive one from an event.

``16-notifications.md`` asks for a notification system that is **event-driven**:
business modules publish domain events without knowing that notifications exist,
and a Notification Service turns those events into notifications for the right
people. This module is the half of that with no I/O in it — the vocabulary, the
text, and the table that says *which event becomes which notification for whom*.

Nothing here touches a database, a session, a socket, or a user. It is pure data
plus four derivations, and each one is a requirement of the spec made mechanical:

* **What a notification is about** — :class:`NotificationCategory`,
  :class:`NotificationType`, :class:`NotificationPriority`, and
  :class:`NotificationTarget`. The first is an *open* registry and the other two
  are closed; see :mod:`models.notification` for why the storage follows that
  split exactly.
* **Which notifications a person receives** — :class:`NotificationPreferenceKey`,
  the seven the spec lists by name, each mapped from a rule rather than from a
  category, so *"OCR completion"* can be silenced without silencing everything
  about a document.
* **What an event becomes** — :data:`EVENT_RULES`, the single table mapping a
  :class:`~core.events.DomainEventType` onto a :class:`NotificationRule`. Adding
  an event is **one entry here**; nothing in the service, the subscriber, the
  repository, the API, or the client changes, which is the spec's *"support
  additional events without redesign"* made structural rather than promised.
* **What it says** — :class:`NotificationTemplate`, in Arabic, French, and
  English.

**The text is rendered at read time, never stored.** A notification row keeps its
rule key and a small, screened :func:`normalize_context`; the title and the
message are produced by :func:`render_notification` in the language the reader
asks for. Three things follow from that, and all three are why it was done this
way:

* a user who switches the interface to Arabic sees their **whole history** in
  Arabic, rather than a list of notifications frozen in whichever language was
  configured the day each one arrived — which is what
  ``ai-workflow-rules.md``'s *"no hardcoded UI strings, RTL for Arabic"* actually
  requires of a persisted feed;
* *"Never log confidential notification contents"* becomes trivial: there is no
  stored prose to log, and the context is bounded scalars screened by the same
  rule the event dispatcher applies to a payload;
* a future email or WhatsApp sender renders from **this** module rather than
  re-deriving the wording, so ``code-standards.md``'s *"notification logic must
  never be duplicated"* holds across channels that do not exist yet.

The cost is stated rather than hidden: a rule key removed in a later version
would leave old rows unrenderable, so :func:`render_notification` falls back to
the category's own generic wording instead of raising. A notification that reads
"Case update" is worse than one that reads well; it is very much better than a
history page that will not load.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from core.events import DomainEventType
from core.indexing import LANGUAGE_ARABIC, LANGUAGE_ENGLISH, LANGUAGE_FRENCH
from core.rag import SUPPORTED_ANSWER_LANGUAGES
from models.notification import NotificationPriority, NotificationType

# --------------------------------------------------------------------------- #
# Categories, types, and priorities
# --------------------------------------------------------------------------- #


class NotificationCategory(StrEnum):
    """What area of the platform a notification belongs to.

    Exactly the eight ``16-notifications.md`` requires *"at minimum"*. The spec
    also requires that *"the implementation should allow future categories
    without redesign"*, which is why the storage column is a ``VARCHAR`` rather
    than a PostgreSQL enum — the same trade ``timeline_events.event_type`` makes,
    and the opposite of the one ``report_type`` makes. A ninth category is a
    member here plus its rules; **no migration**, and every read path tolerates a
    value it does not recognise.
    """

    #: A case's lifecycle, assignments, and metadata.
    CASE = "case"
    #: A file attached to a case: uploaded, replaced, withdrawn.
    DOCUMENT = "document"
    #: The court's calendar: hearings scheduled, moved, or awaited.
    HEARING = "hearing"
    #: Text extraction from an uploaded file.
    OCR = "ocr"
    #: Anything the AI pipeline produced that is not a report.
    AI = "ai"
    #: Generated legal reports.
    REPORT = "report"
    #: The reader's own account.
    USER = "user"
    #: Platform-wide announcements and maintenance windows.
    SYSTEM = "system"


#: :class:`~models.notification.NotificationType` and
#: :class:`~models.notification.NotificationPriority` are **re-exported** from
#: :mod:`models.notification` rather than defined here, and the asymmetry with
#: :class:`NotificationCategory` above is the point rather than an inconsistency.
#:
#: Both of those are persisted as **database enums**, so — following the rule
#: :mod:`core.roles` applies to ``UserRole`` — the storage enum *is* the
#: canonical definition and lives beside the column it constrains, where the two
#: cannot drift apart. The category is a ``VARCHAR`` because its registry is open
#: by design, so it has no storage type to be the definition of and belongs here
#: with the rest of the vocabulary.
#:
#: Re-exported rather than left to be imported from two places, so every consumer
#: of this feature has one import site for its vocabulary.

#: Display and sort order for priorities.
#:
#: Ordered by *rank* rather than by the stored value, for the reason
#: :data:`~core.cases.PRIORITY_RANK` records: sorting alphabetically would place
#: ``critical`` below ``low`` and make "most urgent first" mean nothing.
PRIORITY_RANK: Mapping[NotificationPriority, int] = MappingProxyType(
    {
        NotificationPriority.LOW: 0,
        NotificationPriority.NORMAL: 1,
        NotificationPriority.HIGH: 2,
        NotificationPriority.CRITICAL: 3,
    }
)


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #


class NotificationTargetType(StrEnum):
    """What a notification opens, when it opens something.

    ``16-notifications.md``: *"Navigation should remain independent from frontend
    routing implementation."* So a notification names a **resource**, never a
    URL: ``case``/``<uuid>`` rather than ``/cases/<uuid>``. The client owns its
    own routes and maps this pair onto one — which is what lets the web app
    restructure its paths, and a future mobile client exist at all, without the
    API changing.

    There is deliberately no ``url`` field anywhere in this feature.

    **There is no ``HEARING`` member, and its absence is a fact about the
    platform rather than an omission here.** ``16-notifications.md`` lists *"open
    hearing"* among the navigation examples, but a hearing is not a resource on
    this platform: it is a set of *fields on a case* (``court_name``,
    ``filing_date``, ``next_hearing_date``), which is why
    ``cases:update-hearing`` is a narrowing of ``cases:update`` rather than a
    permission of its own. So the two hearing rules target their **case**, which
    is exactly where the hearing information a reader is being sent to look at
    lives. A member here would be a target nothing could open. If hearings ever
    become their own entity, this is one member plus one branch in
    :func:`target_for`.
    """

    CASE = "case"
    DOCUMENT = "document"
    REPORT = "report"
    #: The reader's own account settings. Carries no identifier of its own.
    ACCOUNT = "account"


@dataclass(frozen=True, slots=True)
class NotificationTarget:
    """The resource a notification points at, if any."""

    target_type: NotificationTargetType
    #: ``None`` for :attr:`NotificationTargetType.ACCOUNT`, which names the
    #: reader and therefore needs no identifier.
    target_id: uuid.UUID | None = None


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


class NotificationPreferenceKey(StrEnum):
    """What a user can switch off.

    Exactly the seven ``16-notifications.md`` lists, and they are deliberately
    **not** the same axis as :class:`NotificationCategory`: *"OCR completion"* is
    its own preference while OCR is also, arguably, document news, and *"hearing
    updates"* is a refinement of case news rather than a separate source. Keying
    preferences to the rule rather than to the category is what lets a lawyer
    keep case updates and silence the extraction pipeline.

    Like the category, this is an **open** registry in storage: preferences are
    one row per ``(user, key)``, so an eighth key needs no migration — see
    :mod:`models.notification`.
    """

    CASE_UPDATES = "case_updates"
    DOCUMENT_UPDATES = "document_updates"
    OCR_COMPLETION = "ocr_completion"
    AI_REPORT_COMPLETION = "ai_report_completion"
    HEARING_UPDATES = "hearing_updates"
    ACCOUNT_ACTIVITY = "account_activity"
    SYSTEM_ANNOUNCEMENTS = "system_announcements"


#: Whether a preference is on for a user who has never expressed one.
#:
#: All seven default to **on**, and that is the only defensible default for this
#: platform: `architecture.md` invariant 3 says every important event produces a
#: notification for its authorized users, so a default of *off* would make the
#: invariant false for every account until somebody visited a settings page.
#:
#: A row is written only when a user actually changes something, so an untouched
#: account costs no storage and silently follows any future change to these
#: defaults — which is the behaviour somebody who has never opened the settings
#: page would expect.
DEFAULT_PREFERENCES: Mapping[NotificationPreferenceKey, bool] = MappingProxyType(
    dict.fromkeys(NotificationPreferenceKey, True)
)


def preference_from_value(value: str) -> NotificationPreferenceKey | None:
    """Resolve a stored preference key, tolerating one this version has dropped.

    ``None`` rather than an exception, for the reason the timeline's event-type
    registry is read tolerantly: the column is an open vocabulary, and a row
    written by a later version of the platform must not make an earlier one
    unable to read a user's settings.
    """
    try:
        return NotificationPreferenceKey(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Audiences
# --------------------------------------------------------------------------- #


class NotificationAudience(StrEnum):
    """Who a rule's notification is for.

    ``16-notifications.md``: *"Users should only receive notifications they are
    authorized to view […] Authorization should be enforced before notification
    creation."* This enum names the **intent**;
    :mod:`services.notification_recipients` resolves it against the database and
    then re-checks every resolved recipient against the policy that owns the
    resource. The two are separate on purpose — an audience that widened by
    accident would still be narrowed by the policy, because the policy is asked
    last and asked about each person individually.
    """

    #: Everyone party to the case: its assigned lawyer, its assigned court
    #: representative, and whoever created it. Deliberately **not** "every
    #: administrator": an administrator holds ``cases:view-all`` and could open
    #: the case, but notifying all of them about every case on the platform is
    #: noise rather than news, and the spec asks for notifications about *"events
    #: occurring within the platform"* that concern the reader.
    CASE_PARTICIPANTS = "case_participants"
    #: The person named by the event's payload — the ``assignee_id`` of a case
    #: assignment. The one audience read from a payload rather than from a row.
    ASSIGNEE = "assignee"
    #: The owner of the resource the event is about. A report belongs to the user
    #: who generated it (``14-ai-report-agent.md``), and nobody else may even
    #: learn that it exists.
    RESOURCE_OWNER = "resource_owner"
    #: The account the event is about, which for a ``user`` topic is the account
    #: the topic names.
    SUBJECT_USER = "subject_user"


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

#: Longest rendered title and message, in characters.
#:
#: Generous for a sentence and far too small for anything that could be mistaken
#: for content. The templates below are all well inside both; the limits exist
#: because a rendered string interpolates a *context value*, and a bound that is
#: only ever reached by a bug is exactly the bound worth having.
MAX_TITLE_LENGTH: Final[int] = 200
MAX_MESSAGE_LENGTH: Final[int] = 500

#: Most keys one notification's context may carry, and the longest string value.
#:
#: The same shape as :data:`~core.events.MAX_PAYLOAD_KEYS` and deliberately
#: smaller: an event payload is a client's refetch instruction, while this is the
#: handful of identifiers a sentence interpolates.
MAX_CONTEXT_KEYS: Final[int] = 12
MAX_CONTEXT_VALUE_LENGTH: Final[int] = 200

#: What a missing context value renders as.
#:
#: An em dash rather than an empty string or a raw ``{placeholder}``: the first
#: makes a sentence read as though a word were lost, and the second shows a user
#: the platform's own template syntax. This is only reachable if a publisher
#: stops sending a key a template expects, which is a bug — and one that should
#: degrade into a slightly vague notification rather than into no notification.
MISSING_VALUE: Final[str] = "—"


class _DefaultingContext(dict[str, Any]):
    """A format mapping that renders an absent key as :data:`MISSING_VALUE`."""

    def __missing__(self, key: str) -> str:
        return MISSING_VALUE


@dataclass(frozen=True, slots=True)
class NotificationTemplate:
    """One notification's wording, in each language the platform supports.

    French is the fallback for a language with no entry, matching
    :meth:`~core.reports.ReportTemplate.title` and for the same reason:
    ``project-overview.md`` names Arabic and French as the platform's interface
    languages, and English is present because the API and its tests are written
    in it.
    """

    titles: Mapping[str, str]
    messages: Mapping[str, str]

    def title(self, language: str) -> str:
        """The heading in ``language``."""
        return self.titles.get(language) or self.titles[LANGUAGE_FRENCH]

    def message(self, language: str) -> str:
        """The body in ``language``."""
        return self.messages.get(language) or self.messages[LANGUAGE_FRENCH]


def _template(
    *, fr: tuple[str, str], ar: tuple[str, str], en: tuple[str, str]
) -> NotificationTemplate:
    """Build a template from one ``(title, message)`` pair per language.

    A helper rather than three literal mappings per rule, for the reason
    :func:`~core.reports._template` is one: forty rules written as raw mappings
    is forty chances to put an Arabic message under the French key, and that is a
    mistake nobody notices until an Arabic reader does.
    """
    return NotificationTemplate(
        titles=MappingProxyType(
            {LANGUAGE_FRENCH: fr[0], LANGUAGE_ARABIC: ar[0], LANGUAGE_ENGLISH: en[0]}
        ),
        messages=MappingProxyType(
            {LANGUAGE_FRENCH: fr[1], LANGUAGE_ARABIC: ar[1], LANGUAGE_ENGLISH: en[1]}
        ),
    )


#: What a notification says when its rule key is one this version does not know.
#:
#: Reached only by a row written by a *later* version of the platform, or by one
#: whose rule was withdrawn. Keyed by category, so even the fallback says which
#: part of the platform the news came from.
_FALLBACK_TEMPLATES: Mapping[NotificationCategory, NotificationTemplate] = MappingProxyType(
    {
        NotificationCategory.CASE: _template(
            fr=("Mise à jour du dossier", "Un dossier a été mis à jour."),
            ar=("تحديث في الملف", "تم تحديث أحد الملفات."),
            en=("Case update", "A case was updated."),
        ),
        NotificationCategory.DOCUMENT: _template(
            fr=("Mise à jour d'un document", "Un document a été mis à jour."),
            ar=("تحديث في المستندات", "تم تحديث أحد المستندات."),
            en=("Document update", "A document was updated."),
        ),
        NotificationCategory.HEARING: _template(
            fr=("Mise à jour d'audience", "Une information d'audience a changé."),
            ar=("تحديث بشأن الجلسة", "تم تغيير معلومات الجلسة."),
            en=("Hearing update", "Hearing information changed."),
        ),
        NotificationCategory.OCR: _template(
            fr=("Extraction de texte", "L'extraction de texte a évolué."),
            ar=("استخراج النص", "تم تحديث حالة استخراج النص."),
            en=("Text extraction", "Text extraction was updated."),
        ),
        NotificationCategory.AI: _template(
            fr=("Assistant juridique", "Une tâche d'intelligence artificielle a évolué."),
            ar=("المساعد القانوني", "تم تحديث إحدى مهام الذكاء الاصطناعي."),
            en=("Legal assistant", "An AI task was updated."),
        ),
        NotificationCategory.REPORT: _template(
            fr=("Rapport", "Un rapport a évolué."),
            ar=("تقرير", "تم تحديث أحد التقارير."),
            en=("Report", "A report was updated."),
        ),
        NotificationCategory.USER: _template(
            fr=("Votre compte", "Une modification a été apportée à votre compte."),
            ar=("حسابك", "تم إجراء تغيير على حسابك."),
            en=("Your account", "A change was made to your account."),
        ),
        NotificationCategory.SYSTEM: _template(
            fr=("Annonce", "La plateforme a publié une annonce."),
            ar=("إعلان", "نشرت المنصة إعلانًا."),
            en=("Announcement", "The platform published an announcement."),
        ),
    }
)


# --------------------------------------------------------------------------- #
# Localized labels for the values a template interpolates
# --------------------------------------------------------------------------- #
#
# Three small maps, and they exist for one reason: a notification that says "the
# status of case CASE-2026-0004 changed" is useless where one that says "moved to
# *awaiting hearing*" is not, and interpolating the raw enum value would put an
# English snake_case token in the middle of an Arabic sentence.
#
# They are **presentation only** and they fail soft: an unmapped value renders as
# itself. That is what keeps them from becoming a second definition of the case
# lifecycle that has to be kept in step with `models/case.py` — a status added
# there and not here produces a slightly raw notification, never a broken one.

_STATUS_LABELS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "draft": MappingProxyType(
            {LANGUAGE_FRENCH: "brouillon", LANGUAGE_ARABIC: "مسودة", LANGUAGE_ENGLISH: "draft"}
        ),
        "open": MappingProxyType(
            {LANGUAGE_FRENCH: "ouvert", LANGUAGE_ARABIC: "مفتوح", LANGUAGE_ENGLISH: "open"}
        ),
        "in_progress": MappingProxyType(
            {
                LANGUAGE_FRENCH: "en cours",
                LANGUAGE_ARABIC: "قيد المعالجة",
                LANGUAGE_ENGLISH: "in progress",
            }
        ),
        "waiting_for_hearing": MappingProxyType(
            {
                LANGUAGE_FRENCH: "en attente d'audience",
                LANGUAGE_ARABIC: "في انتظار الجلسة",
                LANGUAGE_ENGLISH: "waiting for hearing",
            }
        ),
        "closed": MappingProxyType(
            {LANGUAGE_FRENCH: "clôturé", LANGUAGE_ARABIC: "مغلق", LANGUAGE_ENGLISH: "closed"}
        ),
        "archived": MappingProxyType(
            {LANGUAGE_FRENCH: "archivé", LANGUAGE_ARABIC: "مؤرشف", LANGUAGE_ENGLISH: "archived"}
        ),
    }
)

_PRIORITY_LABELS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "low": MappingProxyType(
            {LANGUAGE_FRENCH: "basse", LANGUAGE_ARABIC: "منخفضة", LANGUAGE_ENGLISH: "low"}
        ),
        "medium": MappingProxyType(
            {LANGUAGE_FRENCH: "moyenne", LANGUAGE_ARABIC: "متوسطة", LANGUAGE_ENGLISH: "medium"}
        ),
        "high": MappingProxyType(
            {LANGUAGE_FRENCH: "haute", LANGUAGE_ARABIC: "عالية", LANGUAGE_ENGLISH: "high"}
        ),
        "urgent": MappingProxyType(
            {LANGUAGE_FRENCH: "urgente", LANGUAGE_ARABIC: "عاجلة", LANGUAGE_ENGLISH: "urgent"}
        ),
    }
)

_ROLE_LABELS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "administrator": MappingProxyType(
            {
                LANGUAGE_FRENCH: "administrateur",
                LANGUAGE_ARABIC: "مدير",
                LANGUAGE_ENGLISH: "administrator",
            }
        ),
        "lawyer": MappingProxyType(
            {LANGUAGE_FRENCH: "avocat", LANGUAGE_ARABIC: "محامٍ", LANGUAGE_ENGLISH: "lawyer"}
        ),
        "court_representative": MappingProxyType(
            {
                LANGUAGE_FRENCH: "représentant du tribunal",
                LANGUAGE_ARABIC: "ممثل المحكمة",
                LANGUAGE_ENGLISH: "court representative",
            }
        ),
    }
)

#: Context keys whose values are rendered through a label map before being
#: interpolated. Everything else is interpolated as it was stored.
_LABELLED_KEYS: Mapping[str, Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        "status": _STATUS_LABELS,
        "previous_status": _STATUS_LABELS,
        "priority": _PRIORITY_LABELS,
        "previous_priority": _PRIORITY_LABELS,
        "role": _ROLE_LABELS,
    }
)


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class NotificationRule:
    """Everything the platform knows about turning one event into a notification.

    A rule is looked up rather than branched on, which is the whole reason
    :meth:`~services.notification_events.NotificationEventSubscriber.handle` has
    no ``if event.event_type is …`` in it: an event with no rule produces no
    notification and is not an error, and an event with a rule produces exactly
    the notification the table describes.
    """

    #: Stable identifier for the wording. Usually the event type's own value, and
    #: separate from it so that one event can produce different notifications for
    #: different audiences (a case assignment notifies the assignee that they were
    #: assigned, and nobody else at all) or different wording under different
    #: conditions (a status change *to* "waiting for hearing" is hearing news).
    key: str

    category: NotificationCategory
    notification_type: NotificationType
    priority: NotificationPriority
    preference: NotificationPreferenceKey
    audience: NotificationAudience
    template: NotificationTemplate

    #: Which resource this notification opens, if any. Resolved against the
    #: event by :func:`target_for`.
    target_type: NotificationTargetType | None = None

    #: Payload keys carried into the stored context, and therefore available to
    #: the template. Listed explicitly rather than copying the whole payload: a
    #: context is persisted, and *"payloads should remain lightweight"* applies at
    #: least as strongly to something that is kept.
    context_keys: tuple[str, ...] = ()


def _rule(
    key: str,
    *,
    category: NotificationCategory,
    notification_type: NotificationType = NotificationType.INFORMATION,
    priority: NotificationPriority = NotificationPriority.NORMAL,
    preference: NotificationPreferenceKey,
    audience: NotificationAudience = NotificationAudience.CASE_PARTICIPANTS,
    target_type: NotificationTargetType | None = None,
    context_keys: tuple[str, ...] = (),
    fr: tuple[str, str],
    ar: tuple[str, str],
    en: tuple[str, str],
) -> NotificationRule:
    """Build one rule. Keyword-only past the key, so a table entry reads as prose."""
    return NotificationRule(
        key=key,
        category=category,
        notification_type=notification_type,
        priority=priority,
        preference=preference,
        audience=audience,
        target_type=target_type,
        context_keys=context_keys,
        template=_template(fr=fr, ar=ar, en=en),
    )


# --- Rules that are not a plain event lookup ------------------------------- #
#
# Three events produce more than one notification shape, and each is declared as
# its own rule so the wording, the category, and the preference can differ. The
# refinement itself is one function, :func:`rule_for`, and it is the only place
# in the feature that looks at a payload to decide anything.

#: A case assignment, seen by the person who was assigned.
RULE_CASE_ASSIGNED = _rule(
    "case.assigned",
    category=NotificationCategory.CASE,
    priority=NotificationPriority.HIGH,
    preference=NotificationPreferenceKey.CASE_UPDATES,
    audience=NotificationAudience.ASSIGNEE,
    target_type=NotificationTargetType.CASE,
    context_keys=("case_number",),
    fr=("Dossier attribué", "Vous avez été affecté(e) au dossier {case_number}."),
    ar=("تم إسناد ملف إليك", "تم تعيينك في الملف {case_number}."),
    en=("Case assigned", "You were assigned to case {case_number}."),
)

#: The same event, seen by the person who was removed from the case.
RULE_CASE_UNASSIGNED = _rule(
    "case.unassigned",
    category=NotificationCategory.CASE,
    notification_type=NotificationType.WARNING,
    priority=NotificationPriority.HIGH,
    preference=NotificationPreferenceKey.CASE_UPDATES,
    audience=NotificationAudience.ASSIGNEE,
    # No target: by the time this is read the case is no longer theirs, so
    # offering to open it would offer a 403.
    context_keys=("case_number",),
    fr=("Affectation retirée", "Vous n'êtes plus affecté(e) au dossier {case_number}."),
    ar=("تم إلغاء إسنادك", "لم تعد معينًا في الملف {case_number}."),
    en=("Assignment removed", "You are no longer assigned to case {case_number}."),
)

#: A case update that touched the court-facing fields.
RULE_HEARING_UPDATED = _rule(
    "hearing.updated",
    category=NotificationCategory.HEARING,
    preference=NotificationPreferenceKey.HEARING_UPDATES,
    target_type=NotificationTargetType.CASE,
    context_keys=("case_number",),
    fr=("Audience mise à jour", "Les informations d'audience du dossier {case_number} ont changé."),
    ar=("تحديث الجلسة", "تم تغيير معلومات الجلسة في الملف {case_number}."),
    en=("Hearing updated", "Hearing information changed on case {case_number}."),
)

#: A status change *into* "waiting for hearing", which is hearing news rather
#: than lifecycle news and is what a lawyer actually wants to be told about.
RULE_HEARING_SCHEDULED = _rule(
    "hearing.scheduled",
    category=NotificationCategory.HEARING,
    priority=NotificationPriority.HIGH,
    preference=NotificationPreferenceKey.HEARING_UPDATES,
    target_type=NotificationTargetType.CASE,
    context_keys=("case_number", "status", "previous_status"),
    fr=("Audience à venir", "Le dossier {case_number} est désormais en attente d'audience."),
    ar=("جلسة قادمة", "أصبح الملف {case_number} في انتظار الجلسة."),
    en=("Hearing awaited", "Case {case_number} is now waiting for a hearing."),
)


#: Field labels that mark a case update as *hearing* news.
#:
#: :mod:`services.case` publishes ``case.updated`` with the **labels** of the
#: fields that changed, and knows nothing about notifications — which is exactly
#: the isolation the spec requires and exactly why this set has to live here
#: rather than being a flag on the event.
#:
#: It **fails soft by construction**: a label renamed in the case service simply
#: stops matching, and the update is notified as ordinary case news under
#: :data:`EVENT_RULES`. That is a slightly less specific notification, never a
#: missing or a wrong one — which is the only acceptable failure mode for a rule
#: derived from another module's wording.
HEARING_FIELD_LABELS: frozenset[str] = frozenset({"court", "filing date", "next hearing date"})


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #

#: Which events become notifications, and what each one becomes.
#:
#: **This is the whole of the feature's subscription list.** An event absent from
#: it produces nothing, silently and correctly — the subscriber receives every
#: event the platform publishes and acts on the ones named here.
#:
#: What is deliberately **absent**, because each omission is a decision rather
#: than a gap:
#:
#: * ``document.updated`` — a category or description edit. The timeline records
#:   it; interrupting everyone on the matter for it is how a notification feed
#:   becomes something people stop reading.
#: * ``ocr.started`` / ``indexing.*`` — pipeline stages nobody asked for.
#:   ``16-notifications.md`` names OCR completion and failure and stops there,
#:   and indexing is a stage below the one a lawyer thinks in.
#: * ``report.started`` / ``report.progress`` — the author is watching a progress
#:   bar that is already live over the channel. A notification per section would
#:   be a dozen per report.
#: * ``timeline.updated`` — the timeline is *derived* from the same business
#:   changes these rules already cover, so subscribing to it would notify
#:   everything twice.
#: * ``presence.changed`` — presence visualization is out of scope
#:   (``15-real-time-synchronization.md``), and "someone opened a second tab" is
#:   not news.
#: * ``user.deactivated`` — and this one is the most instructive omission of the
#:   set, because the rule was written before it was removed. A deactivated
#:   account cannot sign in, so the notification would be created for somebody
#:   with no way to read it, and it would still be sitting there if the account
#:   were ever reinstated. ``16-notifications.md`` names account *activation*, a
#:   password reset, and a role change — and not this — which turns out to be
#:   exactly right. The **event** is still published by :mod:`services.user`, so
#:   a future email channel has it: telling somebody their access was suspended
#:   is a message for a channel they can still reach.
#: * ``notification.*`` — this module's own events. Their absence is what makes a
#:   feedback loop impossible rather than merely unlikely.
EVENT_RULES: Mapping[DomainEventType, NotificationRule] = MappingProxyType(
    {
        # --- Case management ------------------------------------------------ #
        DomainEventType.CASE_CREATED: _rule(
            "case.created",
            category=NotificationCategory.CASE,
            preference=NotificationPreferenceKey.CASE_UPDATES,
            target_type=NotificationTargetType.CASE,
            context_keys=("case_number",),
            fr=("Nouveau dossier", "Le dossier {case_number} a été créé."),
            ar=("ملف جديد", "تم إنشاء الملف {case_number}."),
            en=("New case", "Case {case_number} was created."),
        ),
        DomainEventType.CASE_UPDATED: _rule(
            "case.updated",
            category=NotificationCategory.CASE,
            preference=NotificationPreferenceKey.CASE_UPDATES,
            target_type=NotificationTargetType.CASE,
            context_keys=("case_number",),
            fr=("Dossier mis à jour", "Le dossier {case_number} a été mis à jour."),
            ar=("تم تحديث الملف", "تم تحديث الملف {case_number}."),
            en=("Case updated", "Case {case_number} was updated."),
        ),
        DomainEventType.CASE_STATUS_CHANGED: _rule(
            "case.status_changed",
            category=NotificationCategory.CASE,
            preference=NotificationPreferenceKey.CASE_UPDATES,
            target_type=NotificationTargetType.CASE,
            context_keys=("case_number", "status", "previous_status"),
            fr=(
                "Statut modifié",
                "Le dossier {case_number} est passé de « {previous_status} » à « {status} ».",
            ),
            ar=(
                "تغيّرت الحالة",
                "انتقل الملف {case_number} من «{previous_status}» إلى «{status}».",
            ),
            en=(
                "Status changed",
                "Case {case_number} moved from “{previous_status}” to “{status}”.",
            ),
        ),
        DomainEventType.CASE_PRIORITY_CHANGED: _rule(
            "case.priority_changed",
            category=NotificationCategory.CASE,
            preference=NotificationPreferenceKey.CASE_UPDATES,
            target_type=NotificationTargetType.CASE,
            context_keys=("case_number", "priority", "previous_priority"),
            fr=(
                "Priorité modifiée",
                "La priorité du dossier {case_number} est désormais {priority}.",
            ),
            ar=("تغيّرت الأولوية", "أصبحت أولوية الملف {case_number} {priority}."),
            en=("Priority changed", "Case {case_number} is now {priority} priority."),
        ),
        DomainEventType.CASE_ARCHIVED: _rule(
            "case.archived",
            category=NotificationCategory.CASE,
            notification_type=NotificationType.WARNING,
            preference=NotificationPreferenceKey.CASE_UPDATES,
            target_type=NotificationTargetType.CASE,
            context_keys=("case_number",),
            fr=("Dossier archivé", "Le dossier {case_number} a été archivé."),
            ar=("تمت أرشفة الملف", "تمت أرشفة الملف {case_number}."),
            en=("Case archived", "Case {case_number} was archived."),
        ),
        DomainEventType.CASE_RESTORED: _rule(
            "case.restored",
            category=NotificationCategory.CASE,
            notification_type=NotificationType.SUCCESS,
            preference=NotificationPreferenceKey.CASE_UPDATES,
            target_type=NotificationTargetType.CASE,
            context_keys=("case_number", "status"),
            fr=("Dossier restauré", "Le dossier {case_number} a été restauré."),
            ar=("تمت استعادة الملف", "تمت استعادة الملف {case_number}."),
            en=("Case restored", "Case {case_number} was restored."),
        ),
        # Refined by `rule_for` into RULE_CASE_ASSIGNED or RULE_CASE_UNASSIGNED.
        # The entry here is the default and is what a future assignment event
        # carrying neither flag would fall back to.
        DomainEventType.CASE_ASSIGNMENT_CHANGED: RULE_CASE_ASSIGNED,
        # --- Documents ------------------------------------------------------ #
        DomainEventType.DOCUMENT_UPLOADED: _rule(
            "document.uploaded",
            category=NotificationCategory.DOCUMENT,
            preference=NotificationPreferenceKey.DOCUMENT_UPDATES,
            target_type=NotificationTargetType.DOCUMENT,
            context_keys=("case_number", "category", "file_extension"),
            fr=("Nouveau document", "Un document a été ajouté au dossier {case_number}."),
            ar=("مستند جديد", "تمت إضافة مستند إلى الملف {case_number}."),
            en=("New document", "A document was added to case {case_number}."),
        ),
        DomainEventType.DOCUMENT_REPLACED: _rule(
            "document.replaced",
            category=NotificationCategory.DOCUMENT,
            preference=NotificationPreferenceKey.DOCUMENT_UPDATES,
            target_type=NotificationTargetType.DOCUMENT,
            context_keys=("case_number", "category", "version"),
            fr=(
                "Document remplacé",
                "Un document du dossier {case_number} a une nouvelle version (v{version}).",
            ),
            ar=("تم استبدال مستند", "أصبح لأحد مستندات الملف {case_number} إصدار جديد (v{version})."),
            en=(
                "Document replaced",
                "A document on case {case_number} has a new version (v{version}).",
            ),
        ),
        DomainEventType.DOCUMENT_DELETED: _rule(
            "document.deleted",
            category=NotificationCategory.DOCUMENT,
            notification_type=NotificationType.WARNING,
            preference=NotificationPreferenceKey.DOCUMENT_UPDATES,
            # No target: the document is withdrawn, so opening it would 404.
            context_keys=("case_number", "category"),
            fr=("Document retiré", "Un document a été retiré du dossier {case_number}."),
            ar=("تم سحب مستند", "تم سحب مستند من الملف {case_number}."),
            en=("Document removed", "A document was removed from case {case_number}."),
        ),
        # --- OCR ------------------------------------------------------------- #
        DomainEventType.OCR_COMPLETED: _rule(
            "ocr.completed",
            category=NotificationCategory.OCR,
            notification_type=NotificationType.SUCCESS,
            # Low, deliberately: extraction succeeding is the pipeline working as
            # designed. It is worth being able to see and not worth interrupting
            # anyone for, which is exactly what a priority is for.
            priority=NotificationPriority.LOW,
            preference=NotificationPreferenceKey.OCR_COMPLETION,
            target_type=NotificationTargetType.DOCUMENT,
            context_keys=("case_number", "page_count"),
            fr=(
                "Texte extrait",
                "Le texte d'un document du dossier {case_number} est maintenant disponible "
                "({page_count} page(s)).",
            ),
            ar=(
                "تم استخراج النص",
                "أصبح نص أحد مستندات الملف {case_number} متاحًا ({page_count} صفحة).",
            ),
            en=(
                "Text extracted",
                "Text from a document on case {case_number} is now available "
                "({page_count} page(s)).",
            ),
        ),
        DomainEventType.OCR_FAILED: _rule(
            "ocr.failed",
            category=NotificationCategory.OCR,
            notification_type=NotificationType.ERROR,
            priority=NotificationPriority.HIGH,
            preference=NotificationPreferenceKey.OCR_COMPLETION,
            target_type=NotificationTargetType.DOCUMENT,
            # The machine-readable failure *code*, never a library's message —
            # which can echo the text it was processing. The same line
            # `models/report.py` draws for `error_message`.
            context_keys=("case_number", "error_code"),
            fr=(
                "Échec de l'extraction",
                "L'extraction du texte d'un document du dossier {case_number} a échoué. "
                "Vous pouvez la relancer.",
            ),
            ar=(
                "فشل استخراج النص",
                "فشل استخراج نص أحد مستندات الملف {case_number}. يمكنك إعادة المحاولة.",
            ),
            en=(
                "Text extraction failed",
                "Text extraction failed for a document on case {case_number}. "
                "You can retry it.",
            ),
        ),
        # --- AI reports ------------------------------------------------------ #
        DomainEventType.REPORT_GENERATED: _rule(
            "report.generated",
            category=NotificationCategory.REPORT,
            notification_type=NotificationType.SUCCESS,
            preference=NotificationPreferenceKey.AI_REPORT_COMPLETION,
            audience=NotificationAudience.RESOURCE_OWNER,
            target_type=NotificationTargetType.REPORT,
            context_keys=("case_number", "report_type"),
            fr=("Rapport prêt", "Votre rapport sur le dossier {case_number} est prêt."),
            ar=("التقرير جاهز", "أصبح تقريرك عن الملف {case_number} جاهزًا."),
            en=("Report ready", "Your report on case {case_number} is ready."),
        ),
        DomainEventType.REPORT_FAILED: _rule(
            "report.failed",
            category=NotificationCategory.REPORT,
            notification_type=NotificationType.ERROR,
            priority=NotificationPriority.HIGH,
            preference=NotificationPreferenceKey.AI_REPORT_COMPLETION,
            audience=NotificationAudience.RESOURCE_OWNER,
            target_type=NotificationTargetType.REPORT,
            context_keys=("case_number", "report_type", "error_code"),
            fr=(
                "Échec de la génération",
                "La génération de votre rapport sur le dossier {case_number} a échoué. "
                "Vous pouvez la relancer.",
            ),
            ar=(
                "فشل إنشاء التقرير",
                "فشل إنشاء تقريرك عن الملف {case_number}. يمكنك إعادة المحاولة.",
            ),
            en=(
                "Report generation failed",
                "Generating your report on case {case_number} failed. You can retry it.",
            ),
        ),
        # --- Account --------------------------------------------------------- #
        DomainEventType.USER_ACTIVATED: _rule(
            "user.activated",
            category=NotificationCategory.USER,
            notification_type=NotificationType.SUCCESS,
            preference=NotificationPreferenceKey.ACCOUNT_ACTIVITY,
            audience=NotificationAudience.SUBJECT_USER,
            target_type=NotificationTargetType.ACCOUNT,
            fr=("Compte activé", "Votre compte est de nouveau actif."),
            ar=("تم تفعيل الحساب", "أصبح حسابك نشطًا من جديد."),
            en=("Account activated", "Your account is active again."),
        ),
        DomainEventType.USER_PASSWORD_RESET: _rule(
            "user.password_reset",
            category=NotificationCategory.USER,
            notification_type=NotificationType.WARNING,
            # The only CRITICAL rule the platform ships. A password reset the
            # account holder did not ask for is the one notification here whose
            # whole value is being seen immediately.
            priority=NotificationPriority.CRITICAL,
            preference=NotificationPreferenceKey.ACCOUNT_ACTIVITY,
            audience=NotificationAudience.SUBJECT_USER,
            target_type=NotificationTargetType.ACCOUNT,
            fr=(
                "Mot de passe réinitialisé",
                "Un administrateur a réinitialisé votre mot de passe et vos sessions ont été "
                "fermées. Si vous ne l'avez pas demandé, signalez-le immédiatement.",
            ),
            ar=(
                "تمت إعادة تعيين كلمة المرور",
                "قام أحد المديرين بإعادة تعيين كلمة مرورك وتم إنهاء جلساتك. "
                "إذا لم تطلب ذلك، فأبلغ عنه فورًا.",
            ),
            en=(
                "Password reset",
                "An administrator reset your password and signed out your sessions. "
                "If you did not request this, report it immediately.",
            ),
        ),
        DomainEventType.USER_ROLE_CHANGED: _rule(
            "user.role_changed",
            category=NotificationCategory.USER,
            priority=NotificationPriority.HIGH,
            preference=NotificationPreferenceKey.ACCOUNT_ACTIVITY,
            audience=NotificationAudience.SUBJECT_USER,
            target_type=NotificationTargetType.ACCOUNT,
            context_keys=("role", "previous_role"),
            fr=("Rôle modifié", "Votre rôle sur la plateforme est désormais {role}."),
            ar=("تغيّر دورك", "أصبح دورك على المنصة {role}."),
            en=("Role changed", "Your role on the platform is now {role}."),
        ),
    }
)


# --------------------------------------------------------------------------- #
# System announcements
# --------------------------------------------------------------------------- #
#
# Two rules that are **not** in the table above, and their absence from it is the
# one place this feature departs from "every notification comes from a domain
# event".
#
# The reason is structural rather than a preference. `core/events.py` states that
# there is deliberately **no broadcast scope**: every event the platform carries
# is about something somebody owns, because a scope with no owner is a scope with
# no authorization rule. A platform-wide announcement has no owner and no
# resource, so there is no topic it could be published on and no rule that could
# authorize it — and inventing one would put a channel into the event system that
# every future publisher could reach.
#
# So an announcement enters through the **Notification Service's own API**
# (`POST /notifications/announcements`, gated on `notifications:manage`), which is
# the service creating notifications rather than a business module doing it. The
# spec's rule — *"business logic should never create notifications directly"* —
# is untouched: there is no business module involved at all.
#
# It still travels the ordinary path from there: persisted, then published on
# each recipient's own `user:<id>` topic like every other notification, so
# delivery, deduplication, preferences, and read state are the same code.


class AnnouncementKind(StrEnum):
    """Which of the spec's two system notifications an announcement is."""

    #: Planned unavailability. A warning, because it asks the reader to do
    #: something (finish and save) before a deadline.
    MAINTENANCE = "maintenance"
    #: Everything else the platform wants to say.
    ANNOUNCEMENT = "announcement"


#: The announcement rules, keyed by kind.
#:
#: Their templates interpolate ``{message}`` — the one place in this feature
#: where a human's words become a notification's body. It is bounded by
#: :data:`MAX_MESSAGE_LENGTH`, screened like any other context value, and written
#: by an administrator holding ``notifications:manage`` rather than by any
#: business path, which is what keeps it from being a way to smuggle content into
#: everyone's feed.
ANNOUNCEMENT_RULES: Mapping[AnnouncementKind, NotificationRule] = MappingProxyType(
    {
        AnnouncementKind.MAINTENANCE: _rule(
            "system.maintenance",
            category=NotificationCategory.SYSTEM,
            notification_type=NotificationType.WARNING,
            priority=NotificationPriority.HIGH,
            preference=NotificationPreferenceKey.SYSTEM_ANNOUNCEMENTS,
            audience=NotificationAudience.SUBJECT_USER,
            context_keys=("message",),
            fr=("Maintenance planifiée", "{message}"),
            ar=("صيانة مُبرمجة", "{message}"),
            en=("Scheduled maintenance", "{message}"),
        ),
        AnnouncementKind.ANNOUNCEMENT: _rule(
            "system.announcement",
            category=NotificationCategory.SYSTEM,
            preference=NotificationPreferenceKey.SYSTEM_ANNOUNCEMENTS,
            audience=NotificationAudience.SUBJECT_USER,
            context_keys=("message",),
            fr=("Annonce", "{message}"),
            ar=("إعلان", "{message}"),
            en=("Announcement", "{message}"),
        ),
    }
)


#: Every rule the platform can produce, by key. Used to render a stored row.
#:
#: Built from the two tables above rather than maintained by hand, so a rule that
#: exists cannot fail to be renderable — which would otherwise be a notification
#: created today and unreadable tomorrow.
RULES_BY_KEY: Mapping[str, NotificationRule] = MappingProxyType(
    {
        rule.key: rule
        for rule in (
            *EVENT_RULES.values(),
            *ANNOUNCEMENT_RULES.values(),
            RULE_CASE_ASSIGNED,
            RULE_CASE_UNASSIGNED,
            RULE_HEARING_UPDATED,
            RULE_HEARING_SCHEDULED,
        )
    }
)


# --------------------------------------------------------------------------- #
# Deriving a rule from an event
# --------------------------------------------------------------------------- #


def rule_for(
    event_type: DomainEventType, payload: Mapping[str, Any] | None = None
) -> NotificationRule | None:
    """The rule one event produces, or ``None`` if it produces no notification.

    **The only function in this feature that reads a payload to decide
    anything**, and it reads exactly three things — whether an assignment added
    or removed somebody, whether a case update touched the court fields, and
    whether a status change moved a case to *waiting for hearing*. Each of those
    is a *refinement* of an event the publishing module emits without knowing
    notifications exist, and each degrades to the table's default entry when the
    payload does not say: a slightly less specific notification rather than a
    missing one.

    Args:
        event_type: the published event's type.
        payload: its (already screened) payload.

    Returns:
        The rule, or ``None`` when the platform sends nothing for this event.
    """
    rule = EVENT_RULES.get(event_type)
    if rule is None:
        return None

    values = payload or {}

    if event_type is DomainEventType.CASE_ASSIGNMENT_CHANGED:
        assigned = values.get("assigned")
        if assigned is False:
            return RULE_CASE_UNASSIGNED
        return RULE_CASE_ASSIGNED

    if event_type is DomainEventType.CASE_UPDATED:
        fields = values.get("fields")
        if isinstance(fields, list | tuple) and HEARING_FIELD_LABELS.intersection(
            str(entry) for entry in fields
        ):
            return RULE_HEARING_UPDATED
        return rule

    if event_type is DomainEventType.CASE_STATUS_CHANGED:
        if values.get("status") == "waiting_for_hearing":
            return RULE_HEARING_SCHEDULED
        return rule

    return rule


def target_for(
    rule: NotificationRule,
    *,
    case_id: uuid.UUID | None,
    payload: Mapping[str, Any] | None = None,
) -> NotificationTarget | None:
    """Resolve which resource a notification opens.

    Returns ``None`` when the rule names no target — a withdrawn document and a
    case somebody was just removed from both deliberately have none, because
    offering to open either would offer a refusal.
    """
    if rule.target_type is None:
        return None

    values = payload or {}

    if rule.target_type is NotificationTargetType.ACCOUNT:
        return NotificationTarget(target_type=NotificationTargetType.ACCOUNT)

    if rule.target_type is NotificationTargetType.CASE:
        return (
            NotificationTarget(NotificationTargetType.CASE, case_id)
            if case_id is not None
            else None
        )

    key = "document_id" if rule.target_type is NotificationTargetType.DOCUMENT else "report_id"
    resource_id = _as_uuid(values.get(key))
    return (
        NotificationTarget(rule.target_type, resource_id) if resource_id is not None else None
    )


def _as_uuid(value: Any) -> uuid.UUID | None:
    """Read an identifier out of a payload, tolerating one that is not there.

    Payload values are JSON-safe by the time they reach here — the dispatcher
    renders a ``UUID`` as a string — so this is a parse rather than a cast.
    """
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


class InvalidNotificationContextError(ValueError):
    """A context cannot be represented as a bounded, JSON-safe object."""


def normalize_context(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, JSON-safe copy of the values a template interpolates.

    The same three rules :func:`~core.events.normalize_payload` applies, applied
    to something that is *stored* rather than merely sent: flat, bounded, and
    JSON-safe. There is no forbidden-key screen here and there does not need to
    be one — a context is built from :attr:`NotificationRule.context_keys`, which
    is a closed list written in this module, so nothing a publisher invents can
    reach it in the first place.

    Over-long strings are **clipped rather than refused**, which is the opposite
    of the dispatcher's choice and deliberate: an event that cannot be built is
    an event nobody misses, while a notification that cannot be built is a person
    not being told something happened.
    """
    if not values:
        return {}

    normalized: dict[str, Any] = {}
    for raw_key, value in list(values.items())[:MAX_CONTEXT_KEYS]:
        rendered = _normalize_context_value(value)
        if rendered is not None:
            normalized[str(raw_key)] = rendered
    return normalized


def _normalize_context_value(value: Any) -> Any:
    """Render one context value, or ``None`` to drop it."""
    if value is None:
        return None
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, str):
        return value[:MAX_CONTEXT_VALUE_LENGTH]
    # Lists, dicts, and anything else are dropped rather than rendered: a
    # template interpolates a word, and `str(["a", "b"])` in the middle of a
    # sentence is a bug shown to a user.
    return None


def build_context(rule: NotificationRule, payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Collect the payload values ``rule`` interpolates, and nothing else."""
    values = payload or {}
    return normalize_context(
        {key: values[key] for key in rule.context_keys if key in values}
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def resolve_notification_language(requested: str | None) -> str:
    """Decide which language a notification is read in.

    The same resolver shape as :func:`~core.reports.resolve_report_language`, and
    for the same reason there is no detection step: there is no free text to
    detect from, only an explicit choice to honour and French to fall back to.
    """
    if requested:
        wanted = requested.strip().lower()
        if wanted in SUPPORTED_ANSWER_LANGUAGES:
            return wanted
    return LANGUAGE_FRENCH


@dataclass(frozen=True, slots=True)
class RenderedNotification:
    """One notification's wording, in one language."""

    title: str
    message: str


def render_notification(
    *,
    rule_key: str,
    category: NotificationCategory | str,
    context: Mapping[str, Any] | None = None,
    language: str | None = None,
) -> RenderedNotification:
    """Render a stored notification's title and message.

    Never raises, and that is its contract rather than a courtesy: this runs
    while serving somebody's notification list, and one row written by a version
    of the platform that had a rule this one does not must not take the page down
    with it. An unknown rule falls back to its category's generic wording, an
    unknown category to the platform's, and a missing context value to
    :data:`MISSING_VALUE`.
    """
    resolved = resolve_notification_language(language)
    rule = RULES_BY_KEY.get(rule_key)

    if rule is not None:
        template = rule.template
    else:
        template = _FALLBACK_TEMPLATES.get(
            _as_category(category), _FALLBACK_TEMPLATES[NotificationCategory.SYSTEM]
        )

    values = _DefaultingContext(_localized_context(context, resolved))
    return RenderedNotification(
        title=_render(template.title(resolved), values, limit=MAX_TITLE_LENGTH),
        message=_render(template.message(resolved), values, limit=MAX_MESSAGE_LENGTH),
    )


def _as_category(value: NotificationCategory | str) -> NotificationCategory:
    """Read a stored category, tolerating one this version does not define."""
    if isinstance(value, NotificationCategory):
        return value
    try:
        return NotificationCategory(value)
    except ValueError:
        return NotificationCategory.SYSTEM


def _localized_context(context: Mapping[str, Any] | None, language: str) -> dict[str, Any]:
    """Translate the values that have label maps; pass everything else through."""
    rendered: dict[str, Any] = {}
    for key, value in (context or {}).items():
        labels = _LABELLED_KEYS.get(key)
        if labels is not None and isinstance(value, str):
            translations = labels.get(value)
            rendered[key] = (
                translations.get(language, value) if translations is not None else value
            )
            continue
        rendered[key] = value
    return rendered


def _render(template: str, values: Mapping[str, Any], *, limit: int) -> str:
    """Interpolate one string, clipping it to ``limit``.

    ``format_map`` over a defaulting mapping rather than ``format``: the second
    raises on a key the template mentions and the context lacks, which would turn
    a publisher dropping a field into an exception on somebody's notification
    page. A stray brace in a template is a programming error and is allowed to
    raise here, because it fails on the first render in the first test.
    """
    try:
        rendered = template.format_map(values)
    except (IndexError, ValueError):  # pragma: no cover - malformed template literal
        rendered = template
    return rendered[:limit]


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #

#: Separator inside a deduplication key. Not a colon, so a key cannot be confused
#: with a topic or a permission identifier in a log line.
_DEDUPE_SEPARATOR: Final[str] = "|"


def dedupe_key(
    *,
    rule_key: str,
    case_id: uuid.UUID | None = None,
    target: NotificationTarget | None = None,
    discriminator: str | None = None,
) -> str:
    """A stable identifier for "this same notification, again".

    ``16-notifications.md`` requires both that *"duplicate events"* be handled
    and that *"duplicate notifications"* be avoided, and those are two different
    problems that need two different mechanisms — the same shape
    ``10-document-indexing.md`` records for its own idempotency:

    * **the same dispatched event** reaching the subscriber twice is caught by a
      unique index on ``(recipient_id, event_id)``, because an event's identity
      is assigned once by the dispatcher and never reused;
    * **a semantically identical notification** — a worker retried, a user
      double-clicked, two writes produced the same change — is caught by *this*
      key inside a bounded window (``NOTIFICATION_DEDUPE_WINDOW_SECONDS``).

    The window is what makes this a suppression rather than a ban: a case
    genuinely updated twice in a week is two notifications, and the same case
    updated twice in ten seconds is one. A unique constraint on this key would
    silence the first case forever, which is why it is a query rather than an
    index.

    Hashed rather than concatenated, so the stored value is fixed-width and — the
    point that matters — **carries no identifiers**: a deduplication key sitting
    in a log line or an index dump would otherwise be a case identifier in plain
    text.
    """
    parts = [
        rule_key,
        str(case_id) if case_id is not None else "",
        target.target_type.value if target is not None else "",
        str(target.target_id) if target is not None and target.target_id else "",
        discriminator or "",
    ]
    digest = hashlib.sha256(_DEDUPE_SEPARATOR.join(parts).encode("utf-8")).hexdigest()
    return f"{rule_key}{_DEDUPE_SEPARATOR}{digest[:32]}"


# --------------------------------------------------------------------------- #
# The specification a service is handed
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class NotificationSpec:
    """One notification, fully decided and not yet persisted.

    The value :mod:`services.notification_events` produces and
    :class:`~services.notification.NotificationService` consumes. It exists so
    that *deciding* what to create and *creating* it are two testable halves —
    the first against no database at all.
    """

    recipient_id: uuid.UUID
    rule: NotificationRule
    context: Mapping[str, Any] = field(default_factory=dict)
    case_id: uuid.UUID | None = None
    actor_id: uuid.UUID | None = None
    target: NotificationTarget | None = None
    #: The dispatched event this came from, when it came from one. ``None`` for a
    #: system announcement, which has no event.
    event_id: uuid.UUID | None = None
    event_type: str | None = None
    #: When the originating event happened, used to measure delivery latency
    #: against what the platform is actually responsible for.
    occurred_at: datetime | None = None
    dedupe: str = ""


__all__ = [
    "ANNOUNCEMENT_RULES",
    "DEFAULT_PREFERENCES",
    "EVENT_RULES",
    "HEARING_FIELD_LABELS",
    "MAX_CONTEXT_KEYS",
    "MAX_CONTEXT_VALUE_LENGTH",
    "MAX_MESSAGE_LENGTH",
    "MAX_TITLE_LENGTH",
    "MISSING_VALUE",
    "PRIORITY_RANK",
    "RULES_BY_KEY",
    "RULE_CASE_ASSIGNED",
    "RULE_CASE_UNASSIGNED",
    "RULE_HEARING_SCHEDULED",
    "RULE_HEARING_UPDATED",
    "AnnouncementKind",
    "InvalidNotificationContextError",
    "NotificationAudience",
    "NotificationCategory",
    "NotificationPreferenceKey",
    "NotificationPriority",
    "NotificationRule",
    "NotificationSpec",
    "NotificationTarget",
    "NotificationTargetType",
    "NotificationTemplate",
    "NotificationType",
    "RenderedNotification",
    "build_context",
    "dedupe_key",
    "normalize_context",
    "preference_from_value",
    "render_notification",
    "resolve_notification_language",
    "rule_for",
    "target_for",
]
