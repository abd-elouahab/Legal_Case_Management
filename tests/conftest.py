"""Shared pytest fixtures for the backend test suite.

The testing environment is forced before any application module is imported so
that settings validation and client construction use test-safe defaults.

Tests run without Docker: the database is SQLite in-memory and the Redis-backed
token denylist is replaced by an in-memory double, both via FastAPI dependency
overrides.
"""

from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "testing")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("ENABLE_DOCS", "true")
# bcrypt's minimum cost. Real deployments use the configured default (12); tests
# would otherwise spend most of their runtime deliberately hashing slowly.
os.environ.setdefault("BCRYPT_ROUNDS", "4")

import itertools
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    """Provide a TestClient with the application lifespan active."""
    from main import app

    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# Authentication fixtures
# --------------------------------------------------------------------------- #


class InMemoryRevocationStore:
    """Test double for :class:`~services.token_revocation.TokenRevocationStore`.

    Mirrors the real contract (revoke by ``jti``, honour expiry) without Redis.
    """

    def __init__(self) -> None:
        self._revoked: dict[str, datetime] = {}

    def revoke(self, jti: str, expires_at: datetime) -> None:
        if (expires_at - datetime.now(UTC)).total_seconds() > 0:
            self._revoked[jti] = expires_at

    def is_revoked(self, jti: str) -> bool:
        expires_at = self._revoked.get(jti)
        if expires_at is None:
            return False
        if expires_at <= datetime.now(UTC):
            # Expired entries would have been dropped by Redis' TTL.
            del self._revoked[jti]
            return False
        return True


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A SQLite in-memory session with the full schema created."""
    import models  # noqa: F401  -- registers models on Base.metadata
    from db.base import Base

    engine = create_engine(
        "sqlite://",
        # A single shared connection so the in-memory database survives across
        # sessions opened during one test.
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


class InMemoryLoginThrottle:
    """Test double for :class:`~services.login_throttle.LoginThrottle`.

    Mirrors the real contract — per-account and per-IP counters, lockout after the
    configured threshold, reset on success — without Redis. Lockouts are held as an
    absolute expiry so tests can freeze or advance the clock via ``now_provider``.
    """

    def __init__(self, now_provider: Callable[[], datetime] | None = None) -> None:
        from core.config import settings

        self._max_attempts = settings.MAX_FAILED_LOGIN_ATTEMPTS
        self._window = settings.login_failure_window
        self._lockout = settings.login_lockout_duration
        self._base_now = now_provider or (lambda: datetime.now(UTC))
        #: Test-controlled clock offset, moved by :meth:`advance`.
        self._offset = timedelta()
        #: scope key -> (attempt count, window expiry)
        self._attempts: dict[str, tuple[int, datetime]] = {}
        #: scope key -> lockout expiry
        self._locks: dict[str, datetime] = {}

    def _now(self) -> datetime:
        return self._base_now() + self._offset

    def advance(self, delta: timedelta) -> None:
        """Move the throttle's clock forward, to test window and lockout expiry."""
        self._offset += delta

    def check(self, *, email: str, ip_address: str | None):  # type: ignore[no-untyped-def]
        from services.login_throttle import ThrottleStatus

        longest = 0
        for key in self._scopes(email, ip_address):
            expiry = self._locks.get(key)
            if expiry is None:
                continue
            remaining = (expiry - self._now()).total_seconds()
            if remaining <= 0:
                del self._locks[key]
                continue
            longest = max(longest, int(remaining))

        if longest > 0:
            return ThrottleStatus(blocked=True, retry_after_seconds=longest)
        return ThrottleStatus(blocked=False)

    def register_failure(self, *, email: str, ip_address: str | None):  # type: ignore[no-untyped-def]
        from services.login_throttle import ThrottleStatus

        locked = False
        for key in self._scopes(email, ip_address):
            count, expiry = self._attempts.get(key, (0, self._now() + self._window))
            if expiry <= self._now():
                count, expiry = 0, self._now() + self._window
            count += 1

            if count >= self._max_attempts:
                self._locks[key] = self._now() + self._lockout
                self._attempts.pop(key, None)
                locked = True
            else:
                self._attempts[key] = (count, expiry)

        if locked:
            return ThrottleStatus(blocked=True, retry_after_seconds=int(self._lockout.total_seconds()))
        return ThrottleStatus(blocked=False)

    def reset(self, *, email: str, ip_address: str | None) -> None:
        for key in self._scopes(email, ip_address):
            self._attempts.pop(key, None)
            self._locks.pop(key, None)

    @staticmethod
    def _scopes(email: str, ip_address: str | None) -> list[str]:
        scopes = [f"email:{email.strip().lower()}"]
        if ip_address:
            scopes.append(f"ip:{ip_address}")
        return scopes


