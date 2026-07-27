"""Unit tests for application configuration and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config import Environment, Settings


def _base_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "ENVIRONMENT": Environment.DEVELOPMENT,
        "POSTGRES_HOST": "db",
        "POSTGRES_PORT": 5432,
        "POSTGRES_USER": "user",
        "POSTGRES_PASSWORD": "secret",
        "POSTGRES_DB": "legal",
        "REDIS_HOST": "cache",
        "REDIS_PORT": 6379,
        "REDIS_DB": 0,
    }
    kwargs.update(overrides)
    return kwargs


def test_database_url_uses_psycopg_driver() -> None:
    settings = Settings(**_base_kwargs())
    assert settings.DATABASE_URL == "postgresql+psycopg://user:secret@db:5432/legal"


def test_redis_url_includes_password_when_set() -> None:
    settings = Settings(**_base_kwargs(REDIS_PASSWORD="pw"))
    assert settings.REDIS_URL == "redis://:pw@cache:6379/0"


def test_cors_origins_accepts_comma_separated_string() -> None:
    settings = Settings(**_base_kwargs(CORS_ORIGINS="http://a.com, http://b.com"))
    assert settings.CORS_ORIGINS == ["http://a.com", "http://b.com"]


def test_production_rejects_debug() -> None:
    with pytest.raises(ValidationError):
        Settings(**_base_kwargs(ENVIRONMENT=Environment.PRODUCTION, DEBUG=True, ALLOWED_HOSTS=["api.example.com"]))


def test_production_rejects_default_db_password() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **_base_kwargs(
                ENVIRONMENT=Environment.PRODUCTION,
                POSTGRES_PASSWORD="postgres",
                ALLOWED_HOSTS=["api.example.com"],
                MINIO_SECRET_KEY="strong-secret",
            )
        )


def test_production_rejects_wildcard_allowed_hosts() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **_base_kwargs(
                ENVIRONMENT=Environment.PRODUCTION,
                ALLOWED_HOSTS=["*"],
                MINIO_SECRET_KEY="strong-secret",
            )
        )


# --------------------------------------------------------------------------- #
# Authentication settings
# --------------------------------------------------------------------------- #

STRONG_JWT_SECRET = "a" * 64


def _production_kwargs(**overrides: object) -> dict[str, object]:
    """Base kwargs for a production config that is otherwise valid.

    ``DEBUG`` is pinned because the repo-root ``.env`` (loaded by Settings) sets
    it to true for local development, which would otherwise fail the production
    check before the setting under test is reached.
    """
    kwargs = _base_kwargs(
        ENVIRONMENT=Environment.PRODUCTION,
        DEBUG=False,
        ALLOWED_HOSTS=["api.example.com"],
        MINIO_SECRET_KEY="strong-secret",
        JWT_SECRET_KEY=STRONG_JWT_SECRET,
        REFRESH_COOKIE_SECURE=True,
    )
    kwargs.update(overrides)
    return kwargs


def test_token_lifetimes_match_the_specified_windows() -> None:
    settings = Settings(**_base_kwargs())

    assert settings.ACCESS_TOKEN_EXPIRE_MINUTES == 15
    assert settings.REFRESH_TOKEN_EXPIRE_DAYS == 7
    assert settings.access_token_ttl.total_seconds() == 15 * 60
    assert settings.refresh_token_ttl.total_seconds() == 7 * 24 * 60 * 60


def test_a_valid_production_config_is_accepted() -> None:
    settings = Settings(**_production_kwargs())

    assert settings.is_production
    assert settings.JWT_SECRET_KEY == STRONG_JWT_SECRET


def test_production_rejects_the_development_jwt_secret() -> None:
    from core.config import DEV_JWT_SECRET_PLACEHOLDER

    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        Settings(**_production_kwargs(JWT_SECRET_KEY=DEV_JWT_SECRET_PLACEHOLDER))


def test_production_rejects_a_short_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        Settings(**_production_kwargs(JWT_SECRET_KEY="too-short"))


def test_production_requires_a_secure_refresh_cookie() -> None:
    with pytest.raises(ValidationError, match="REFRESH_COOKIE_SECURE"):
        Settings(**_production_kwargs(REFRESH_COOKIE_SECURE=False))


def test_development_tolerates_the_placeholder_secret() -> None:
    # Local development must work straight from .env.example.
    settings = Settings(**_base_kwargs())

    assert settings.JWT_SECRET_KEY
    assert not settings.is_production


@pytest.mark.parametrize(
    "field",
    ["ACCESS_TOKEN_EXPIRE_MINUTES", "REFRESH_TOKEN_EXPIRE_DAYS", "BCRYPT_ROUNDS"],
)
def test_rejects_non_positive_token_and_hashing_settings(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        Settings(**_base_kwargs(**{field: 0}))


def test_rejects_an_unsupported_jwt_algorithm() -> None:
    with pytest.raises(ValidationError):
        Settings(**_base_kwargs(JWT_ALGORITHM="none"))


def test_blank_cookie_domain_becomes_none() -> None:
    # A blank value in .env means "host-only cookie", not an empty domain.
    assert Settings(**_base_kwargs(REFRESH_COOKIE_DOMAIN="")).REFRESH_COOKIE_DOMAIN is None
