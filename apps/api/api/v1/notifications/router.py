"""Notification endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :class:`~services.notification.NotificationService`, and shape the HTTP
response. No business logic lives here, and — the part that matters for this
feature — **no route creates a notification from a business action**. There is
one creation endpoint, `POST /notifications/announcements`, and it exists because
the platform has no broadcast topic to publish an announcement on (see
:data:`~core.notifications.ANNOUNCEMENT_RULES`). Everything else in the feed
arrived through the event dispatcher, from modules that do not know this API
exists.

**The surface is small on purpose.** A notification is not a resource a client
manipulates: it cannot be created, edited, or deleted through this API, because
the only writes the spec describes are *read state* and *preferences*. There is
no `DELETE /notifications/{id}` — ``16-notifications.md`` puts archiving out of
scope and asks the implementation to *prepare* for it, which
:attr:`~models.notification.Notification.archived_at` does without an endpoint
that would have to be designed twice.

Authorization is layered, and the two layers answer different questions:

* the ``notifications:*`` **capabilities** are required by the reusable
  dependencies in :mod:`api.authorization`, declared next to the route so they
  appear in the OpenAPI schema. Because ``CurrentUser`` resolves first, an
  anonymous caller gets **401** and only an authenticated-but-unentitled one gets
  **403**;
* the **notification** — whose it is — is applied by the repository's queries,
  which are all keyed by recipient, so somebody else's notification is a **404**
  rather than a 403. The asymmetry is deliberate: confirming that another
  person's notification exists is itself the disclosure.

There is deliberately **no per-resource access module** behind these routes.
"May this caller read this notification" is a single equality that every query in
:mod:`repositories.notification` asserts in its own ``WHERE`` clause — the same
shape :mod:`repositories.conversation` has, and the reason neither feature needs
one.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, status

from api.authorization import require_permission
from api.deps import (
    EmailDeliveryServiceDep,
    NotificationServiceDep,
    NotificationSubscriberDep,
)
from core.notifications import (
    ChannelPreference,
    ChannelPreferenceUpdate,
    NotificationCategory,
    NotificationPreferenceKey,
)
from core.permissions import Permission
from models.user import User
from schemas.email import EmailMetricsQuery, EmailMetricsRead
from schemas.notification import (
    AnnouncementCreate,
    AnnouncementResult,
    NotificationListQuery,
    NotificationMetricsQuery,
    NotificationMetricsRead,
    NotificationPage,
    NotificationPreferenceRead,
    NotificationPreferencesRead,
    NotificationPreferencesUpdate,
    NotificationRead,
    NotificationReadAllRequest,
    NotificationReadRequest,
    NotificationSummaryRead,
)

logger = structlog.get_logger(__name__)

#: Mounted under ``/notifications``.
router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
# --------------------------------------------------------------------------- #

Recipient = Annotated[User, Depends(require_permission(Permission.NOTIFICATIONS_VIEW))]
Announcer = Annotated[User, Depends(require_permission(Permission.NOTIFICATIONS_MANAGE))]
NotificationMonitor = Annotated[
    User, Depends(require_permission(Permission.NOTIFICATIONS_MONITOR))
]

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "Missing, invalid, or expired access token.",
    }
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "description": "The account is disabled or lacks the required permission.",
    }
}
_NOT_FOUND: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such notification belongs to this caller — it does not exist, it was archived, "
            "or it is somebody else's. The three are deliberately indistinguishable."
        ),
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Request validation failed."}
}
_UNAVAILABLE: dict[int | str, dict[str, object]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": "Notification creation is disabled on this deployment.",
    }
}


# --------------------------------------------------------------------------- #
# The bell
# --------------------------------------------------------------------------- #


@router.get(
    "/summary",
    response_model=NotificationSummaryRead,
    status_code=status.HTTP_200_OK,
    summary="Your unread notification state",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_notification_summary(
    actor: Recipient, notifications: NotificationServiceDep
) -> NotificationSummaryRead:
    """Return how many notifications the caller has not read yet.

    **The bell's endpoint.** Its own route rather than a field on the list,
    because the badge is drawn on every page of the application and the list is
    not: fetching a page of notifications to render a number would download a
    feed nobody opened.

    The count is **capped**, and says so. Counting thousands of unread rows
    exactly is a scan to render something that would display "999+" either way,
    so past the ceiling `unread_count_capped` is true and the number means "this
    many or more".

    `highest_unread_priority` is what lets the bell show a critical account alert
    differently from routine case news without fetching the feed to find out.

    **Registered before `/{notification_id}`** so `summary` is matched as a
    literal path rather than parsed as an identifier — FastAPI resolves routes in
    declaration order, and a UUID parser would answer 422 for this URL.
    """
    counts = notifications.summary(actor=actor)
    return NotificationSummaryRead(
        unread_count=counts.unread,
        unread_count_capped=counts.unread_capped,
        total_count=counts.total,
        unread_by_category=counts.unread_by_category,
        highest_unread_priority=counts.highest_unread_priority,
    )


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


@router.get(
    "/preferences",
    response_model=NotificationPreferencesRead,
    status_code=status.HTTP_200_OK,
    summary="Your notification preferences",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_notification_preferences(
    actor: Recipient, notifications: NotificationServiceDep
) -> NotificationPreferencesRead:
    """Return every preference the platform offers, with the caller's answer.

    The **complete** set rather than only the choices they have made, so a
    settings page renders from one response and a preference added later appears
    automatically at its default. `is_default` says which is which — an account
    that has expressed no opinion has no stored row at all, and showing "on"
    without saying it is the default would imply somebody chose it.

    Preferences are the caller's own. There is no endpoint for reading or setting
    anybody else's, and `notifications:manage` does not grant one: what somebody
    has chosen to be told about is a small statement about how they work.
    """
    return _to_preferences(notifications.preferences(actor=actor))


@router.put(
    "/preferences",
    response_model=NotificationPreferencesRead,
    status_code=status.HTTP_200_OK,
    summary="Update your notification preferences",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def update_notification_preferences(
    actor: Recipient,
    notifications: NotificationServiceDep,
    payload: NotificationPreferencesUpdate,
) -> NotificationPreferencesRead:
    """Set some of the caller's preferences and return the complete set.

    **A list of changes rather than the whole set**, so two settings panels open
    at once cannot silently revert each other's saves, and so an eighth
    preference added later does not make an older client's payload invalid.
    Anything omitted keeps its current value.

    **Partial per channel as well as per key.** An entry carrying only `in_app`
    leaves `email` exactly as it was — which is what stops a client written before
    the email channel existed from switching it off by not mentioning it, and what
    the next channel inherits for free.

    Switching a preference off stops *new* notifications of that kind being
    created — or, for `email`, being sent — and does not remove the ones already
    in the feed, which is the only behaviour that makes "turn this off" a decision
    somebody can reverse.
    """
    return _to_preferences(
        notifications.update_preferences(
            {
                entry.preference_key: ChannelPreferenceUpdate(
                    in_app=entry.in_app, email=entry.email
                )
                for entry in payload.preferences
            },
            actor=actor,
        )
    )


def _to_preferences(
    values: dict[NotificationPreferenceKey, ChannelPreference],
) -> NotificationPreferencesRead:
    """Project the service's answer onto the response shape.

    Ordered by the platform's own declaration order rather than by the mapping's
    iteration order, so a settings page's rows do not move between requests.
    """
    return NotificationPreferencesRead(
        preferences=[
            NotificationPreferenceRead(
                preference_key=key,
                in_app=values[key].in_app,
                email=values[key].email,
                is_default=values[key].is_default,
            )
            for key in NotificationPreferenceKey
            if key in values
        ]
    )


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


@router.get(
    "/metrics",
    response_model=NotificationMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="Notification metrics",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_notification_metrics(
    actor: NotificationMonitor,
    notifications: NotificationServiceDep,
    subscriber: NotificationSubscriberDep,
    query: Annotated[NotificationMetricsQuery, Query()],
) -> NotificationMetricsRead:
    """Return platform-wide notification health.

    The five figures the spec's Monitoring section names — **notifications
    created, notifications delivered, unread notifications, failed deliveries,
    and average delivery latency** — plus the suppression counters that explain a
    low creation rate and the breakdowns that say what is being generated and why
    something failed.

    **The figures come from two places, and the response says which.** Counts of
    rows are SQL aggregates: exact, surviving a restart, and the same on every API
    instance. Delivery, latency, failure, and suppression accumulate in *this*
    process and carry `since`, because a delivery is not a row — the same split
    `/assistant/metrics` makes, and the reason `/reports/metrics` carries no
    caveat at all.

    `delivered` means "published onto the event channel", not "arrived in a
    browser": the second would measure a client's network, which the platform
    neither controls nor can distinguish from a laptop going to sleep.

    An operational view, so it is gated on `notifications:monitor` and reports
    **counts, durations, categories, and rule keys only** — never a notification,
    a message, a case, or whose feed it was in.

    **Registered before `/{notification_id}`** for the same reason `/summary` is.
    """
    metrics = notifications.metrics(
        window_days=query.window_days, pending=subscriber.pending
    )
    statistics = metrics.statistics
    counters = metrics.counters

    return NotificationMetricsRead(
        since=counters.since,
        enabled=metrics.enabled,
        total_notifications=statistics.total,
        unread_notifications=statistics.unread,
        read_notifications=statistics.read,
        read_rate=statistics.read_rate,
        recipients=statistics.recipients,
        created=counters.created,
        delivered=counters.delivered,
        failed=counters.failed,
        suppressed_by_preference=counters.suppressed_by_preference,
        deduplicated=counters.deduplicated,
        dropped=counters.dropped,
        pending=metrics.pending,
        average_delivery_latency_ms=counters.average_delivery_latency_ms,
        notifications_by_category=statistics.by_category,
        created_by_rule=counters.created_by_rule,
        failures_by_reason=counters.failures_by_reason,
        window_days=metrics.window_days,
    )


@router.get(
    "/email/metrics",
    response_model=EmailMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="Email delivery metrics",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def get_email_metrics(
    actor: NotificationMonitor,
    emails: EmailDeliveryServiceDep,
    query: Annotated[EmailMetricsQuery, Query()],
) -> EmailMetricsRead:
    """Return platform-wide email delivery health.

    The five figures `17-email-delivery-channel.md`'s Monitoring section names —
    **queued emails, sent emails, failed emails, retry count, and average delivery
    latency** — plus the skip counters that explain a low send rate and the
    breakdowns that say what is being sent and why something failed.

    **The figures come from two places, and the response says which.** Counts of
    rows are SQL aggregates: exact, surviving a restart, and the same on every API
    instance — which matters more here than for the in-app feed, because "how many
    emails are stuck?" is the first question after a deployment. Retries, latency,
    and skips accumulate in *this* process and carry `since`.

    `sent` means "a provider accepted it", not "it arrived" and certainly not "it
    was read". SMTP hands off to a relay and what happens next is outside anything
    this platform can observe — the same line `/notifications/metrics` draws
    between "published onto the channel" and "arrived in a browser".

    **There is deliberately no endpoint that lists deliveries.** A delivery names
    a person, a rule, a moment, and an address, so a list of them would be a live
    index of who the platform writes to about what. Troubleshooting a specific
    complaint is a query against `email_deliveries` under the database's own
    access controls; this endpoint serves what an operational view legitimately
    needs.

    An administrative view, so it is gated on `notifications:monitor` — **no new
    permission**. Email is a *delivery channel* for notifications rather than a
    feature of its own: a deployment that trusts somebody with notification health
    is trusting them with the same information one channel at a time, and a
    separate `email:monitor` would be a second grant meaning the same thing.

    **Registered before `/{notification_id}`** for the same reason `/summary` is.
    """
    metrics = emails.metrics(window_days=query.window_days)
    statistics = metrics.statistics
    counters = metrics.counters

    return EmailMetricsRead(
        since=counters.since,
        enabled=metrics.enabled,
        provider=metrics.provider,
        provider_available=metrics.provider_available,
        templates_available=metrics.templates_available,
        total_deliveries=statistics.total,
        queued=statistics.pending,
        sending=statistics.sending,
        sent=statistics.sent,
        failed=statistics.failed,
        delivery_rate=statistics.delivery_rate,
        recipients=statistics.recipients,
        attempts=statistics.attempts,
        queued_this_process=counters.queued,
        sent_this_process=counters.sent,
        failed_this_process=counters.failed,
        retried=counters.retried,
        skipped=counters.skipped,
        average_delivery_latency_ms=counters.average_delivery_latency_ms,
        average_send_duration_ms=counters.average_send_duration_ms,
        sent_by_rule=counters.sent_by_rule,
        failures_by_code=counters.failures_by_code,
        stored_failures_by_code=statistics.by_failure_code,
        retries_by_code=counters.retries_by_code,
        skipped_by_reason=counters.skipped_by_reason,
        window_days=metrics.window_days,
    )


# --------------------------------------------------------------------------- #
# Announcements
# --------------------------------------------------------------------------- #


@router.post(
    "/announcements",
    response_model=AnnouncementResult,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a platform-wide announcement",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION, **_UNAVAILABLE},
)
def publish_announcement(
    actor: Announcer, notifications: NotificationServiceDep, payload: AnnouncementCreate
) -> AnnouncementResult:
    """Tell everyone on the platform something.

    **The one endpoint that creates a notification.** Every other notification is
    derived from a domain event by a subscriber that no business module knows
    about; an announcement has no business action behind it and no resource it is
    about, so there is no event it could be published as — `core/events.py`
    defines no broadcast scope, deliberately, because a scope with no owner is a
    scope with no authorization rule.

    It is still the Notification Service creating it — no business module is
    involved — and everything downstream is the ordinary path: preferences are
    honoured, rows are batched, and each one is delivered on its recipient's own
    topic.

    Reaches **active accounts only**. A suspended user cannot read the platform,
    and writing them a notification would accumulate a feed nobody can see.

    `skipped` reports how many active accounts have switched system announcements
    off, so an administrator can tell "nobody was told" from "nobody wanted to
    be".

    Requires `notifications:manage`, which lawyers and court representatives do
    not hold: addressing the whole platform is not part of either role.
    """
    outcome = notifications.announce(
        kind=payload.kind, message=payload.message, actor=actor
    )
    return AnnouncementResult(
        recipients=outcome.recipients, skipped=outcome.skipped, kind=outcome.kind
    )


# --------------------------------------------------------------------------- #
# Read state
# --------------------------------------------------------------------------- #


@router.patch(
    "/read",
    response_model=NotificationSummaryRead,
    status_code=status.HTTP_200_OK,
    summary="Mark notifications as read",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def mark_notifications_read(
    actor: Recipient,
    notifications: NotificationServiceDep,
    payload: NotificationReadRequest,
) -> NotificationSummaryRead:
    """Mark specific notifications as read, and return the new unread state.

    **Identifiers that are not the caller's are ignored rather than refused.** A
    panel marks what it has on screen, and one row archived underneath it must
    not fail the whole request — and refusing would confirm that a notification
    with that identifier exists, which is the disclosure this feature avoids
    everywhere else.

    Re-marking something already read is a no-op: "when did I read this" must not
    move because a list refreshed.

    The response is the **summary** rather than the marked rows, because what a
    client does next is redraw the badge — and returning the rows it already has
    would be a page of data to update a number.
    """
    notifications.mark_read(payload.notification_ids, actor=actor)
    return get_notification_summary(actor, notifications)


@router.patch(
    "/read-all",
    response_model=NotificationSummaryRead,
    status_code=status.HTTP_200_OK,
    summary="Mark all notifications as read",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def mark_all_notifications_read(
    actor: Recipient,
    notifications: NotificationServiceDep,
    payload: NotificationReadAllRequest | None = None,
) -> NotificationSummaryRead:
    """Mark everything unread as read, and return the new unread state.

    One statement whatever the feed's size, which is why this is its own endpoint
    rather than a bulk call: loading a thousand identifiers to send them back
    would be a page of network to do what the database does in a clause.

    `category` narrows it, and exists because a feed filtered to one category
    beside a "mark all as read" button that ignored the filter is the most
    reliable way to lose a notification somebody meant to keep.
    """
    category: NotificationCategory | None = payload.category if payload else None
    notifications.mark_all_read(actor=actor, category=category)
    return get_notification_summary(actor, notifications)


# --------------------------------------------------------------------------- #
# The feed
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=NotificationPage,
    status_code=status.HTTP_200_OK,
    summary="List your notifications",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def list_notifications(
    actor: Recipient,
    notifications: NotificationServiceDep,
    query: Annotated[NotificationListQuery, Query()],
) -> NotificationPage:
    """List the caller's own notifications, newest first.

    **Only the caller's.** The scope is applied in the database query, so the page
    totals count only their notifications — a total that included other people's
    would disclose how many exist, and the spec requires history to remain
    user-specific.

    `unread_only`, `category`, `notification_type`, `priority`, and `case_id` are
    the spec's "notification filtering", and every one of them executes in SQL:
    filtering after the fact would return short pages and make the totals lie.

    **The titles and messages are rendered in `language`**, defaulting to French.
    A notification stores no prose — only the rule that produced it and the few
    values its sentence interpolates — so switching the interface to Arabic
    re-renders the *whole history* rather than only what arrives afterwards.

    `unread_count` comes back with the page because the panel that renders one
    also draws the badge, and making it ask twice would be two round trips to
    render one popover.
    """
    page = notifications.list_notifications(query, actor=actor)

    return NotificationPage.build(
        [
            NotificationRead.from_row(notification, language=query.language)
            for notification in page.results
        ],
        total=page.total,
        unread=page.counts.unread,
        unread_capped=page.counts.unread_capped,
        page=page.page,
        page_size=page.page_size,
    )


@router.get(
    "/{notification_id}",
    response_model=NotificationRead,
    status_code=status.HTTP_200_OK,
    summary="Read one notification",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND},
)
def get_notification(
    notification_id: uuid.UUID,
    actor: Recipient,
    notifications: NotificationServiceDep,
    language: Annotated[
        str | None,
        Query(description="ISO 639-1 code to render the title and message in."),
    ] = None,
) -> NotificationRead:
    """Return one notification.

    **Reading it does not mark it read.** The two are separate operations because
    only the client knows whether a notification was actually *seen* — a
    prefetch, a deep link, and a background refresh all fetch one without anybody
    looking at it, and a GET with that side effect would quietly empty somebody's
    badge.

    A notification that does not exist, one that was archived, and one belonging
    to another user all answer **404** — telling them apart is precisely the
    disclosure this feature must not make.
    """
    return NotificationRead.from_row(
        notifications.get_notification(notification_id, actor=actor), language=language
    )


__all__ = ["router"]
