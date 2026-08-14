"""Notification request and response schemas.

Two responsibilities, both required by ``code-standards.md`` ("validate every
request using Pydantic models", "return standardized API response structures"):

* **Validation** — page bounds, the category / type / priority enums, the bulk
  read ceiling, and the announcement length limit are enforced here, so routes
  stay thin and every rejection comes back in the same envelope.
* **Serialization** — a stored notification carries no prose (see
  :mod:`models.notification`), so :class:`NotificationRead` is built by
  *rendering* one in the reader's language rather than by validating the row
  attribute-for-attribute. That is why this module has an explicit
  :meth:`NotificationRead.from_row` rather than relying on
  ``model_validate``: the title and the message do not exist until somebody asks
  for them in a language.

Business rules that need the database — which events become notifications, who
receives them, whether a preference silences one — live in
:mod:`core.notifications`, :mod:`services.notification_recipients`, and
:mod:`services.notification` and cannot be expressed here.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.config import settings
from core.notifications import (
    MAX_MESSAGE_LENGTH,
    MAX_TITLE_LENGTH,
    AnnouncementKind,
    NotificationCategory,
    NotificationPreferenceKey,
    NotificationPriority,
    NotificationType,
    render_notification,
    resolve_notification_language,
)
from models.notification import Notification
from schemas.case import SortOrder

# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class NotificationSortField(StrEnum):
    """Columns the notification feed may be ordered by.

    An allow-list rather than a free-text column name: the value reaches an
    ``ORDER BY``, and an enum is what makes that safe by construction rather than
    by escaping.

    Deliberately short. A feed is read newest-first, and the only other ordering
    anybody wants is *most urgent first* — which is why ``priority`` sorts by
    :data:`~core.notifications.PRIORITY_RANK` in the repository rather than by
    its stored value, exactly as a case's priority does.
    """

    CREATED_AT = "created_at"
    PRIORITY = "priority"


class NotificationListQuery(BaseModel):
    """Validated query parameters for ``GET /notifications``.

    Note what is **not** here: there is no ``recipient_id`` filter. The feed is
    the caller's own by construction — every read in
    :mod:`repositories.notification` is keyed by them — so a filter naming a user
    would either be redundant or be a request the API must refuse, and offering
    it would suggest the second is possible. The same reasoning
    :class:`~schemas.report.ReportListQuery` records.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="Page number, 1-based.")
    page_size: int = Field(
        default=settings.NOTIFICATION_PAGE_SIZE,
        ge=1,
        le=settings.NOTIFICATION_MAX_PAGE_SIZE,
        description=f"Notifications per page (max {settings.NOTIFICATION_MAX_PAGE_SIZE}).",
    )
    unread_only: bool = Field(
        default=False,
        description="Only notifications the caller has not read yet. What the panel sends.",
    )
    category: NotificationCategory | None = Field(
        default=None, description="Only notifications in this category."
    )
    notification_type: NotificationType | None = Field(
        default=None, description="Only notifications of this type."
    )
    priority: NotificationPriority | None = Field(
        default=None, description="Only notifications at this priority."
    )
    case_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Only notifications about this case. What a case workspace sends, so it shows this "
            "matter's news rather than the platform's."
        ),
    )
    sort_by: NotificationSortField = Field(
        default=NotificationSortField.CREATED_AT, description="Column to order by."
    )
    sort_order: SortOrder = Field(
        default=SortOrder.DESC,
        description="Ordering direction. Newest first by default — a feed is read that way.",
    )
    language: str | None = Field(
        default=None,
        max_length=10,
        description=(
            "ISO 639-1 code to render the titles and messages in (`ar`, `fr`, or `en`). "
            "Notifications store no prose, so the whole history is rendered in whichever "
            "language is asked for rather than in the one that was current when each arrived."
        ),
    )


