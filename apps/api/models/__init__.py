"""SQLAlchemy ORM models.

Every model module must be imported here so that its table registers on
``Base.metadata`` before Alembic autogenerate runs and before ``create_all`` is
used in tests.
"""

from __future__ import annotations

from models.user import User, UserRole

__all__ = ["User", "UserRole"]