@pytest.fixture
def revocations() -> InMemoryRevocationStore:
    """A fresh in-memory token denylist per test."""
    return InMemoryRevocationStore()


@pytest.fixture
def throttle() -> InMemoryLoginThrottle:
    """A fresh in-memory login throttle per test."""
    return InMemoryLoginThrottle()


@pytest.fixture
def make_user(db_session: Session):  # type: ignore[no-untyped-def]
    """Factory creating persisted users with a known password.

    ``is_active`` is accepted as a convenience because most tests only care
    whether an account can sign in; it maps onto the ``status`` column, which is
    the real field. Pass ``status`` directly to build a suspended account.
    """
    from core.security import hash_password
    from models.user import User, UserRole, UserStatus

    def _make(
        *,
        # Note: `.local`/`.test` are special-use domains that email-validator
        # rejects, so fixtures use a domain that passes EmailStr validation.
        email: str = "amina.benali@example.com",
        password: str = "correct-horse-battery",
        first_name: str = "Amina",
        last_name: str = "Benali",
        phone: str | None = None,
        role: UserRole = UserRole.ADMINISTRATOR,
        is_active: bool = True,
        status: UserStatus | None = None,
        must_change_password: bool = False,
        last_login_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            email=email.lower(),
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            role=role,
            status=status or (UserStatus.ACTIVE if is_active else UserStatus.INACTIVE),
            must_change_password=must_change_password,
            last_login_at=last_login_at,
            hashed_password=hash_password(password),
        )
        if created_at is not None:
            # Set explicitly so ordering tests do not depend on wall-clock gaps
            # between rows inserted in the same millisecond.
            user.created_at = created_at
        db_session.add(user)
        db_session.commit()
        return user

    return _make


@pytest.fixture
def make_case(db_session: Session):  # type: ignore[no-untyped-def]
    """Factory creating persisted cases.

    Case numbers default to a per-call counter rather than a fixed string,
    because the column is unique — a fixed default would make the second case in
    any test fail for a reason the test is not about.
    """
    from models.case import Case, CasePriority, CaseStatus

    counter = itertools.count(1)

    def _make(
        *,
        case_number: str | None = None,
        title: str = "Benali v. Societe Atlas",
        description: str | None = None,
        category: str | None = None,
        status: CaseStatus = CaseStatus.OPEN,
        priority: CasePriority = CasePriority.MEDIUM,
        court_name: str | None = None,
        filing_date: date | None = None,
        next_hearing_date: date | None = None,
        assigned_lawyer_id: uuid.UUID | None = None,
        assigned_court_representative_id: uuid.UUID | None = None,
        created_by: uuid.UUID | None = None,
        created_at: datetime | None = None,
    ) -> Case:
        legal_case = Case(
            id=uuid.uuid4(),
            case_number=case_number or f"CASE-2026-{next(counter):04d}",
            title=title,
            description=description,
            category=category,
            status=status,
            priority=priority,
            court_name=court_name,
            filing_date=filing_date,
            next_hearing_date=next_hearing_date,
            assigned_lawyer_id=assigned_lawyer_id,
            assigned_court_representative_id=assigned_court_representative_id,
            created_by=created_by,
            updated_by=created_by,
        )
        if created_at is not None:
            # Set explicitly so ordering tests do not depend on wall-clock gaps
            # between rows inserted in the same millisecond.
            legal_case.created_at = created_at
        db_session.add(legal_case)
        db_session.commit()
        return legal_case

    return _make


# --------------------------------------------------------------------------- #
# Document fixtures
# --------------------------------------------------------------------------- #