class NotificationMetricsQuery(BaseModel):
    """Validated query parameters for ``GET /notifications/metrics``."""

    model_config = ConfigDict(extra="forbid")

    window_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description=(
            "Only count notifications created in the last N days. Omitted covers the platform's "
            "whole history, which is the right default for a figure like 'notifications created'."
        ),
    )


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class NotificationReadRequest(BaseModel):
    """Body for ``PATCH /notifications/read``."""

    model_config = ConfigDict(extra="forbid")

    notification_ids: list[uuid.UUID] = Field(
        min_length=1,
        max_length=settings.NOTIFICATION_MAX_BULK_READ,
        description=(
            f"Notifications to mark as read (max {settings.NOTIFICATION_MAX_BULK_READ}). "
            "Identifiers not belonging to the caller are ignored rather than refused — a panel "
            "that marks what it has on screen must not fail because one row was archived "
            "underneath it."
        ),
    )

    @field_validator("notification_ids")
    @classmethod
    def _deduplicate(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        """Collapse repeats, preserving order.

        A client that sends the same identifier twice is not making an error
        worth refusing, and the ceiling above should bound *distinct* rows rather
        than however many times a list was concatenated.
        """
        seen: set[uuid.UUID] = set()
        unique: list[uuid.UUID] = []
        for entry in value:
            if entry not in seen:
                seen.add(entry)
                unique.append(entry)
        return unique


class NotificationReadAllRequest(BaseModel):
    """Body for ``PATCH /notifications/read-all``."""

    model_config = ConfigDict(extra="forbid")

    category: NotificationCategory | None = Field(
        default=None,
        description=(
            "Only mark this category as read. Omitted marks everything unread. Offered because a "
            "feed filtered to one category and a 'mark all as read' button that ignored the "
            "filter is the most reliable way to lose a notification somebody meant to keep."
        ),
    )


class NotificationPreferenceUpdate(BaseModel):
    """One preference the caller is changing, on any of the channels.

    **Every channel field is optional, and that is the compatibility story.** A
    client that sends only ``in_app`` — which is every client written before the
    email channel existed — leaves ``email`` and ``whatsapp`` exactly as they
    were, rather than switching them off by not mentioning them. That protection
    was written for the email channel and cost nothing when WhatsApp arrived,
    which is what it was for; it applies in the same way to whichever channel
    arrives next.
    """

    model_config = ConfigDict(extra="forbid")

    preference_key: NotificationPreferenceKey = Field(description="Which preference to set.")
    in_app: bool | None = Field(
        default=None,
        description=(
            "Whether in-app notifications of this kind are delivered. Omitted leaves the "
            "current value alone."
        ),
    )
    email: bool | None = Field(
        default=None,
        description=(
            "Whether emails of this kind are delivered. Omitted leaves the current value "
            "alone. Note that this only ever *narrows*: a notification kind the platform does "
            "not email is never emailed however this is set."
        ),
    )
    whatsapp: bool | None = Field(
        default=None,
        description=(
            "Whether WhatsApp messages of this kind are delivered. Omitted leaves the current "
            "value alone. It narrows twice over: a notification kind the platform does not "
            "send over WhatsApp never travels on that channel however this is set, and neither "
            "does anything at all for an account with no phone number on it."
        ),
    )

    @model_validator(mode="after")
    def _require_a_change(self) -> NotificationPreferenceUpdate:
        """Refuse an entry that changes nothing.

        A ``{"preference_key": "case_updates"}`` with no channel at all is almost
        certainly a client bug — a field name typo, a stale mapping — and
        accepting it silently would answer 200 while doing nothing, which is the
        hardest kind of failure to notice from the outside.

        A **model** validator rather than a field one, and the distinction is the
        whole point here: a field validator does not run for a field the request
        omitted, which is precisely the case this needs to catch.
        """
        if self.in_app is None and self.email is None and self.whatsapp is None:
            raise ValueError("Set at least one of `in_app`, `email`, or `whatsapp`.")
        return self


class NotificationPreferencesUpdate(BaseModel):
    """Body for ``PUT /notifications/preferences``.

    A list of the preferences being changed rather than the whole set, so two
    settings panels open at once cannot silently revert each other's saves — and
    so an eighth preference added later does not make an older client's payload
    invalid.
    """

    model_config = ConfigDict(extra="forbid")

    preferences: list[NotificationPreferenceUpdate] = Field(
        min_length=1,
        max_length=len(NotificationPreferenceKey) * 2,
        description="The preferences to set. Anything omitted keeps its current value.",
    )


class AnnouncementCreate(BaseModel):
    """Body for ``POST /notifications/announcements``.

    The **only** way a notification is created by a person rather than derived
    from a domain event, and the reason is structural rather than a preference:
    :mod:`core.events` defines no broadcast scope, because every event the
    platform carries is about something somebody owns and a scope with no owner
    is a scope with no authorization rule. See the note above
    :data:`~core.notifications.ANNOUNCEMENT_RULES`.
    """

    model_config = ConfigDict(extra="forbid")

    kind: AnnouncementKind = Field(
        default=AnnouncementKind.ANNOUNCEMENT,
        description=(
            "`maintenance` for planned unavailability, which is delivered as a high-priority "
            "warning; `announcement` for everything else."
        ),
    )
    message: str = Field(
        min_length=1,
        max_length=settings.NOTIFICATION_ANNOUNCEMENT_MAX_LENGTH,
        description=(
            "What to tell everyone. Rendered as the notification's body verbatim, under a "
            "platform-supplied heading — the one place in this feature where a human's words "
            "become a notification."
        ),
    )

    @field_validator("message")
    @classmethod
    def _normalize_message(cls, value: str) -> str:
        trimmed = " ".join(value.split())
        if not trimmed:
            raise ValueError("An announcement must not be blank.")
        return trimmed


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class NotificationActorRead(BaseModel):
    """Who caused a notification, as a person rather than an identifier.

    An identifier alone is useless in a feed and would force the client to
    resolve every one of them itself — the same reasoning
    :class:`~schemas.case.CaseUserSummary` records. The **name and role only**:
    an email address is contact information the feed has no reason to carry.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    role: str


class NotificationTargetRead(BaseModel):
    """What a notification opens.

    A **resource**, never a URL: ``{"target_type": "case", "target_id": "…"}``
    rather than ``/cases/…``. ``16-notifications.md`` requires that *"navigation
    should remain independent from frontend routing implementation"*, so the
    client owns its own routes and maps this pair onto one — which is what lets
    the web app restructure its paths, and a future mobile client exist at all,
    without the API changing.
    """

    target_type: str = Field(description="`case`, `document`, `report`, or `account`.")
    target_id: uuid.UUID | None = Field(
        default=None,
        description="The resource's identifier. Null for `account`, which names the reader.",
    )


class NotificationRead(BaseModel):
    """One notification, rendered for one reader in one language."""

    id: uuid.UUID
    category: str = Field(
        description=(
            "Which area of the platform this belongs to. A string rather than a closed enum in "
            "the response, because the registry is open by design — a client renders an unknown "
            "category with a neutral icon instead of failing."
        )
    )
    notification_type: NotificationType
    priority: NotificationPriority

    title: str = Field(max_length=MAX_TITLE_LENGTH, description="Rendered heading.")
    message: str = Field(max_length=MAX_MESSAGE_LENGTH, description="Rendered body.")
    language: str = Field(description="ISO 639-1 code this was rendered in.")

    event_type: str | None = Field(
        default=None, description="The domain event this came from, when it came from one."
    )
    rule_key: str = Field(description="Which rule produced the wording. Stable, for the client.")

    case_id: uuid.UUID | None = Field(
        default=None, description="The case this concerns, when there is one."
    )
    actor: NotificationActorRead | None = Field(
        default=None, description="Who caused it, when a person did."
    )
    target: NotificationTargetRead | None = Field(
        default=None, description="What this opens, if anything."
    )

    read_at: datetime | None = Field(default=None, description="When the reader read it.")
    is_read: bool = Field(description="Derived from `read_at`, so the two cannot disagree.")
    created_at: datetime = Field(description="When it was created — the spec's `timestamp`.")

    @classmethod
    def from_row(cls, notification: Notification, *, language: str | None = None) -> NotificationRead:
        """Render one stored notification for a reader.

        A named constructor rather than ``model_validate``, because the two most
        visible fields on this schema are **not columns**: a notification stores
        its rule key and a bounded context, and the title and message are
        produced here by :func:`~core.notifications.render_notification`. See
        :mod:`models.notification` for why the prose is not persisted.
        """
        resolved = resolve_notification_language(language)
        rendered = render_notification(
            rule_key=notification.rule_key,
            category=notification.category,
            context=notification.context,
            language=resolved,
        )

        actor = notification.actor
        return cls(
            id=notification.id,
            category=notification.category,
            notification_type=notification.notification_type,
            priority=notification.priority,
            title=rendered.title,
            message=rendered.message,
            language=resolved,
            event_type=notification.event_type,
            rule_key=notification.rule_key,
            case_id=notification.case_id,
            actor=(
                NotificationActorRead(
                    id=actor.id, full_name=actor.full_name, role=actor.role.value
                )
                if actor is not None
                else None
            ),
            target=(
                NotificationTargetRead(
                    target_type=notification.target_type, target_id=notification.target_id
                )
                if notification.target_type is not None
                else None
            ),
            read_at=notification.read_at,
            is_read=notification.is_read,
            created_at=notification.created_at,
        )


class NotificationPage(BaseModel):
    """One page of the notification feed.

    Carries the totals a client needs to render pagination controls without a
    second count query, plus the **unread count** — because the panel that reads
    this page is also the thing that draws the badge, and making it ask twice
    would be two round trips to render one popover.
    """

    items: list[NotificationRead] = Field(description="Notifications on this page.")
    total_records: int = Field(
        description="Total notifications matching the filters, across all pages."
    )
    unread_count: int = Field(
        description=(
            "Unread notifications the caller has in total, ignoring the filters. Capped at "
            f"{settings.NOTIFICATION_UNREAD_COUNT_CAP}; see `unread_count_capped`."
        )
    )
    unread_count_capped: bool = Field(
        description=(
            "Whether `unread_count` hit its ceiling and means 'this many or more'. Counting "
            "thousands of unread rows exactly costs a full scan to render a badge that would "
            "say '999+' either way."
        )
    )
    page: int = Field(description="Current page number (1-based).")
    page_size: int = Field(description="Maximum number of notifications per page.")
    total_pages: int = Field(description="Number of pages available for the current filters.")

    @classmethod
    def build(
        cls,
        items: list[NotificationRead],
        *,
        total: int,
        unread: int,
        unread_capped: bool,
        page: int,
        page_size: int,
    ) -> NotificationPage:
        """Assemble a page, deriving ``total_pages`` from the total and size.

        An empty result still reports one page, so a client never renders
        "page 1 of 0".
        """
        return cls(
            items=items,
            total_records=total,
            unread_count=unread,
            unread_count_capped=unread_capped,
            page=page,
            page_size=page_size,
            total_pages=max(1, math.ceil(total / page_size)) if page_size else 1,
        )


class NotificationSummaryRead(BaseModel):
    """The caller's unread state, in one small response.

    Its own endpoint rather than a field on the list, because the **bell** needs
    it on every page of the application and the list does not: fetching a page of
    notifications to draw a badge would download a feed nobody opened.
    """

    unread_count: int = Field(description="Unread notifications in total.")
    unread_count_capped: bool = Field(
        description=f"Whether the count hit {settings.NOTIFICATION_UNREAD_COUNT_CAP} and means 'or more'."
    )
    total_count: int = Field(description="Notifications the caller has, read and unread.")
    unread_by_category: dict[str, int] = Field(
        default_factory=dict,
        description="Unread notifications per category, for a filtered panel's counters.",
    )
    highest_unread_priority: NotificationPriority | None = Field(
        default=None,
        description=(
            "The most urgent unread notification's priority, or null when everything is read. "
            "What lets the bell show a critical account alert differently from routine news "
            "without fetching the feed."
        ),
    )


class NotificationPreferenceRead(BaseModel):
    """One preference, as it currently stands, on every channel."""

    preference_key: NotificationPreferenceKey
    in_app: bool = Field(description="Whether in-app notifications of this kind are delivered.")
    email: bool = Field(
        description=(
            "Whether emails of this kind are delivered. Note that a `true` here does not mean "
            "an email is sent: only the notification kinds the platform marks for email travel "
            "on that channel, and only when a provider is configured."
        )
    )
    whatsapp: bool = Field(
        description=(
            "Whether WhatsApp messages of this kind are delivered. As with email, a `true` "
            "here does not mean a message is sent: only the kinds the platform marks for "
            "WhatsApp travel on that channel, only when a provider is configured, and only to "
            "an account that has a phone number on it."
        )
    )
    is_default: bool = Field(
        description=(
            "Whether this is the platform default rather than a choice the caller has made. "
            "Reported so a settings page can show 'default' rather than implying somebody "
            "picked it — and because an account that has expressed no opinion has no stored row."
        )
    )


class NotificationPreferencesRead(BaseModel):
    """Every preference the platform offers, with the caller's answer to each.

    The **complete** set rather than only the stored rows, so a settings page
    renders from one response and a preference added later appears automatically
    at its default.
    """

    preferences: list[NotificationPreferenceRead] = Field(
        description="Every preference, in the platform's own declaration order."
    )


class AnnouncementResult(BaseModel):
    """What a published announcement reached."""

    recipients: int = Field(
        description="Accounts a notification was created for, after preferences were applied."
    )
    skipped: int = Field(
        description=(
            "Active accounts that were not notified because they have switched system "
            "announcements off. Reported rather than hidden, so an administrator knows the "
            "difference between 'nobody was told' and 'nobody wanted to be'."
        )
    )
    kind: AnnouncementKind = Field(description="Which announcement rule was used.")


class NotificationMetricsRead(BaseModel):
    """Platform-wide notification health.

    Every figure ``16-notifications.md``'s Monitoring section names —
    **notifications created, notifications delivered, unread notifications,
    failed deliveries, and average delivery latency** — plus the breakdowns that
    say *what* is being generated and *why* something failed.

    The figures come from two places on purpose, exactly as the assistant's do.
    Counts of rows (created, unread, by category) are **SQL aggregates**: they
    are properties of persisted data, so counting them in a process would reset
    on restart *and* be wrong. Delivery, failure, suppression, and latency
    accumulate **in the process** behind a
    :class:`~services.notification_metrics.NotificationMetricsRecorder`, because
    a delivery is not a row — which is why `since` qualifies those and not the
    others.
    """

    since: datetime = Field(description="When this process started counting the in-process figures.")
    enabled: bool = Field(description="Whether notification creation is switched on here.")

    total_notifications: int = Field(description="Notifications stored, in the window.")
    unread_notifications: int = Field(description="Stored notifications nobody has read yet.")
    read_notifications: int = Field(description="Stored notifications that have been read.")
    read_rate: float = Field(description="Share of stored notifications that have been read, 0-100.")
    recipients: int = Field(description="Distinct accounts holding at least one notification.")

    created: int = Field(description="Notifications this process created.")
    delivered: int = Field(
        description=(
            "Notifications this process published onto the event channel. **Not** 'arrived at a "
            "browser': that would measure a client's network, which the platform neither "
            "controls nor can distinguish from a laptop going to sleep."
        )
    )
    failed: int = Field(description="Notifications this process could not create or publish.")
    suppressed_by_preference: int = Field(
        description="Notifications not created because the recipient had switched them off."
    )
    deduplicated: int = Field(
        description="Notifications not created because the recipient already had that one."
    )
    dropped: int = Field(
        description=(
            "Events discarded because the notification queue was full. A backlog the platform "
            "could not keep up with — the one counter here that is a capacity signal."
        )
    )
    pending: int = Field(description="Events accepted and not yet turned into notifications.")

    average_delivery_latency_ms: float | None = Field(
        default=None,
        description=(
            "Mean time from the originating event being published to the notification being "
            "persisted and announced. Null rather than zero when nothing has been delivered — "
            "an average over no deliveries is undefined, while zero would read as instantaneous."
        ),
    )

    notifications_by_category: dict[str, int] = Field(
        default_factory=dict, description="Stored notifications per category, in the window."
    )
    created_by_rule: dict[str, int] = Field(
        default_factory=dict, description="What this process has been creating, by rule."
    )
    failures_by_reason: dict[str, int] = Field(
        default_factory=dict, description="Why creation failed, by cause."
    )

    window_days: int | None = Field(
        default=None, description="The window applied to the SQL figures, or null for all history."
    )


__all__ = [
    "AnnouncementCreate",
    "AnnouncementResult",
    "NotificationActorRead",
    "NotificationListQuery",
    "NotificationMetricsQuery",
    "NotificationMetricsRead",
    "NotificationPage",
    "NotificationPreferenceRead",
    "NotificationPreferenceUpdate",
    "NotificationPreferencesRead",
    "NotificationPreferencesUpdate",
    "NotificationRead",
    "NotificationReadAllRequest",
    "NotificationReadRequest",
    "NotificationSortField",
    "NotificationSummaryRead",
    "NotificationTargetRead",
]
