"""PostgreSQL engine, session factory, and FastAPI dependency.

Uses a synchronous SQLAlchemy engine with connection pooling and pre-ping so
stale connections are recycled transparently. ``get_db`` provides a
request-scoped session for dependency injection.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import settings

engine: Engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    echo=settings.DB_ECHO,
    future=True,
    # Bound the initial TCP connect so readiness probes fail fast when the
    # database is unreachable rather than blocking on the OS default timeout.
    connect_args={"connect_timeout": settings.DB_CONNECT_TIMEOUT},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped database session and guarantee cleanup."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def check_database_connection() -> None:
    """Execute a lightweight query to verify database connectivity.

    Raises the underlying SQLAlchemy exception if the connection fails.
    """
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def dispose_engine() -> None:
    """Dispose of the connection pool (called on application shutdown)."""
    engine.dispose()