class FakeObjectStream:
    """Test double for :class:`~services.document_storage.ObjectStream`."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield self._payload

    def close(self) -> None:
        self.closed = True


class InMemoryDocumentStorage:
    """Test double for :class:`~services.document_storage.DocumentStorageService`.

    Mirrors the real contract — write once per key, stream back, report metadata,
    and *never physically delete* — with a dict instead of MinIO. Keeping the
    "logical delete" behaviour in the double matters: a test asserting that a
    deleted document's bytes survive would otherwise pass against a double that
    simply removed them.
    """

    def __init__(self, bucket: str = "test-documents") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.logical_deletes: list[str] = []
        #: Set to raise on the next write, to exercise the storage-failure path.
        self.fail_next_upload = False

    def ensure_bucket(self) -> None:
        return None

    def upload_object(self, *, key: str, stream, size: int, content_type: str):  # type: ignore[no-untyped-def]
        from core.exceptions import DocumentStorageError
        from services.document_storage import StoredObjectInfo

        if self.fail_next_upload:
            self.fail_next_upload = False
            raise DocumentStorageError(detail="fake storage failure")

        stream.seek(0)
        self.objects[key] = stream.read()
        self.content_types[key] = content_type
        return StoredObjectInfo(
            key=key, size=size, content_type=content_type, last_modified=None, etag=None
        )

    def open_object(self, key: str) -> FakeObjectStream:
        from core.exceptions import DocumentStorageError

        if key not in self.objects:
            raise DocumentStorageError(detail=f"missing object {key!r}")
        return FakeObjectStream(self.objects[key])

    def object_metadata(self, key: str):  # type: ignore[no-untyped-def]
        from core.exceptions import DocumentStorageError
        from services.document_storage import StoredObjectInfo

        if key not in self.objects:
            raise DocumentStorageError(detail=f"missing object {key!r}")
        return StoredObjectInfo(
            key=key,
            size=len(self.objects[key]),
            content_type=self.content_types[key],
            last_modified=None,
            etag=None,
        )

    def delete_object(self, key: str, *, reason: str) -> None:
        # Logical only, exactly like the real service: the bytes stay.
        self.logical_deletes.append(key)


@pytest.fixture
def document_storage() -> InMemoryDocumentStorage:
    """A fresh in-memory object store per test."""
    return InMemoryDocumentStorage()


@pytest.fixture
def make_document(db_session: Session, document_storage: InMemoryDocumentStorage):  # type: ignore[no-untyped-def]
    """Factory creating persisted documents, with their version-1 row and bytes.

    Writes to the fake object store as well as the database, so a document built
    by this factory is downloadable — a fixture that only inserted metadata would
    make every download test fail for a reason it is not about.
    """
    from core.documents import build_storage_key, mime_type_for
    from models.document import Document, DocumentCategory, DocumentVersion
    from tests.helpers import PDF_BYTES

    counter = itertools.count(1)

    def _make(
        *,
        case_id: uuid.UUID,
        original_filename: str | None = None,
        extension: str = "pdf",
        category: DocumentCategory = DocumentCategory.OTHER,
        description: str | None = None,
        content: bytes | None = None,
        uploaded_by: uuid.UUID | None = None,
        created_at: datetime | None = None,
        deleted_at: datetime | None = None,
    ) -> Document:
        payload = PDF_BYTES if content is None else content
        index = next(counter)
        document_id = uuid.uuid4()
        stored_filename = f"{uuid.uuid4().hex}.{extension}"
        storage_key = build_storage_key(
            case_id=case_id,
            document_id=document_id,
            version=1,
            stored_filename=stored_filename,
        )
        filename = original_filename or f"document-{index}.{extension}"

        document = Document(
            id=document_id,
            case_id=case_id,
            original_filename=filename,
            stored_filename=stored_filename,
            file_extension=extension,
            mime_type=mime_type_for(extension),
            file_size=len(payload),
            storage_bucket=document_storage.bucket,
            storage_key=storage_key,
            category=category,
            description=description,
            version=1,
            uploaded_by=uploaded_by,
            deleted_at=deleted_at,
        )
        document.versions.append(
            DocumentVersion(
                id=uuid.uuid4(),
                document_id=document_id,
                version=1,
                original_filename=filename,
                stored_filename=stored_filename,
                file_extension=extension,
                mime_type=mime_type_for(extension),
                file_size=len(payload),
                storage_bucket=document_storage.bucket,
                storage_key=storage_key,
                uploaded_by=uploaded_by,
            )
        )
        if created_at is not None:
            # Set explicitly so ordering tests do not depend on wall-clock gaps
            # between rows inserted in the same millisecond.
            document.created_at = created_at
            document.versions[0].created_at = created_at

        db_session.add(document)
        db_session.commit()

        document_storage.objects[storage_key] = payload
        document_storage.content_types[storage_key] = mime_type_for(extension)
        return document

    return _make


@pytest.fixture
def auth_service(  # type: ignore[no-untyped-def]
    db_session: Session,
    revocations: InMemoryRevocationStore,
    throttle: InMemoryLoginThrottle,
):
    """An :class:`~services.auth.AuthService` wired to the test doubles."""
    from typing import cast

    from repositories.user import UserRepository
    from services.auth import AuthService
    from services.login_throttle import LoginThrottle
    from services.token_revocation import TokenRevocationStore

    return AuthService(
        UserRepository(db_session),
        cast(TokenRevocationStore, revocations),
        cast(LoginThrottle, throttle),
    )


@pytest.fixture
def api_client(
    db_session: Session,
    revocations: InMemoryRevocationStore,
    throttle: InMemoryLoginThrottle,
    document_storage: InMemoryDocumentStorage,
) -> Iterator[TestClient]:
    """A TestClient whose database, denylist, throttle, and object store are doubles."""
    from api.deps import get_document_storage, get_login_throttle, get_token_revocation_store
    from db.session import get_db
    from main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_token_revocation_store] = lambda: revocations
    app.dependency_overrides[get_login_throttle] = lambda: throttle
    app.dependency_overrides[get_document_storage] = lambda: document_storage
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
