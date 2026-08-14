"""Per-widget authorization for the dashboard.

``19-dashboard-analytics.md`` states the rule three times, in three ways:
*"every widget must enforce authorization independently"*, *"analytics should only
include data the authenticated user is allowed to access"*, and *"aggregated
metrics must never leak unauthorized information"*. All three are the same
requirement seen from different distances, and this module is where it is
answered.

**It owns no policy of its own.** That is the whole design, and it is the reason
this is a module rather than a few checks inside the service. A dashboard is the
first feature on the platform that reads *across* every other one, so it is
precisely the place where a second, subtly different copy of "may this person see
this" would appear — and it would be the copy nobody notices is wrong, because a
dashboard shows a number rather than a document. So there is no rule here. There
are three delegations:

* **Which widgets** — the capabilities declared on each
  :class:`~core.dashboard.WidgetDefinition`, checked against the caller's
  role-derived permissions by :class:`~services.authorization.AuthorizationService`.
  A widget the caller does not hold every capability for is not filtered out of
  the response after being computed; it is never computed.
* **Which rows** — :meth:`~services.case_access.CaseAccessPolicy.visibility_scope`,
  the *same* call the case list, the document list, the timeline, and semantic
  search all make. A lawyer's dashboard counts their cases because the query is
  restricted by the identical predicate that restricts their case list, not
  because a widget remembered to filter.
* **Which private histories** — identity equality, for the resources that belong
  to a person rather than to a case: reports, conversations, and notifications.
  There is no policy object for those anywhere on the platform (see
  :mod:`repositories.report`, :mod:`repositories.conversation`,
  :mod:`repositories.notification`), because the rule is one equality that every
  query asserts in its own ``WHERE`` clause — so the dashboard asserts it in the
  same place, by passing the caller's own identifier and never accepting one.

**Note what this class cannot do.** It has no method that takes a user
identifier, so there is no way to ask it about somebody else; and it returns
scopes and booleans rather than rows, so it can narrow a query and can never
widen one.
"""

from __future__ import annotations

import uuid

import structlog

from core.dashboard import (
    QuickActionKey,
    WidgetDefinition,
    WidgetKey,
    available_actions,
    widget_definition,
)
from core.permissions import Permission
from models.user import User
from services.authorization import AuthorizationService
from services.case_access import CaseAccessPolicy

logger = structlog.get_logger(__name__)


class DashboardAccessPolicy:
    """Decides which widgets a user is offered, and what each one may count.

    Stateless and pure — it reads the caller's role-derived permissions and their
    identity, and touches neither the database nor the network.
    """

    def __init__(
        self,
        authorization: AuthorizationService | None = None,
        cases: CaseAccessPolicy | None = None,
    ) -> None:
        self._authorization = authorization or AuthorizationService()
        self._cases = cases or CaseAccessPolicy()

    # --------------------------------------------------------------- widgets #

    def permissions_for(self, user: User) -> frozenset[Permission]:
        """Everything ``user`` is allowed to do.

        Resolved once per dashboard load and passed down, rather than asked per
        widget: nineteen widgets asking the same question nineteen times would be
        nineteen chances for one of them to ask it differently.
        """
        return self._authorization.permissions_for(user)

    def can_view(self, definition: WidgetDefinition, permissions: frozenset[Permission]) -> bool:
        """Whether a caller holding ``permissions`` may be offered this widget."""
        return definition.is_visible_to(permissions)

    def visible_widgets(
        self, keys: tuple[WidgetKey, ...], permissions: frozenset[Permission]
    ) -> tuple[WidgetKey, ...]:
        """Narrow a role's layout to the widgets its holder may actually see.

        An **intersection, never a union**: a layout is an ordering and a
        statement about relevance, and the only thing that decides visibility is
        the capability set. That is what makes
        :data:`~core.dashboard.ROLE_LAYOUTS` safe to edit without reviewing it as
        security policy.
        """
        return tuple(
            key for key in keys if self.can_view(widget_definition(key), permissions)
        )

    def require_view(self, key: WidgetKey, user: User) -> WidgetDefinition:
        """Return a widget's definition, or refuse the caller.

        Used by the single-widget refresh endpoint, which is reached with a key
        from a URL rather than from a layout the server just computed.

        Raises:
            PermissionError: the caller lacks one of the widget's capabilities.
                Translated by the router into the same **404** an unknown widget
                key produces — see :func:`~api.v1.dashboard.router.refresh_widget`
                for why the two are deliberately indistinguishable.
        """
        definition = widget_definition(key)
        if not self.can_view(definition, self.permissions_for(user)):
            logger.warning(
                "dashboard_widget_denied",
                user_id=str(user.id),
                role=user.role.value,
                widget=key.value,
                reason="missing_permission",
            )
            raise PermissionError(key.value)
        return definition

    # ----------------------------------------------------------------- scope #

    def case_scope(self, user: User) -> uuid.UUID | None:
        """The user id every case-derived query must be restricted to, or ``None``.

        ``None`` means "no restriction" and is returned only for a caller holding
        ``cases:view-all``. Delegated rather than re-derived, so a dashboard tile
        and a case list can never disagree about whose cases they are counting.
        """
        return self._cases.visibility_scope(user)

    def owner_scope(self, user: User) -> uuid.UUID:
        """The identifier a private history must be keyed by.

        Always the caller, and there is no parameter by which it could be anybody
        else. Reports, conversations, and notifications belong to one person; a
        dashboard is the last place that should be the first to offer a way round
        that.
        """
        return user.id

    # --------------------------------------------------------- quick actions #

    def quick_actions(self, permissions: frozenset[Permission]) -> tuple[QuickActionKey, ...]:
        """The shortcuts this caller may use.

        Gated on the capabilities the *destination* requires, so a shortcut can
        never appear for somebody the endpoint behind it would refuse — the spec's
        *"quick actions should respect authorization"*, enforced against the same
        permission the action's own route declares.
        """
        return available_actions(permissions)


__all__ = ["DashboardAccessPolicy"]
