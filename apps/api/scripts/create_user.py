"""Create or update a platform user from the command line.

Self-registration does not exist: accounts are provisioned by administrators
through the User Management API and UI. This script remains the **bootstrap**
path — it is how the first administrator is created, before any account exists to
authorize that call, and how an account is recovered if every administrator is
locked out.

Run from ``apps/api``::

    python -m scripts.create_user --email admin@example.com \
        --name "Amina Benali" --role administrator --password "..."

Omit ``--password`` to be prompted without the value appearing in shell history
or process arguments. Passing ``--password`` on an existing account resets it.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from core.security import hash_password
from core.users import normalize_email, split_full_name
from db.session import SessionLocal
from models.user import User, UserRole, UserStatus
from repositories.user import UserRepository
from schemas.password import MIN_PASSWORD_LENGTH


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or update a platform user.")
    parser.add_argument("--email", required=True, help="Login email address.")
    parser.add_argument(
        "--name",
        required=True,
        help="Full name. Split on the first space into first and last name.",
    )
    parser.add_argument(
        "--role",
        required=True,
        choices=[role.value for role in UserRole],
        help="Platform role.",
    )
    parser.add_argument(
        "--password",
        help="Account password. Omit to be prompted interactively (recommended).",
    )
    parser.add_argument(
        "--inactive",
        action="store_true",
        help="Create the account in a disabled state.",
    )
    return parser.parse_args(argv)


def _resolve_password(supplied: str | None) -> str:
    password = supplied or getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if supplied is None and password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match.")
    return password


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    password = _resolve_password(args.password)
    email = normalize_email(args.email)
    first_name, last_name = split_full_name(args.name)
    status = UserStatus.INACTIVE if args.inactive else UserStatus.ACTIVE

    session = SessionLocal()
    try:
        repository = UserRepository(session)
        existing = repository.get_by_email(email)

        if existing is not None:
            existing.first_name = first_name
            existing.last_name = last_name
            existing.role = UserRole(args.role)
            existing.status = status
            existing.hashed_password = hash_password(password)
            # A bootstrap reset is an out-of-band credential change, so it must
            # end any session still holding the old password, exactly as an
            # in-app password change does.
            existing.session_generation += 1
            existing.must_change_password = False
            existing.updated_at = datetime.now(UTC)
            session.commit()
            print(f"Updated existing user {email} (id={existing.id}).")
            return 0

        user = User(
            email=email,
            first_name=first_name,
            last_name=last_name,
            role=UserRole(args.role),
            status=status,
            hashed_password=hash_password(password),
        )
        session.add(user)
        session.commit()
        print(f"Created user {email} (id={user.id}, role={user.role.value}).")
        return 0
    except SQLAlchemyError as exc:
        session.rollback()
        print(f"Database error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
