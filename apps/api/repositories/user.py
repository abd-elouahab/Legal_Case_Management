"""User data access.

Single responsibility: reading and persisting :class:`~models.user.User` rows.
No authentication rules live here — those belong to ``services/auth.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.user import User


class UserRepository:
    """Queries and mutations for the ``users`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        """Return the user with this id, or ``None``."""
        return self._session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        """Return the user with this email, or ``None``.

        Emails are stored lowercase, so the lookup is normalized to match and is
        therefore case-insensitive.
        """
        statement = select(User).where(User.email == email.strip().lower())
        return self._session.execute(statement).scalar_one_or_none()

    def set_last_login(self, user: User, when: datetime) -> None:
        """Record a successful sign-in timestamp."""
        user.last_login_at = when
        self._session.commit()

    def set_password(self, user: User, hashed_password: str, *, revoke_sessions: bool = True) -> None:
        """Persist a new password hash, optionally revoking every existing session.

        Bumping ``session_generation`` invalidates all tokens issued under the
        previous generation. Both fields are written in one transaction: a password
        change that succeeded but left old sessions valid would be a security hole,
        so the two must never diverge.
        """
        user.hashed_password = hashed_password
        if revoke_sessions:
            user.session_generation += 1
        self._session.commit()
