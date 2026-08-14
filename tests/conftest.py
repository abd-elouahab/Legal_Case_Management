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
from contextlib import suppress
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
def db_engine():  # type: ignore[no-untyped-def]
    """A SQLite in-memory engine with the full schema created.

    Separate from :func:`db_session` because two consumers need the *engine*
    rather than a session: the request-scoped session below, and the WebSocket
    layer, which opens a short session per question because a connection
    outlives any request. Sharing one ``Session`` object between them would
    hand the same non-thread-safe object to a worker thread.
    """
    import models  # noqa: F401  -- registers models on Base.metadata
    from db.base import Base

    engine = create_engine(
        "sqlite://",
        # A single shared connection so the in-memory database survives across
        # sessions opened during one test — and is visible to every session,
        # whichever thread opens it.
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    """A SQLite in-memory session with the full schema created."""
    session_factory = sessionmaker(
        bind=db_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


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


# --------------------------------------------------------------------------- #
# OCR fixtures
# --------------------------------------------------------------------------- #


class FakeOcrEngine:
    """Test double for an :class:`~services.ocr_engine.OcrEngine`.

    A real Tesseract install is a *system* dependency — a binary, plus a language
    pack per language — so a suite that needed one would only run on machines
    that had it. This double keeps the contract (a name, a version, an
    availability probe, a format policy, and one ``extract`` call) and lets a test
    choose the outcome, which is what makes the failure paths testable at all: an
    engine timeout and a corrupted PDF are not things a fixture can produce on
    demand from a real binary.
    """

    name = "fake"

    def __init__(self) -> None:
        from services.ocr_engine import ExtractedPage

        self.available = True
        self.engine_version = "5.0.0-fake"
        self.detected_language = "eng+fra"
        #: Pages the next extraction returns. Replace to change the outcome.
        self.pages: list[ExtractedPage] = [
            ExtractedPage(page_number=1, text="Contrat de bail commercial.", confidence=91.5)
        ]
        #: Set to an exception instance to make the next extraction raise it.
        self.raises: Exception | None = None
        #: Every (content length, extension) pair the engine was asked to read.
        self.calls: list[tuple[int, str]] = []

    def version(self) -> str | None:
        return self.engine_version if self.available else None

    def is_available(self) -> bool:
        return self.available

    def supports(self, extension: str) -> bool:
        from core.ocr import is_supported

        return is_supported(extension)

    def extract(self, content: bytes, *, extension: str):  # type: ignore[no-untyped-def]
        from core.ocr import mean_confidence
        from services.ocr_engine import Extraction, OcrUnsupportedFormatError

        self.calls.append((len(content), extension))

        if self.raises is not None:
            raise self.raises
        if not self.supports(extension):
            raise OcrUnsupportedFormatError(f"OCR does not support {extension!r} files.")

        return Extraction(
            pages=list(self.pages),
            engine=self.name,
            engine_version=self.engine_version,
            detected_language=self.detected_language,
            confidence=mean_confidence(page.confidence for page in self.pages),
        )


class RecordingOcrQueue:
    """Test double for :class:`~services.ocr_queue.OcrJobQueue`.

    Records every job **and** runs it on the calling thread. Both halves matter:
    recording is how a test asserts that an upload scheduled work without waiting
    for it, and running inline is how the rest of the pipeline becomes testable
    without a thread — a test that has to wait for a worker is a test that will
    eventually be flaky.

    It keeps the contract that a failing job must not fail the caller, so a test
    passing against this double exercises the same error handling production
    does. ``run_inline`` can be switched off to observe a run in its ``pending``
    state.
    """

    def __init__(self, runner=None) -> None:  # type: ignore[no-untyped-def]
        self.jobs: list[object] = []
        self.run_inline = True
        self._runner = runner

    def enqueue(self, job: object) -> None:
        self.jobs.append(job)
        if self.run_inline and self._runner is not None:
            # Swallowed, exactly as the real queues do: a failing job must never
            # fail the caller that scheduled it.
            with suppress(Exception):
                self._runner(job)


@pytest.fixture
def ocr_engine() -> FakeOcrEngine:
    """A fresh, controllable OCR engine per test."""
    return FakeOcrEngine()


@pytest.fixture
def ocr_queue(  # type: ignore[no-untyped-def]
    db_session: Session,
    document_storage: InMemoryDocumentStorage,
    ocr_engine: FakeOcrEngine,
    indexing_service,
):
    """A queue that records jobs and runs them against the test doubles.

    The runner builds an :class:`~services.ocr.OcrService` the same way
    :func:`services.ocr_worker.run_ocr_job` does — real repositories, real
    timeline, real access policy, **and a real indexing scheduler** — but on the
    test session with the fake storage and engine. So the pipeline under test is
    the production one; only the genuinely external things are substituted.

    The indexing service is passed for the same reason the timeline is: a
    completed extraction schedules an index in production, and a runner that
    omitted it would make every "extraction hands off to indexing" assertion a
    test of the fixture rather than of the platform.
    """
    from typing import cast

    from repositories.case import CaseRepository
    from repositories.document import DocumentRepository
    from repositories.ocr import OcrRepository
    from repositories.timeline import TimelineRepository
    from services.document_storage import DocumentStorageService
    from services.ocr import OcrService
    from services.ocr_engine import OcrEngine
    from services.ocr_queue import NullOcrJobQueue, OcrJob
    from services.timeline import TimelineService

    def run(job: OcrJob) -> None:
        OcrService(
            OcrRepository(db_session),
            DocumentRepository(db_session),
            cast(DocumentStorageService, document_storage),
            cast(OcrEngine, ocr_engine),
            # A worker may not queue more work, exactly as in production.
            NullOcrJobQueue(),
            timeline=TimelineService(TimelineRepository(db_session), CaseRepository(db_session)),
            indexing=indexing_service,
        ).process(job)

    return RecordingOcrQueue(run)


@pytest.fixture
def ocr_service(  # type: ignore[no-untyped-def]
    db_session: Session,
    document_storage: InMemoryDocumentStorage,
    ocr_engine: FakeOcrEngine,
    ocr_queue: RecordingOcrQueue,
    indexing_service,
):
    """An :class:`~services.ocr.OcrService` wired to the test doubles."""
    from typing import cast

    from repositories.case import CaseRepository
    from repositories.document import DocumentRepository
    from repositories.ocr import OcrRepository
    from repositories.timeline import TimelineRepository
    from services.document_storage import DocumentStorageService
    from services.ocr import OcrService
    from services.ocr_engine import OcrEngine
    from services.ocr_queue import OcrJobQueue
    from services.timeline import TimelineService

    return OcrService(
        OcrRepository(db_session),
        DocumentRepository(db_session),
        cast(DocumentStorageService, document_storage),
        cast(OcrEngine, ocr_engine),
        cast(OcrJobQueue, ocr_queue),
        timeline=TimelineService(TimelineRepository(db_session), CaseRepository(db_session)),
        indexing=indexing_service,
    )


@pytest.fixture
def make_ocr_result(db_session: Session):  # type: ignore[no-untyped-def]
    """Factory creating persisted OCR runs, with their pages.

    Bypasses the service on purpose: tests about *reading* a run need rows in
    controlled states — a run that failed three days ago, a run mid-processing —
    and going through the pipeline would tie them to whatever the engine happens
    to return today.
    """
    from models.ocr import OcrPage, OcrResult, OcrStatus

    def _make(
        *,
        document_id: uuid.UUID,
        document_version: int = 1,
        status: OcrStatus = OcrStatus.COMPLETED,
        pages: list[str] | None = None,
        engine: str | None = "fake",
        engine_version: str | None = "5.0.0-fake",
        detected_language: str | None = "eng+fra",
        confidence: float | None = 92.0,
        duration_ms: int | None = 1250,
        attempt_count: int = 1,
        error_code: str | None = None,
        error_message: str | None = None,
        requested_by: uuid.UUID | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> OcrResult:
        texts = ["Page one text." ] if pages is None else pages
        terminal = status in {OcrStatus.COMPLETED, OcrStatus.FAILED}

        result = OcrResult(
            id=uuid.uuid4(),
            document_id=document_id,
            document_version=document_version,
            status=status,
            engine=engine,
            engine_version=engine_version,
            detected_language=detected_language,
            page_count=len(texts) if status is OcrStatus.COMPLETED else None,
            confidence=confidence if status is OcrStatus.COMPLETED else None,
            started_at=started_at
            or (datetime.now(UTC) if status is not OcrStatus.PENDING else None),
            finished_at=finished_at or (datetime.now(UTC) if terminal else None),
            duration_ms=duration_ms if terminal else None,
            attempt_count=attempt_count,
            error_code=error_code,
            error_message=error_message,
            requested_by=requested_by,
        )

        if status is OcrStatus.COMPLETED:
            for number, text in enumerate(texts, start=1):
                result.pages.append(
                    OcrPage(
                        id=uuid.uuid4(),
                        ocr_result_id=result.id,
                        page_number=number,
                        text=text,
                        confidence=confidence,
                    )
                )

        if created_at is not None:
            # Set explicitly so ordering and window tests do not depend on
            # wall-clock gaps between rows inserted in the same millisecond.
            result.created_at = created_at

        db_session.add(result)
        db_session.commit()
        return result

    return _make


# --------------------------------------------------------------------------- #
# Document indexing fixtures
# --------------------------------------------------------------------------- #


class FakeEmbedder:
    """Test double for an :class:`~services.embedding.Embedder`.

    The real model is ``BAAI/bge-m3`` — roughly two gigabytes, fetched from the
    network on first use — so a suite that needed one would download it once per
    CI machine and take minutes to start. This double keeps the contract (a model
    name, a vector width, an availability probe, and one ``embed`` call) and lets
    a test choose the outcome, which is what makes the failure paths testable at
    all: an unavailable model and a dimension mismatch are not things a real
    model produces on demand.

    **The vectors are deterministic and derived from the text**, which is not
    incidental: the spec requires the real embedder to be deterministic, and
    several tests assert that re-indexing unchanged text produces the same
    vectors. A random double would make that assertion vacuous. This is a fixture,
    not an embedding algorithm — it is not, and must not become, importable from
    the application.
    """

    name = "fake-embedder"

    def __init__(self) -> None:
        self.model_name = "fake/test-embedder"
        self.available = True
        self.width = 8
        #: Set to an exception instance to make the next call raise it.
        self.raises: Exception | None = None
        #: Every batch of texts the embedder was asked to encode.
        self.calls: list[list[str]] = []

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def dimensions(self) -> int:
        return self.width

    def is_available(self) -> bool:
        return self.available

    def embed(self, texts):  # type: ignore[no-untyped-def]
        import hashlib

        from services.embedding import EmbeddingBatch

        self.calls.append(list(texts))

        if self.raises is not None:
            raise self.raises

        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [digest[index] / 255.0 for index in range(self.width)]
            # Unit length, like the real embedder's `normalize_embeddings=True`,
            # so a test that checks the magnitude checks something true of both.
            norm = sum(value * value for value in raw) ** 0.5 or 1.0
            vectors.append([value / norm for value in raw])

        return EmbeddingBatch(
            vectors=vectors, model=self.model_name, dimensions=self.width
        )


class InMemoryVectorStore:
    """Test double for a :class:`~services.vector_store.VectorStore`.

    Mirrors the real contract — upsert by point id, delete by document version,
    count, describe — with a dict instead of Qdrant. Two behaviours are kept
    faithfully because tests depend on them being the *same* as production:

    * **upsert replaces by id.** That is what makes a derived point id an
      idempotency guarantee rather than a hope, and a double that appended would
      let a duplicate-vector bug pass.
    * **delete is filtered by document *and* version.** A double that deleted by
      document alone would hide the bug where re-indexing version 2 wipes version
      1's vectors.
    """

    def __init__(self, collection_name: str = "test-chunks") -> None:
        self.collection_name = collection_name
        self.points: dict[str, object] = {}
        self.available = True
        self.created_with: int | None = None
        #: Set to an exception instance to make the next write raise it.
        self.raises: Exception | None = None

    @property
    def collection(self) -> str:
        return self.collection_name

    def is_available(self) -> bool:
        return self.available

    def ensure_collection(self, *, dimensions: int) -> None:
        if self.raises is not None:
            raise self.raises
        self.created_with = dimensions

    def upsert(self, points) -> int:  # type: ignore[no-untyped-def]
        if self.raises is not None:
            raise self.raises
        for point in points:
            self.points[str(point.id)] = point
        return len(points)

    def delete_document_version(self, document_id: uuid.UUID, version: int) -> None:
        if self.raises is not None:
            raise self.raises
        for key, point in list(self.points.items()):
            payload = point.payload  # type: ignore[attr-defined]
            if (
                payload["document_id"] == str(document_id)
                and payload["document_version"] == version
            ):
                del self.points[key]

    def count_document_version(self, document_id: uuid.UUID, version: int) -> int:
        return len(self.for_version(document_id, version))

    def collection_info(self):  # type: ignore[no-untyped-def]
        from services.vector_store import CollectionInfo

        if not self.available:
            return CollectionInfo(
                name=self.collection_name, vector_count=None, dimensions=None, exists=False
            )
        return CollectionInfo(
            name=self.collection_name,
            vector_count=len(self.points),
            dimensions=self.created_with,
            exists=self.created_with is not None,
        )

    # ------------------------------------------------------ test affordances #

    def for_version(self, document_id: uuid.UUID, version: int) -> list[object]:
        """Every point belonging to one version of one document, in chunk order."""
        matching = [
            point
            for point in self.points.values()
            if point.payload["document_id"] == str(document_id)  # type: ignore[attr-defined]
            and point.payload["document_version"] == version  # type: ignore[attr-defined]
        ]
        return sorted(matching, key=lambda point: point.payload["chunk_number"])  # type: ignore[attr-defined,no-any-return]


class RecordingIndexQueue:
    """Test double for a :class:`~services.job_queue.JobQueue`.

    Records every job **and** runs it on the calling thread. Both halves matter:
    recording is how a test asserts that a completed extraction scheduled work
    without waiting for it, and running inline is how the rest of the pipeline
    becomes testable without a thread — a test that has to wait for a worker is a
    test that will eventually be flaky.

    It keeps the contract that a failing job must not fail the caller, so a test
    passing against this double exercises the same error handling production
    does. ``run_inline`` can be switched off to observe a run in its ``pending``
    state.
    """

    def __init__(self, runner=None) -> None:  # type: ignore[no-untyped-def]
        self.jobs: list[object] = []
        self.run_inline = True
        self._runner = runner

    def enqueue(self, job: object) -> None:
        self.jobs.append(job)
        if self.run_inline and self._runner is not None:
            with suppress(Exception):
                self._runner(job)


@pytest.fixture
def embedder() -> FakeEmbedder:
    """A fresh, controllable embedding model per test."""
    return FakeEmbedder()


@pytest.fixture
def vector_store() -> InMemoryVectorStore:
    """A fresh in-memory vector store per test."""
    return InMemoryVectorStore()


@pytest.fixture
def index_queue(  # type: ignore[no-untyped-def]
    db_session: Session,
    embedder: FakeEmbedder,
    vector_store: InMemoryVectorStore,
):
    """A queue that records jobs and runs them against the test doubles.

    The runner builds an :class:`~services.indexing.IndexingService` the same way
    :func:`services.indexing_worker.run_index_job` does — real repositories, the
    real chunker, real timeline, real access policy — but on the test session with
    the fake embedder and vector store. So the pipeline under test is the
    production one; only the two genuinely external things are substituted.

    **The chunker is real, deliberately.** It is a pure function of text with no
    network, no model, and no binary behind it, so substituting it would mean
    every chunking guarantee the spec asks for — page order, page numbering,
    overlap, semantic boundaries — was asserted against a fake.
    """
    from typing import cast

    from repositories.case import CaseRepository
    from repositories.document import DocumentRepository
    from repositories.indexing import IndexingRepository
    from repositories.ocr import OcrRepository
    from repositories.timeline import TimelineRepository
    from services.chunking import get_chunker
    from services.embedding import Embedder
    from services.indexing import IndexingService, IndexJob
    from services.job_queue import NullJobQueue
    from services.timeline import TimelineService
    from services.vector_store import VectorStore

    def run(job: IndexJob) -> None:
        IndexingService(
            IndexingRepository(db_session),
            DocumentRepository(db_session),
            OcrRepository(db_session),
            get_chunker(),
            cast(Embedder, embedder),
            cast(VectorStore, vector_store),
            # A worker may not queue more work, exactly as in production.
            NullJobQueue(name="indexing"),
            timeline=TimelineService(TimelineRepository(db_session), CaseRepository(db_session)),
        ).process(job)

    return RecordingIndexQueue(run)


@pytest.fixture
def indexing_service(  # type: ignore[no-untyped-def]
    db_session: Session,
    embedder: FakeEmbedder,
    vector_store: InMemoryVectorStore,
    index_queue: RecordingIndexQueue,
):
    """An :class:`~services.indexing.IndexingService` wired to the test doubles."""
    from typing import cast

    from repositories.case import CaseRepository
    from repositories.document import DocumentRepository
    from repositories.indexing import IndexingRepository
    from repositories.ocr import OcrRepository
    from repositories.timeline import TimelineRepository
    from services.chunking import get_chunker
    from services.embedding import Embedder
    from services.indexing import IndexingService, IndexJob
    from services.job_queue import JobQueue
    from services.timeline import TimelineService
    from services.vector_store import VectorStore

    return IndexingService(
        IndexingRepository(db_session),
        DocumentRepository(db_session),
        OcrRepository(db_session),
        get_chunker(),
        cast(Embedder, embedder),
        cast(VectorStore, vector_store),
        cast("JobQueue[IndexJob]", index_queue),
        timeline=TimelineService(TimelineRepository(db_session), CaseRepository(db_session)),
    )


@pytest.fixture
def make_document_index(db_session: Session):  # type: ignore[no-untyped-def]
    """Factory creating persisted indexing runs.

    Bypasses the service on purpose: tests about *reading* an index need rows in
    controlled states — a run that failed three days ago, a run mid-index, a run
    built with a superseded model — and going through the pipeline would tie them
    to whatever the embedder happens to return today.
    """
    from models.indexing import DocumentIndex, IndexStatus

    def _make(
        *,
        document_id: uuid.UUID,
        case_id: uuid.UUID,
        document_version: int = 1,
        status: IndexStatus = IndexStatus.INDEXED,
        chunk_count: int | None = 3,
        page_count: int | None = 1,
        character_count: int | None = 240,
        embedding_model: str | None = "fake/test-embedder",
        embedding_dimensions: int | None = 8,
        vector_collection: str | None = "test-chunks",
        chunk_size: int | None = 1000,
        chunk_overlap: int | None = 200,
        detected_language: str | None = "fr",
        duration_ms: int | None = 4200,
        attempt_count: int = 1,
        error_code: str | None = None,
        error_message: str | None = None,
        requested_by: uuid.UUID | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> DocumentIndex:
        terminal = status in {IndexStatus.INDEXED, IndexStatus.FAILED}
        succeeded = status is IndexStatus.INDEXED

        index = DocumentIndex(
            id=uuid.uuid4(),
            document_id=document_id,
            document_version=document_version,
            case_id=case_id,
            status=status,
            chunk_count=chunk_count if succeeded else None,
            page_count=page_count if succeeded else None,
            character_count=character_count if succeeded else None,
            embedding_model=embedding_model if succeeded else None,
            embedding_dimensions=embedding_dimensions if succeeded else None,
            vector_collection=vector_collection if succeeded else None,
            chunk_size=chunk_size if succeeded else None,
            chunk_overlap=chunk_overlap if succeeded else None,
            detected_language=detected_language if succeeded else None,
            started_at=started_at
            or (datetime.now(UTC) if status is not IndexStatus.PENDING else None),
            finished_at=finished_at or (datetime.now(UTC) if terminal else None),
            duration_ms=duration_ms if terminal else None,
            attempt_count=attempt_count,
            error_code=error_code,
            error_message=error_message,
            requested_by=requested_by,
        )

        if created_at is not None:
            # Set explicitly so ordering and window tests do not depend on
            # wall-clock gaps between rows inserted in the same millisecond.
            index.created_at = created_at

        db_session.add(index)
        db_session.commit()
        return index

    return _make


# --------------------------------------------------------------------------- #
# Timeline fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def timeline_service(db_session: Session):  # type: ignore[no-untyped-def]
    """A :class:`~services.timeline.TimelineService` on the test database.

    The real repository against SQLite, so the search, filter, sort, pagination,
    and scope SQL is genuinely exercised — there is nothing about the timeline
    that would need a double.
    """
    from repositories.case import CaseRepository
    from repositories.timeline import TimelineRepository
    from services.timeline import TimelineService

    return TimelineService(TimelineRepository(db_session), CaseRepository(db_session))


@pytest.fixture
def make_timeline_event(db_session: Session):  # type: ignore[no-untyped-def]
    """Factory creating persisted timeline events directly.

    Bypasses the service on purpose: tests about *reading* a timeline need rows
    with controlled timestamps and actors, and going through ``record`` would tie
    them to whatever the publishing services happen to write today.
    """
    from core.timeline import default_title
    from models.timeline import TimelineEvent, TimelineEventType

    counter = itertools.count(1)

    def _make(
        *,
        case_id: uuid.UUID,
        event_type: TimelineEventType = TimelineEventType.CASE_UPDATED,
        title: str | None = None,
        description: str | None = None,
        actor: object | None = None,
        metadata: dict[str, object] | None = None,
        created_at: datetime | None = None,
    ) -> TimelineEvent:
        index = next(counter)
        event = TimelineEvent(
            id=uuid.uuid4(),
            case_id=case_id,
            event_type=event_type.value,
            title=title or default_title(event_type.value),
            description=description if description is not None else f"Event {index}.",
            actor_id=getattr(actor, "id", None),
            actor_name=getattr(actor, "full_name", None),
            actor_role=getattr(getattr(actor, "role", None), "value", None),
            event_metadata=metadata or {},
        )
        if created_at is not None:
            # Set explicitly so ordering tests do not depend on wall-clock gaps
            # between rows inserted in the same millisecond.
            event.created_at = created_at

        db_session.add(event)
        db_session.commit()
        return event

    return _make


class InMemoryVectorSearcher:
    """Test double for a :class:`~services.vector_search.VectorSearcher`.

    Backed by **the same points** :class:`InMemoryVectorStore` holds, which is
    what makes an end-to-end test possible without Qdrant: a document uploaded,
    extracted, and indexed through the real pipeline becomes searchable through
    this, exactly as it would in production.

    Three behaviours are mirrored faithfully because tests depend on them being
    the same as Qdrant's, and a double that got any of them wrong would let a
    real bug pass:

    * **filters are applied before the limit**, not after. Filtering afterwards
      would return fewer results than asked for and would hide the whole class of
      bug where an authorization filter is dropped;
    * **the case filter's two empty forms are distinct.** ``None`` matches
      everything, an empty set matches nothing — the single most dangerous
      confusion this feature could make;
    * **results come back in descending score order**, so a test about ranking is
      testing the ranker rather than dictionary iteration order.

    Scores are real cosine similarities over :class:`FakeEmbedder`'s vectors, so
    a query whose text exactly matches a passage scores 1.0 and a test can assert
    relevance rather than merely presence.
    """

    def __init__(self, store: InMemoryVectorStore) -> None:
        self._store = store
        self.available = True
        #: Set to an exception instance to make the next search raise it.
        self.raises: Exception | None = None
        #: The filters of the most recent search, for assertions about scoping.
        self.last_filters: object | None = None
        #: Every search's (limit, offset), for assertions about pagination.
        self.calls: list[tuple[int, int]] = []

    @property
    def collection(self) -> str:
        return self._store.collection

    def is_available(self) -> bool:
        return self.available

    def search(  # type: ignore[no-untyped-def]
        self, vector, *, filters, limit, offset=0, score_threshold=None
    ):
        from services.vector_search import VectorMatch

        self.last_filters = filters
        self.calls.append((limit, offset))

        if self.raises is not None:
            raise self.raises

        if filters.matches_nothing():
            return []

        matches = []
        for point in self._store.points.values():
            payload = point.payload  # type: ignore[attr-defined]
            if not self._passes(payload, filters):
                continue
            score = sum(
                left * right
                for left, right in zip(vector, point.vector, strict=False)  # type: ignore[attr-defined]
            )
            if score_threshold is not None and score < score_threshold:
                continue
            matches.append(
                VectorMatch(point_id=str(point.id), score=score, payload=dict(payload))
            )

        matches.sort(key=lambda match: -match.score)
        return matches[offset : offset + limit]

    @staticmethod
    def _passes(payload, filters) -> bool:  # type: ignore[no-untyped-def]
        """Whether one point satisfies every filter, ANDed exactly as Qdrant does."""
        from datetime import datetime

        if filters.case_ids is not None and payload["case_id"] not in {
            str(case_id) for case_id in filters.case_ids
        }:
            return False
        if filters.document_ids is not None and payload["document_id"] not in {
            str(document_id) for document_id in filters.document_ids
        }:
            return False
        if (
            filters.document_version is not None
            and payload["document_version"] != filters.document_version
        ):
            return False
        if filters.languages and payload.get("language") not in filters.languages:
            return False
        if (
            filters.embedding_model is not None
            and payload.get("embedding_model") != filters.embedding_model
        ):
            return False
        if filters.indexed_from is not None or filters.indexed_to is not None:
            indexed_at = datetime.fromisoformat(payload["indexed_at"])
            if filters.indexed_from is not None and indexed_at < filters.indexed_from:
                return False
            if filters.indexed_to is not None and indexed_at > filters.indexed_to:
                return False
        return True


@pytest.fixture
def vector_searcher(vector_store: InMemoryVectorStore) -> InMemoryVectorSearcher:
    """A searcher reading the same points the indexing double writes."""
    return InMemoryVectorSearcher(vector_store)


@pytest.fixture
def search_metrics():  # type: ignore[no-untyped-def]
    """A fresh metrics recorder per test.

    Per test rather than the process-wide one, because the real recorder counts
    for the life of the process — assertions about "one search was recorded"
    would otherwise depend on how many searches every earlier test ran.
    """
    from services.search_metrics import InMemorySearchMetrics

    return InMemorySearchMetrics()


@pytest.fixture
def search_service(  # type: ignore[no-untyped-def]
    db_session: Session,
    embedder: FakeEmbedder,
    vector_searcher: InMemoryVectorSearcher,
    search_metrics,
):
    """A :class:`~services.search.SearchService` wired to the test doubles.

    Real repositories, the real ranker, and the real access policy — only the
    embedding model and the vector database are substituted, which are the two
    genuinely external things. The ranker is deliberately real for the same
    reason the chunker is in ``index_queue``: it is a pure function, and a fake
    would make every ordering guarantee vacuous.
    """
    from typing import cast

    from repositories.case import CaseRepository
    from repositories.document import DocumentRepository
    from repositories.search import SearchRepository
    from services.embedding import Embedder
    from services.search import SearchService
    from services.search_metrics import SearchMetricsRecorder
    from services.search_ranking import get_ranker
    from services.vector_search import VectorSearcher

    return SearchService(
        SearchRepository(db_session),
        DocumentRepository(db_session),
        CaseRepository(db_session),
        cast(Embedder, embedder),
        cast(VectorSearcher, vector_searcher),
        get_ranker(),
        metrics=cast(SearchMetricsRecorder, search_metrics),
    )


# --------------------------------------------------------------------------- #
# RAG pipeline fixtures
# --------------------------------------------------------------------------- #


class ScriptedLLMProvider:
    """Test double for an :class:`~services.llm.LLMProvider`.

    A real provider is a **metered network service behind a credential**, so a
    suite that used one would run only on machines with an API key, cost money
    per assertion, and — worst — be non-deterministic about the very thing these
    tests are checking: whether the platform handles what comes back. This double
    keeps the contract (a name, a model, an availability probe, token counting,
    one ``generate`` call, and one ``stream``) and lets a test choose the
    outcome, which is what makes the failure paths testable at all. A timeout, a
    refusal, an empty completion, and an answer citing a source that was never
    supplied are not things a real model produces on demand.

    **It records the prompt it was given**, which is how the tests assert the
    things that matter most about this feature: that the retrieved passages
    reached the model, that the caller's question did, and that a passage the
    caller may not read never does.
    """

    name = "scripted"

    def __init__(self) -> None:
        self.model_name = "scripted/test-model"
        self.available = True
        #: What the next generation returns. Cites source 1 by default, because
        #: an uncited default would make every citation assertion a test of the
        #: fixture rather than of the pipeline.
        self.answer = "Le loyer est payable le premier jour de chaque mois [1]."
        #: Set to an exception instance to make the next call raise it.
        self.raises: Exception | None = None
        #: Set to a callable to compute the answer from (system, prompt).
        self.reply: Callable[[str, str], str] | None = None
        self.prompt_tokens: int | None = 120
        self.completion_tokens: int | None = 40
        self.finish_reason: str | None = "STOP"
        self.truncated = False
        #: Every (system, prompt) pair the provider was handed.
        self.calls: list[tuple[str, str]] = []
        #: Fragments :meth:`stream` yields. ``None`` streams the whole answer as
        #: one fragment, which is what a provider that cannot stream effectively
        #: does. Set it to a list to exercise a genuinely incremental reply —
        #: which is the only way to test that the platform withholds the refusal
        #: sentinel while it is still a prefix.
        self.stream_chunks: list[str] | None = None
        #: Set to an exception to make :meth:`stream` fail. Raised **before** the
        #: first fragment, so it exercises the fallback to a whole answer; set
        #: ``stream_raises_after`` to fail mid-answer instead, which must not fall
        #: back because text has already been delivered.
        self.stream_raises: Exception | None = None
        self.stream_raises_after: int = 0
        #: How many times :meth:`stream` was entered.
        self.stream_calls = 0

    @property
    def model(self) -> str:
        return self.model_name

    def is_available(self) -> bool:
        return self.available

    def count_tokens(self, text: str) -> int | None:
        # A crude but deterministic stand-in: tests assert that a count reaches
        # the response, never what it is.
        return len(text) // 4

    def generate(  # type: ignore[no-untyped-def]
        self,
        *,
        system: str,
        prompt: str,
        temperature=None,
        max_output_tokens=None,
        timeout_seconds=None,
    ):
        from services.llm import LLMCompletion

        self.calls.append((system, prompt))

        if self.raises is not None:
            raise self.raises

        text = self.reply(system, prompt) if self.reply is not None else self.answer

        total = None
        if self.prompt_tokens is not None or self.completion_tokens is not None:
            total = (self.prompt_tokens or 0) + (self.completion_tokens or 0)

        return LLMCompletion(
            text=text,
            provider=self.name,
            model=self.model_name,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=total,
            finish_reason=self.finish_reason,
            truncated=self.truncated,
        )

    def stream(  # type: ignore[no-untyped-def]
        self,
        *,
        system: str,
        prompt: str,
        temperature=None,
        max_output_tokens=None,
        timeout_seconds=None,
    ) -> Iterator[str]:
        self.stream_calls += 1

        if self.stream_raises is not None and self.stream_raises_after == 0:
            raise self.stream_raises

        if self.stream_chunks is not None:
            self.calls.append((system, prompt))
            for index, chunk in enumerate(self.stream_chunks):
                if (
                    self.stream_raises is not None
                    and index >= self.stream_raises_after > 0
                ):
                    raise self.stream_raises
                yield chunk
            return

        completion = self.generate(
            system=system,
            prompt=prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        yield completion.text


@pytest.fixture
def llm_provider() -> ScriptedLLMProvider:
    """A fresh, controllable language-model provider per test."""
    return ScriptedLLMProvider()


@pytest.fixture
def prompt_library():  # type: ignore[no-untyped-def]
    """The **real** prompt library, reading the templates the platform ships.

    Deliberately real, for the same reason the chunker and the ranker are in the
    indexing and search fixtures: rendering a Jinja template is a pure function
    of files under source control, with no network, no model, and no binary
    behind it. Substituting it would mean every claim the spec makes about the
    prompt — that it forbids answering outside the context, that it names the
    insufficiency sentinel, that it asks for citations, that it fences untrusted
    text — was asserted against a fake.
    """
    from services.prompts import JinjaPromptLibrary

    return JinjaPromptLibrary()


@pytest.fixture
def rag_metrics():  # type: ignore[no-untyped-def]
    """A fresh metrics recorder per test.

    Per test rather than the process-wide one, because the real recorder counts
    for the life of the process — assertions about "one question was recorded"
    would otherwise depend on how many questions every earlier test asked.
    """
    from services.rag_metrics import InMemoryRagMetrics

    return InMemoryRagMetrics()


@pytest.fixture
def rag_service(  # type: ignore[no-untyped-def]
    search_service,
    prompt_library,
    llm_provider: ScriptedLLMProvider,
    rag_metrics,
):
    """A :class:`~services.rag.RagService` wired to the test doubles.

    **The search service is the real one**, on the real repositories, with the
    real access policy — only the embedding model, the vector database, and the
    language model are substituted, which are the three genuinely external
    things. That matters more here than anywhere else in the suite: the
    pipeline's entire authorization story is "retrieval goes through the search
    service", and a faked search service would make every authorization
    assertion in these tests vacuous.
    """
    from services.rag import RagService

    return RagService(search_service, prompt_library, llm_provider, metrics=rag_metrics)


# --------------------------------------------------------------------------- #
# AI Legal Assistant fixtures
# --------------------------------------------------------------------------- #


class ScriptedFollowUpSuggester:
    """Test double for a :class:`~services.suggestions.FollowUpSuggester`.

    The real one is a **second metered model call**, and substituting it here is
    what keeps every assistant test free, fast, and deterministic — a suite whose
    conversation assertions depended on what a model felt like proposing would be
    a suite that fails for reasons nobody can fix.

    It records what it was asked, which is how the tests assert the two things
    that actually matter: that an **ungrounded** answer is never followed by
    suggestions, and that the suggester is handed the answer and its citations
    rather than a way to reach a document.
    """

    name = "scripted"

    def __init__(self) -> None:
        self.available = True
        self.suggestions: list[str] = ["Quelle est la durée du bail ?"]
        #: Set to an exception to make the next call raise — which must never
        #: cost the answer it would have followed.
        self.raises: Exception | None = None
        #: Every (question, answer, citation count, language) it was handed.
        self.calls: list[tuple[str, str, int, str]] = []

    def is_available(self) -> bool:
        return self.available

    def suggest(  # type: ignore[no-untyped-def]
        self, *, question: str, answer: str, citations, language: str
    ) -> list[str]:
        self.calls.append((question, answer, len(citations), language))

        if self.raises is not None:
            raise self.raises
        if not citations:
            return []
        return list(self.suggestions)


@pytest.fixture
def follow_up_suggester() -> ScriptedFollowUpSuggester:
    """A fresh, controllable follow-up suggester per test."""
    return ScriptedFollowUpSuggester()


@pytest.fixture
def assistant_metrics():  # type: ignore[no-untyped-def]
    """A fresh assistant metrics recorder per test.

    Per test rather than the process-wide one, for the reason ``rag_metrics``
    gives: the real recorder counts for the life of the process, so "one message
    was recorded" would otherwise depend on how many every earlier test sent.
    """
    from services.assistant_metrics import InMemoryAssistantMetrics

    return InMemoryAssistantMetrics()


@pytest.fixture
def assistant_service(  # type: ignore[no-untyped-def]
    db_session: Session,
    rag_service,
    follow_up_suggester: ScriptedFollowUpSuggester,
    assistant_metrics,
):
    """An :class:`~services.assistant.AssistantService` wired to the test doubles.

    **The RAG service is the real one**, on the real search service, on the real
    repositories, with the real access policy — only the embedding model, the
    vector database, the language model, and the suggester are substituted. That
    matters here for the same reason it matters in ``rag_service``: this
    feature's whole claim is that it delegates every answer to the pipeline, and
    a faked pipeline would make every one of those assertions vacuous.
    """
    from repositories.conversation import ConversationRepository
    from services.assistant import AssistantService

    return AssistantService(
        ConversationRepository(db_session),
        rag_service,
        suggester=follow_up_suggester,  # type: ignore[arg-type]
        metrics=assistant_metrics,
    )


@pytest.fixture
def make_conversation(db_session: Session):  # type: ignore[no-untyped-def]
    """Create a conversation directly, bypassing the service.

    For tests about *reading* — the list, the transcript, ownership — which
    should not have to spend a pipeline run each to arrange a fixture.
    """
    from models.conversation import Conversation, ConversationStatus

    def _make(  # type: ignore[no-untyped-def]
        *,
        owner,
        title: str = "Bail commercial",
        status: ConversationStatus = ConversationStatus.ACTIVE,
        case_id=None,
        language: str | None = None,
    ) -> Conversation:
        conversation = Conversation(
            owner_id=owner.id,
            title=title,
            status=status,
            case_id=case_id,
            language=language,
        )
        db_session.add(conversation)
        db_session.commit()
        db_session.refresh(conversation)
        return conversation

    return _make


# --------------------------------------------------------------------------- #
# AI report generation fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def report_queue(db_session: Session, rag_service):  # type: ignore[no-untyped-def]
    """A queue that records report jobs and runs them against the test doubles.

    The runner builds a :class:`~services.report.ReportService` the same way
    :func:`services.report_worker.run_report_job` does — real repositories, real
    timeline, real access policy — but on the test session and with the test's
    RAG pipeline, whose only substituted parts are the embedder, the vector
    searcher, and the language model.

    **The pipeline is the real one**, and that matters more here than anywhere
    else in this feature's tests: the whole design claim is "a report section is
    a pipeline answer", and a faked pipeline would make every grounding,
    citation, and authorization assertion in these tests a test of the fixture.

    Reuses :class:`RecordingIndexQueue`, which is generic over its job type — the
    same reason :mod:`services.job_queue` is.
    """
    from repositories.case import CaseRepository
    from repositories.report import ReportRepository
    from repositories.timeline import TimelineRepository
    from services.job_queue import NullJobQueue
    from services.report import ReportJob, ReportService
    from services.timeline import TimelineService

    def run(job: ReportJob) -> None:
        ReportService(
            ReportRepository(db_session),
            CaseRepository(db_session),
            rag_service,
            # A worker may not queue more work, exactly as in production.
            NullJobQueue(name="reports"),
            timeline=TimelineService(TimelineRepository(db_session), CaseRepository(db_session)),
        ).process(job)

    return RecordingIndexQueue(run)


@pytest.fixture
def report_service(  # type: ignore[no-untyped-def]
    db_session: Session,
    rag_service,
    report_queue: RecordingIndexQueue,
):
    """A :class:`~services.report.ReportService` wired to the test doubles.

    Note what is *not* injected: no search service, no embedder, no vector
    searcher, and no prompt library. That is the feature's authorization story —
    the agent can reach a passage only through the pipeline — and a fixture that
    handed it one of those would make the tests pass against a service the
    application never builds.
    """
    from typing import cast

    from repositories.case import CaseRepository
    from repositories.report import ReportRepository
    from repositories.timeline import TimelineRepository
    from services.job_queue import JobQueue
    from services.report import ReportJob, ReportService
    from services.timeline import TimelineService

    return ReportService(
        ReportRepository(db_session),
        CaseRepository(db_session),
        rag_service,
        cast("JobQueue[ReportJob]", report_queue),
        timeline=TimelineService(TimelineRepository(db_session), CaseRepository(db_session)),
    )


@pytest.fixture
def make_report(db_session: Session):  # type: ignore[no-untyped-def]
    """Create a report row directly, bypassing generation.

    For tests about *reading*, *exporting*, *deleting*, and *ownership*, which
    should not have to spend a full multi-section pipeline run each to arrange a
    fixture. Defaults to a completed, grounded, single-section report because
    that is the state most of those tests need.
    """
    from models.report import Report, ReportStatus, ReportType

    def _make(  # type: ignore[no-untyped-def]
        *,
        requested_by,
        case,
        report_type: ReportType = ReportType.CASE_SUMMARY,
        status: ReportStatus = ReportStatus.COMPLETED,
        title: str = "Synthese de l'affaire",
        language: str = "fr",
        sections: list[dict[str, object]] | None = None,
        citations: list[dict[str, object]] | None = None,
        created_at: datetime | None = None,
        deleted_at: datetime | None = None,
    ) -> Report:
        content = (
            [
                {
                    "key": "overview",
                    "title": "Apercu",
                    "content": "Le litige porte sur un bail commercial [1].",
                    "grounded": True,
                    "citation_markers": [1],
                    "retrieved_count": 3,
                    "context_count": 3,
                    "duration_ms": 40,
                }
            ]
            if sections is None
            else sections
        )
        sources = (
            [
                {
                    "marker": 1,
                    "document_id": str(uuid.uuid4()),
                    "document_name": "Contrat.pdf",
                    "document_version": 1,
                    "page_number": 7,
                    "case_id": str(case.id),
                    "score": 0.82,
                    "excerpt": "Le loyer est payable le premier jour du mois.",
                    "excerpt_truncated": False,
                    "referenced": True,
                }
            ]
            if citations is None
            else citations
        )

        report = Report(
            id=uuid.uuid4(),
            case_id=case.id,
            report_type=report_type,
            title=title,
            language=language,
            status=status,
            template_version=1,
            sections_total=len(content),
            sections_completed=len(content) if status is ReportStatus.COMPLETED else 0,
            sections=content if status is ReportStatus.COMPLETED else [],
            citations=sources if status is ReportStatus.COMPLETED else [],
            grounded_sections=(
                sum(1 for section in content if section.get("grounded"))
                if status is ReportStatus.COMPLETED
                else None
            ),
            character_count=(
                sum(len(str(section.get("content", ""))) for section in content)
                if status is ReportStatus.COMPLETED
                else None
            ),
            attempt_count=1,
            requested_by=requested_by.id,
            deleted_at=deleted_at,
        )
        if created_at is not None:
            # Set explicitly so ordering tests do not depend on wall-clock gaps
            # between rows inserted in the same millisecond.
            report.created_at = created_at
        db_session.add(report)
        db_session.commit()
        db_session.refresh(report)
        return report

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
def session_factory(db_engine):  # type: ignore[no-untyped-def]
    """A session factory backed by the test's engine.

    The WebSocket layer cannot hold a request-scoped session — a connection
    outlives any request — so it opens one per question through a factory. This
    is the test's half of that contract, and it builds a **real session per
    call** rather than handing out the shared one: authorization runs in a worker
    thread, and a ``Session`` is not thread-safe, so sharing would be a race
    dressed up as a fixture. The engine is the same, so both see the same rows.
    """
    factory = sessionmaker(
        bind=db_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    return factory


@pytest.fixture
def connection_manager(session_factory):  # type: ignore[no-untyped-def]
    """A connection manager whose authorization runs against the test database.

    Its own instance rather than the process-wide one, for the reason the metrics
    recorders get their own: the shared manager is registered on the dispatcher
    at startup and holds whatever connections previous tests left, so assertions
    about *this* test's subscriptions would count somebody else's.
    """
    from services.event_metrics import InMemoryRealtimeMetrics
    from services.realtime_access import RealtimeAccessPolicy
    from websocket.manager import ConnectionManager

    return ConnectionManager(
        access=RealtimeAccessPolicy(session_factory),
        metrics=InMemoryRealtimeMetrics(),
    )


@pytest.fixture
def event_publisher():  # type: ignore[no-untyped-def]
    """A recording event publisher, replacing the process-wide dispatcher.

    Overridden per test so that "did archiving a case announce it?" is an
    assertion about a list, and so that one test's events cannot be counted by
    another's — the same reason the search, RAG, and assistant metrics recorders
    are overridden rather than shared.
    """
    from services.events import RecordingEventPublisher

    return RecordingEventPublisher()


# --------------------------------------------------------------------------- #
# Notification fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def notification_metrics():  # type: ignore[no-untyped-def]
    """A fresh notification metrics recorder per test.

    Per test rather than the process-wide one, for the reason every other
    ``*_metrics`` fixture here is: the real recorder counts for the life of the
    process, so "one notification was created" would otherwise depend on how many
    every earlier test produced.
    """
    from services.notification_metrics import InMemoryNotificationMetrics

    return InMemoryNotificationMetrics()


# --------------------------------------------------------------------------- #
# Email delivery fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def email_provider():  # type: ignore[no-untyped-def]
    """A provider that accepts every message and records it, sending nothing.

    The real :class:`~services.email_provider.NullEmailProvider` rather than a
    bespoke double, because it is a **shipped** backend with the same contract as
    the SMTP one — so a test that passes against it is exercising the same error
    handling and the same result shape production uses, and its ``sent`` list is
    what an assertion about "was this actually composed and addressed?" reads.
    """
    from services.email_provider import NullEmailProvider

    return NullEmailProvider()


@pytest.fixture
def email_metrics():  # type: ignore[no-untyped-def]
    """A fresh email metrics recorder per test.

    Per test rather than the process-wide one, for the reason every other
    ``*_metrics`` fixture here is: the real recorder counts for the life of the
    process, so "one email was sent" would otherwise depend on how many every
    earlier test produced.
    """
    from services.email_metrics import InMemoryEmailMetrics

    return InMemoryEmailMetrics()


@pytest.fixture
def email_queue(db_session: Session, email_provider, email_metrics):  # type: ignore[no-untyped-def]
    """An inline email queue, so a queued delivery is sent before the assertion.

    In production this is a bounded thread pool and the delivery is sent moments
    later; here the assertion is about the *outcome*, and a test that waits for a
    thread is a test that will eventually be flaky — the same substitution the
    OCR, indexing, and report queues get.

    The service the runner builds is given **no queue of its own**, exactly as the
    real worker's is: a send does not schedule more sends, and a queue here would
    make a bug an unbounded loop.
    """
    from services.email_delivery import build_delivery_service
    from services.email_templates import get_email_template_renderer
    from services.job_queue import InlineJobQueue

    def _run(job) -> None:  # type: ignore[no-untyped-def]
        build_delivery_service(
            db_session,
            provider=email_provider,
            templates=get_email_template_renderer(),
            queue=None,
            metrics=email_metrics,
        ).process(job)

    return InlineJobQueue(_run, name="email")


@pytest.fixture
def email_delivery_service(db_session: Session, email_provider, email_metrics, email_queue):  # type: ignore[no-untyped-def]
    """The Email Delivery Service on the test database.

    The real templates are deliberately *not* substituted: they are files under
    source control with no external dependency, so the message composed here is
    the message production would compose.
    """
    from services.email_delivery import build_delivery_service
    from services.email_templates import get_email_template_renderer

    return build_delivery_service(
        db_session,
        provider=email_provider,
        templates=get_email_template_renderer(),
        queue=email_queue,
        metrics=email_metrics,
    )


# --------------------------------------------------------------------------- #
# WhatsApp delivery fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def whatsapp_provider():  # type: ignore[no-untyped-def]
    """A provider that accepts every message and records it, sending nothing.

    The real :class:`~services.whatsapp_provider.NullWhatsAppProvider` rather than
    a bespoke double, for the reason ``email_provider`` uses the real null email
    backend — and one more that is specific to this channel: a test suite cannot
    have a WhatsApp Business account, an approved template, or a real phone number
    to send to, so substituting here is not a convenience but the only way this
    feature is testable at all.
    """
    from services.whatsapp_provider import NullWhatsAppProvider

    return NullWhatsAppProvider()


@pytest.fixture
def whatsapp_metrics():  # type: ignore[no-untyped-def]
    """A fresh WhatsApp metrics recorder per test.

    Per test rather than the process-wide one, for the reason every other
    ``*_metrics`` fixture here is: the real recorder counts for the life of the
    process.
    """
    from services.whatsapp_metrics import InMemoryWhatsAppMetrics

    return InMemoryWhatsAppMetrics()


@pytest.fixture
def whatsapp_queue(db_session: Session, whatsapp_provider, whatsapp_metrics):  # type: ignore[no-untyped-def]
    """An inline WhatsApp queue, so a queued delivery is sent before the assertion.

    In production this is a bounded thread pool and the message goes out moments
    later; here the assertion is about the *outcome*, and a test that waits for a
    thread is a test that will eventually be flaky.

    The service the runner builds is given **no queue of its own**, exactly as the
    real worker's is: a send does not schedule more sends.
    """
    from services.job_queue import InlineJobQueue
    from services.whatsapp_delivery import build_whatsapp_delivery_service
    from services.whatsapp_templates import get_whatsapp_template_renderer

    def _run(job) -> None:  # type: ignore[no-untyped-def]
        build_whatsapp_delivery_service(
            db_session,
            provider=whatsapp_provider,
            templates=get_whatsapp_template_renderer(),
            queue=None,
            metrics=whatsapp_metrics,
        ).process(job)

    return InlineJobQueue(_run, name="whatsapp")


@pytest.fixture
def whatsapp_delivery_service(  # type: ignore[no-untyped-def]
    db_session: Session, whatsapp_provider, whatsapp_metrics, whatsapp_queue
):
    """The WhatsApp Delivery Service on the test database.

    The real descriptors are deliberately *not* substituted: they are files under
    source control with no external dependency, and their parameter count and
    order are the contract with the approved template — so the message composed
    here is the message production would compose.
    """
    from services.whatsapp_delivery import build_whatsapp_delivery_service
    from services.whatsapp_templates import get_whatsapp_template_renderer

    return build_whatsapp_delivery_service(
        db_session,
        provider=whatsapp_provider,
        templates=get_whatsapp_template_renderer(),
        queue=whatsapp_queue,
        metrics=whatsapp_metrics,
    )


@pytest.fixture
def notification_channels(email_delivery_service, whatsapp_delivery_service):  # type: ignore[no-untyped-def]
    """The delivery channels a created notification batch is offered to.

    A factory taking a session, matching what
    :class:`~services.notification_events.NotificationEventSubscriber` expects —
    and ignoring it, because every fixture here already shares the one test
    session. That is the only difference from production, and it is what lets an
    assertion read the delivery rows the subscriber just wrote.

    **Both channels, always**, rather than one per test file: each is gated by its
    own feature switch inside its own ``dispatch``, so a test that has not turned a
    channel on gets nothing from it — which is both the production behaviour and
    the arrangement that catches a channel leaking into a test that is not about
    it.
    """

    def _channels(_session):  # type: ignore[no-untyped-def]
        return (email_delivery_service, whatsapp_delivery_service)

    return _channels


@pytest.fixture
def notification_subscriber(  # type: ignore[no-untyped-def]
    session_factory, event_publisher, notification_metrics, notification_channels
):
    """A notification subscriber wired to the test database, **not started**.

    Deliberately not started, and that is what makes these tests deterministic:
    :meth:`~services.notification_events.NotificationEventSubscriber.process` is
    public and synchronous precisely so a test can hand it an event and assert
    about rows, rather than queueing one and waiting to see whether a thread got
    there first. A test that waits for a worker is a test that will eventually be
    flaky.

    Its recipient resolver runs against the same engine, so the authorization
    chain exercised here is the real one — the real case policy, the real
    repositories — rather than a mock of it.
    """
    from services.notification_events import NotificationEventSubscriber

    return NotificationEventSubscriber(
        session_factory,
        publisher=event_publisher,
        metrics=notification_metrics,
        channels=notification_channels,
    )


@pytest.fixture
def make_notification(db_session: Session):  # type: ignore[no-untyped-def]
    """Factory creating persisted notifications directly.

    For tests about *reading* — the feed, the filters, the badge, read state —
    which have no business going through an event to arrange their fixtures. The
    tests about *creation* use ``notification_subscriber`` and a real event.

    ``created_at`` is settable so ordering tests do not depend on wall-clock gaps
    between rows inserted in the same millisecond, exactly as ``make_case`` and
    ``make_user`` allow.
    """
    import itertools as _itertools

    from core.notifications import NotificationCategory, dedupe_key
    from models.notification import Notification, NotificationPriority, NotificationType

    counter = _itertools.count(1)

    def _make(
        *,
        recipient_id: uuid.UUID,
        rule_key: str = "case.created",
        category: NotificationCategory | str = NotificationCategory.CASE,
        notification_type: NotificationType = NotificationType.INFORMATION,
        priority: NotificationPriority = NotificationPriority.NORMAL,
        context: dict[str, object] | None = None,
        case_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        target_type: str | None = None,
        target_id: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
        event_type: str | None = None,
        read_at: datetime | None = None,
        archived_at: datetime | None = None,
        created_at: datetime | None = None,
        dedupe: str | None = None,
    ):  # type: ignore[no-untyped-def]
        resolved_category = (
            category.value if isinstance(category, NotificationCategory) else category
        )
        notification = Notification(
            id=uuid.uuid4(),
            recipient_id=recipient_id,
            event_id=event_id,
            event_type=event_type,
            rule_key=rule_key,
            category=resolved_category,
            notification_type=notification_type,
            priority=priority,
            context=context or {"case_number": f"CASE-2026-{next(counter):04d}"},
            case_id=case_id,
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            # Unique per row unless a test is specifically about deduplication,
            # so an ordinary fixture cannot accidentally suppress its own second
            # notification through the windowed duplicate check.
            dedupe_key=dedupe or dedupe_key(rule_key=rule_key, discriminator=str(uuid.uuid4())),
            read_at=read_at,
            archived_at=archived_at,
        )
        if created_at is not None:
            notification.created_at = created_at
        db_session.add(notification)
        db_session.commit()
        return notification

    return _make


# --------------------------------------------------------------------------- #
# Dashboard fixtures
#
# Two, and the shortness of the list is worth noting: the dashboard has no
# provider, no queue, no worker, and no external service, because it reads the
# database and returns. Everything else it uses — the repository, the access
# policy, the widget catalog — is the application's own and is deliberately not
# doubled, so a test exercises the real authorization and the real queries.
# --------------------------------------------------------------------------- #


@pytest.fixture
def dashboard_metrics():  # type: ignore[no-untyped-def]
    """A fresh dashboard metrics recorder per test.

    Per test rather than the process-wide one, for the reason every other
    ``*_metrics`` fixture here is: the real recorder counts for the life of the
    process, so "one dashboard was loaded" would otherwise depend on how many
    every earlier test loaded.
    """
    from services.dashboard_metrics import InMemoryDashboardMetrics

    return InMemoryDashboardMetrics()


@pytest.fixture(autouse=True)
def dashboard_cache():  # type: ignore[no-untyped-def]
    """Empty the platform-wide widget cache around every test.

    Autouse and on both sides, because the cache is module-level and keyed by
    widget and window rather than by caller: without this, a test that stores a
    figure would hand it to the next test's database, which is a *different*
    in-memory database. It is the one piece of state this feature keeps between
    requests, and it is why the feature has a fixture at all.
    """
    from services.dashboard import clear_dashboard_cache

    clear_dashboard_cache()
    yield
    clear_dashboard_cache()


# --------------------------------------------------------------------------- #
# Settings fixtures
# --------------------------------------------------------------------------- #


class InMemorySessionRegistry:
    """Test double for :class:`~services.session_registry.SessionRegistry`.

    Mirrors the real contract — record, list, remove, clear-except-one — without
    Redis. The real registry *fails soft*, so a suite without Redis would still
    pass with it in place; it would pass by reporting **no sessions**, which is
    exactly what the active-sessions tests need to assert about. A double that
    actually remembers is the only way to tell "the list works" from "the list is
    empty because nothing recorded".

    Expiry is honoured on read, matching the real store, so a test can build a
    session that has already lapsed and assert it is not listed.
    """

    def __init__(self) -> None:
        self._sessions: dict[uuid.UUID, dict[str, object]] = {}

    def record(
        self,
        user_id: uuid.UUID,
        session_id: str,
        *,
        expires_at: datetime,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        from services.session_registry import SessionRecord

        now = datetime.now(UTC)
        existing = self._sessions.setdefault(user_id, {}).get(session_id)
        created_at = existing.created_at if isinstance(existing, SessionRecord) else now

        self._sessions[user_id][session_id] = SessionRecord(
            session_id=session_id,
            created_at=created_at,
            last_seen_at=now,
            expires_at=expires_at,
            ip_address=ip_address
            or (existing.ip_address if isinstance(existing, SessionRecord) else None),
            user_agent=user_agent
            or (existing.user_agent if isinstance(existing, SessionRecord) else None),
        )

    def list_sessions(self, user_id: uuid.UUID):  # type: ignore[no-untyped-def]
        from services.session_registry import SessionRecord

        now = datetime.now(UTC)
        records = [
            record
            for record in self._sessions.get(user_id, {}).values()
            if isinstance(record, SessionRecord) and record.expires_at > now
        ]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def remove(self, user_id: uuid.UUID, session_id: str) -> None:
        self._sessions.get(user_id, {}).pop(session_id, None)

    def clear(self, user_id: uuid.UUID, *, keep: str | None = None) -> None:
        stored = self._sessions.get(user_id, {})
        self._sessions[user_id] = (
            {keep: stored[keep]} if keep is not None and keep in stored else {}
        )


@pytest.fixture
def session_registry() -> InMemorySessionRegistry:
    """A fresh in-memory record of live sign-ins per test."""
    return InMemorySessionRegistry()


@pytest.fixture
def settings_metrics():  # type: ignore[no-untyped-def]
    """A fresh settings metrics recorder per test.

    Per test rather than the process-wide one, for the reason every other
    ``*_metrics`` fixture here is: the real recorder counts for the life of the
    process, so "one setting was changed" would otherwise depend on how many every
    earlier test changed.
    """
    from services.settings_metrics import InMemorySettingsMetrics

    return InMemorySettingsMetrics()


@pytest.fixture
def localization_metrics():  # type: ignore[no-untyped-def]
    """A fresh localization metrics recorder per test.

    Per test rather than the process-wide one, for the reason every other
    ``*_metrics`` fixture here is — and more strongly than most: two of the four
    figures it holds are *reported by clients*, so a shared recorder would make
    "one missing key was reported" depend on how many every earlier test reported.
    """
    from services.localization_metrics import InMemoryLocalizationMetrics

    return InMemoryLocalizationMetrics()


@pytest.fixture
def api_client(
    db_session: Session,
    revocations: InMemoryRevocationStore,
    throttle: InMemoryLoginThrottle,
    document_storage: InMemoryDocumentStorage,
    ocr_engine: FakeOcrEngine,
    ocr_queue: RecordingOcrQueue,
    embedder: FakeEmbedder,
    vector_store: InMemoryVectorStore,
    index_queue: RecordingIndexQueue,
    vector_searcher: InMemoryVectorSearcher,
    search_metrics,  # type: ignore[no-untyped-def]
    llm_provider: ScriptedLLMProvider,
    rag_metrics,  # type: ignore[no-untyped-def]
    follow_up_suggester: ScriptedFollowUpSuggester,
    assistant_metrics,  # type: ignore[no-untyped-def]
    report_queue: RecordingIndexQueue,
    event_publisher,  # type: ignore[no-untyped-def]
    session_factory,  # type: ignore[no-untyped-def]
    connection_manager,  # type: ignore[no-untyped-def]
    notification_metrics,  # type: ignore[no-untyped-def]
    notification_subscriber,  # type: ignore[no-untyped-def]
    email_provider,  # type: ignore[no-untyped-def]
    email_metrics,  # type: ignore[no-untyped-def]
    email_queue,  # type: ignore[no-untyped-def]
    whatsapp_provider,  # type: ignore[no-untyped-def]
    whatsapp_metrics,  # type: ignore[no-untyped-def]
    whatsapp_queue,  # type: ignore[no-untyped-def]
    dashboard_metrics,  # type: ignore[no-untyped-def]
    dashboard_cache,  # type: ignore[no-untyped-def]
    session_registry: InMemorySessionRegistry,
    settings_metrics,  # type: ignore[no-untyped-def]
    localization_metrics,  # type: ignore[no-untyped-def]
) -> Iterator[TestClient]:
    """A TestClient whose external collaborators are all doubles.

    The database, the token denylist, the login throttle, the object store, the
    OCR engine, the OCR queue, the embedding model, the vector store, the
    indexing queue, the vector searcher, the search metrics recorder, the
    language-model provider, and the RAG metrics recorder. Everything else —
    routers, services, repositories, chunker, ranker, **prompt templates**,
    access policies — is the application's own.

    The prompt library is deliberately *not* overridden: the templates are files
    under source control with no external dependency, so the pipeline exercised
    here builds the same prompt production would.
    """
    from api.deps import (
        get_assistant_metrics_recorder,
        get_dashboard_metrics_recorder,
        get_document_storage,
        get_email_job_queue,
        get_email_metrics_recorder,
        get_email_provider_dependency,
        get_embedder_dependency,
        get_event_publisher,
        get_follow_up_suggester_dependency,
        get_index_job_queue,
        get_llm_provider_dependency,
        get_localization_metrics_recorder,
        get_login_throttle,
        get_notification_metrics_recorder,
        get_notification_subscriber_dependency,
        get_ocr_engine_dependency,
        get_ocr_job_queue,
        get_rag_metrics_recorder,
        get_report_job_queue,
        get_search_metrics_recorder,
        get_session_factory,
        get_session_registry,
        get_settings_metrics_recorder,
        get_token_revocation_store,
        get_vector_searcher_dependency,
        get_vector_store_dependency,
        get_whatsapp_job_queue,
        get_whatsapp_metrics_recorder,
        get_whatsapp_provider_dependency,
    )
    from api.v1.websocket.router import get_manager
    from db.session import get_db
    from main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_token_revocation_store] = lambda: revocations
    app.dependency_overrides[get_login_throttle] = lambda: throttle
    app.dependency_overrides[get_document_storage] = lambda: document_storage
    app.dependency_overrides[get_ocr_engine_dependency] = lambda: ocr_engine
    app.dependency_overrides[get_ocr_job_queue] = lambda: ocr_queue
    app.dependency_overrides[get_embedder_dependency] = lambda: embedder
    app.dependency_overrides[get_vector_store_dependency] = lambda: vector_store
    app.dependency_overrides[get_index_job_queue] = lambda: index_queue
    app.dependency_overrides[get_vector_searcher_dependency] = lambda: vector_searcher
    app.dependency_overrides[get_search_metrics_recorder] = lambda: search_metrics
    app.dependency_overrides[get_llm_provider_dependency] = lambda: llm_provider
    app.dependency_overrides[get_rag_metrics_recorder] = lambda: rag_metrics
    app.dependency_overrides[get_follow_up_suggester_dependency] = lambda: follow_up_suggester
    app.dependency_overrides[get_assistant_metrics_recorder] = lambda: assistant_metrics
    # Inline, so a report requested over HTTP is generated before the response is
    # read. In production it is a thread pool and the client polls; here the
    # assertion is about the *outcome*, and a test that waits for a worker is a
    # test that will eventually be flaky.
    app.dependency_overrides[get_report_job_queue] = lambda: report_queue
    # The dispatcher is process-wide and the WebSocket manager subscribes to it at
    # startup, so leaving it in place would make one test's uploads visible to
    # another's assertions about throughput. The recorder keeps what was
    # published without delivering it anywhere.
    app.dependency_overrides[get_event_publisher] = lambda: event_publisher
    # The WebSocket endpoint resolves its own sessions (a connection is not a
    # request), so overriding `get_db` alone would leave it reaching for the real
    # PostgreSQL. These two are what make the socket testable at all.
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_manager] = lambda: connection_manager
    # The notification recorder and subscriber are process-wide for the same
    # reason the dispatcher is, and are overridden for the same reason: the real
    # subscriber is registered at startup with worker threads and whatever
    # backlog earlier tests left, so an assertion about *this* test's counters
    # would be counting somebody else's. The substituted subscriber is never
    # started — tests drive `process()` directly.
    app.dependency_overrides[get_notification_metrics_recorder] = lambda: notification_metrics
    app.dependency_overrides[get_notification_subscriber_dependency] = (
        lambda: notification_subscriber
    )
    # The mail provider, its queue, and its recorder, for the same three reasons:
    # the real provider would open a connection to a relay nobody configured, the
    # real queue is a thread pool the assertion would race, and the real recorder
    # counts for the life of the process. The **templates are deliberately not
    # overridden** — they are files under source control with no external
    # dependency, so the message composed here is the message production composes.
    app.dependency_overrides[get_email_provider_dependency] = lambda: email_provider
    app.dependency_overrides[get_email_job_queue] = lambda: email_queue
    app.dependency_overrides[get_email_metrics_recorder] = lambda: email_metrics
    # The WhatsApp provider, its queue, and its recorder, for the same three
    # reasons one channel over — and for one more that is specific to this one: a
    # test suite cannot have a WhatsApp Business account, an approved template, or
    # a real number to send to, so the discarding provider is not a convenience
    # here but the only way the channel is exercisable at all. The **descriptors
    # are deliberately not overridden**: their parameter count and order are the
    # contract with the approved template, so the message composed here is the
    # message production composes.
    app.dependency_overrides[get_whatsapp_provider_dependency] = lambda: whatsapp_provider
    app.dependency_overrides[get_whatsapp_job_queue] = lambda: whatsapp_queue
    app.dependency_overrides[get_whatsapp_metrics_recorder] = lambda: whatsapp_metrics
    # The dashboard recorder, for the reason every other recorder here is
    # overridden. Its collaborators are otherwise **all the application's own** —
    # there is no dashboard queue, no provider, and no external service to double,
    # because the dashboard reads the database and nothing else. The one thing
    # that does need clearing between tests is the platform-wide widget cache,
    # which the `dashboard_cache` fixture handles.
    app.dependency_overrides[get_dashboard_metrics_recorder] = lambda: dashboard_metrics
    # The session registry, because the real one is Redis-backed and — unlike the
    # denylist — **fails soft**: without this override the active-sessions tests
    # would pass by reporting an empty list, which is indistinguishable from the
    # feature being broken. The settings recorder, for the reason every other
    # recorder here is overridden. Everything else the Settings module needs is
    # the application's own: there is no queue, no provider, and no external
    # service, because it reads and writes the database and delegates the rest.
    app.dependency_overrides[get_session_registry] = lambda: session_registry
    app.dependency_overrides[get_settings_metrics_recorder] = lambda: settings_metrics
    # Localization overrides **only** the metrics recorder. The language
    # directory underneath it is the application's own, reading the same
    # `user_settings` rows the Settings API writes — which is the whole point of
    # these tests: a preference saved through one endpoint has to reach an email,
    # a WhatsApp message, and a notification feed without anything being
    # substituted in between.
    app.dependency_overrides[get_localization_metrics_recorder] = (
        lambda: localization_metrics
    )
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
