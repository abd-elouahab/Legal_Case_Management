"""User data access.

Single responsibility: reading and persisting :class:`~models.user.User` rows.
No authentication or authorization rules live here — those belong to
``services/auth.py`` and ``services/user.py``. In particular this layer never
decides *whether* a change is allowed, only how it is written.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, asc, desc, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import UnaryExpression

from core.users import normalize_email
from models.user import User, UserRole, UserStatus
from schemas.user import SortOrder, UserListQuery, UserSortField


class UserRepository:
    """Queries and mutations for the ``users`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------- reading #

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        """Return the user with this id, or ``None``."""
        return self._session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        """Return the user with this email, or ``None``.

        Emails are stored lowercase, so the lookup is normalized to match and is
        therefore case-insensitive.
        """
        statement = select(User).where(User.email == normalize_email(email))
        return self._session.execute(statement).scalar_one_or_none()

    def email_taken(self, email: str, *, excluding: uuid.UUID | None = None) -> bool:
        """Whether another account already uses this email.

        ``excluding`` skips one user, so an edit that leaves the email unchanged
        (or merely re-cases it) does not collide with the record being edited.
        """
        statement = select(User.id).where(User.email == normalize_email(email))
        if excluding is not None:
            statement = statement.where(User.id != excluding)
        return self._session.execute(statement.limit(1)).scalar_one_or_none() is not None

    def list_users(self, query: UserListQuery) -> tuple[list[User], int]:
        """Return one page of users and the total number matching the filters.

        The total is counted over the *filtered* set but before pagination, so a
        client can render "showing 20 of 137" and know how many pages exist.
        Both queries are built from the same filter clause, which is what keeps
        the count honest when a filter is added later.
        """
        filtered = self._apply_filters(select(User), query)

        total = self._session.execute(
            select(func.count()).select_from(filtered.subquery())
        ).scalar_one()

        page = (
            filtered.order_by(*self._order_by(query))
            .offset(query.offset)
            .limit(query.page_size)
        )
        return list(self._session.execute(page).scalars().all()), total

    # ------------------------------------------------------------- writing #

    def create(self, user: User) -> User:
        """Persist a new user and return it with its generated columns populated."""
        self._session.add(user)
        self._session.commit()
        self._session.refresh(user)
        return user

    def save(self, user: User) -> User:
        """Persist pending changes to an existing user."""
        self._session.commit()
        self._session.refresh(user)
        return user

    def set_last_login(self, user: User, when: datetime) -> None:
        """Record a successful sign-in timestamp."""
        user.last_login_at = when
        self._session.commit()

    def set_password(
        self,
        user: User,
        hashed_password: str,
        *,
        revoke_sessions: bool = True,
        must_change_password: bool = False,
        updated_by: uuid.UUID | None = None,
    ) -> None:
        """Persist a new password hash, optionally revoking every existing session.

        Bumping ``session_generation`` invalidates all tokens issued under the
        previous generation. Every field is written in one transaction: a password
        change that succeeded but left old sessions valid — or left the
        "must change" flag behind — would be a security hole, so they must never
        diverge.
        """
        user.hashed_password = hashed_password
        user.must_change_password = must_change_password
        if revoke_sessions:
            user.session_generation += 1
        if updated_by is not None:
            user.updated_by = updated_by
        self._session.commit()

    # ------------------------------------------------------------- helpers #

    @staticmethod
    def _apply_filters(statement: Select[tuple[User]], query: UserListQuery) -> Select[tuple[User]]:
        """Narrow a user query by the requested search term and filters.

        Search and the two filters combine with AND, so they are freely
        combinable exactly as the spec requires.
        """
        if query.search:
            # ILIKE with escaped wildcards: a term containing % or _ must match
            # those characters literally rather than turning into a pattern.
            pattern = f"%{_escape_like(query.search)}%"
            statement = statement.where(
                or_(
                    User.first_name.ilike(pattern, escape="\\"),
                    User.last_name.ilike(pattern, escape="\\"),
                    User.email.ilike(pattern, escape="\\"),
                )
            )

        if query.role is not None:
            statement = statement.where(User.role == UserRole(query.role))

        if query.status is not None:
            statement = statement.where(User.status == UserStatus(query.status))

        return statement

    @staticmethod
    def _order_by(query: UserListQuery) -> tuple[UnaryExpression[object], ...]:
        """Build the ORDER BY clause for the requested sort.

        The primary key is appended as a tiebreaker: without it, rows that share
        a sort value (two users created in the same transaction, or two who have
        never logged in) could appear in a different order on each request and
        be duplicated or skipped across page boundaries.
        """
        direction = asc if query.sort_order is SortOrder.ASC else desc

        columns = {
            # "Name" sorts by family name first, which is how a directory is
            # conventionally ordered, then by given name to break ties.
            UserSortField.NAME: (User.last_name, User.first_name),
            UserSortField.EMAIL: (User.email,),
            UserSortField.CREATED_AT: (User.created_at,),
            UserSortField.LAST_LOGIN: (User.last_login_at,),
        }[query.sort_by]

        return (*(direction(column) for column in columns), asc(User.id))


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search term is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
