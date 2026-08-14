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


# --------------------------------------------------------------------------- #
# Email delivery
# --------------------------------------------------------------------------- #


def test_the_email_channel_is_off_by_default() -> None:
    """One of the two feature switches on this platform that default to off.

    Every other one — OCR, indexing, search, RAG, reports, real-time,
    notifications — defaults to on, because the worst case of an unconfigured one
    is a recorded failure nobody outside the platform sees. The delivery channels
    are different in kind: they are the platform's *outward-facing* side effects,
    and a deployment that has not yet chosen a relay, a from-address, and a base
    URL should not be mailing real people the first time somebody is assigned a
    case.
    """
    assert Settings(**_base_kwargs()).EMAIL_ENABLED is False


def test_the_supported_email_languages_match_the_platforms() -> None:
    """`core.config` cannot import `core.rag` — it is imported *by* everything,
    including that module — so the language set is duplicated as a literal. This
    is what keeps the copy from drifting: a language added to the platform and not
    here would make `EMAIL_DEFAULT_LANGUAGE` reject a value the renderer supports.
    """
    from core.config import SUPPORTED_EMAIL_LANGUAGES
    from core.rag import SUPPORTED_ANSWER_LANGUAGES

    assert frozenset(SUPPORTED_ANSWER_LANGUAGES) == SUPPORTED_EMAIL_LANGUAGES


def test_rejects_an_unsupported_email_language() -> None:
    with pytest.raises(ValidationError, match="EMAIL_DEFAULT_LANGUAGE"):
        Settings(**_base_kwargs(EMAIL_DEFAULT_LANGUAGE="de"))


def test_rejects_a_backoff_ceiling_below_the_base() -> None:
    """Not a ceiling but a silent replacement: every retry would wait the cap and
    the exponential schedule the operator configured would never happen."""
    with pytest.raises(ValidationError, match="EMAIL_RETRY_MAX_BACKOFF_SECONDS"):
        Settings(
            **_base_kwargs(
                EMAIL_RETRY_BACKOFF_SECONDS=120.0, EMAIL_RETRY_MAX_BACKOFF_SECONDS=60.0
            )
        )


def test_rejects_a_stale_threshold_at_or_below_the_send_timeout() -> None:
    """It would reclaim deliveries that are merely slow, so a relay taking nine
    seconds would have its message re-queued and eventually sent twice."""
    with pytest.raises(ValidationError, match="EMAIL_STALE_SENDING_SECONDS"):
        Settings(**_base_kwargs(SMTP_TIMEOUT_SECONDS=30, EMAIL_STALE_SENDING_SECONDS=30))


@pytest.mark.parametrize(
    "field",
    ["EMAIL_WORKER_CONCURRENCY", "EMAIL_MAX_ATTEMPTS", "EMAIL_RETRY_BATCH_SIZE"],
)
def test_rejects_non_positive_email_settings(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        Settings(**_base_kwargs(**{field: 0}))


def test_blank_smtp_credentials_become_none() -> None:
    """A blank value in .env means "unset", not an empty password."""
    settings = Settings(**_base_kwargs(SMTP_HOST="", SMTP_PASSWORD=""))
    assert settings.SMTP_HOST is None
    assert settings.SMTP_PASSWORD is None


def test_the_email_base_url_falls_back_to_the_first_cors_origin() -> None:
    """Which in every deployment configured so far is exactly the web
    application's own address, and is already something an operator had to get
    right for the frontend to work at all."""
    settings = Settings(
        **_base_kwargs(CORS_ORIGINS="https://legal.example,https://other.example")
    )
    assert settings.email_base_url == "https://legal.example"


def test_an_explicit_base_url_wins_and_loses_its_trailing_slash() -> None:
    settings = Settings(
        **_base_kwargs(
            EMAIL_BASE_URL="https://mail-links.example/", CORS_ORIGINS="https://other.example"
        )
    )
    assert settings.email_base_url == "https://mail-links.example"


def test_a_wildcard_origin_is_not_a_base_url() -> None:
    """`https://*` is not somewhere a link can point."""
    assert Settings(**_base_kwargs(CORS_ORIGINS="*")).email_base_url is None


# --------------------------------------------------------------------------- #
# WhatsApp delivery channel
# --------------------------------------------------------------------------- #


def test_the_whatsapp_channel_is_off_by_default() -> None:
    """The second switch that defaults to off, for the reason email's does and one
    more besides: it reaches a device in somebody's pocket, and it cannot work at
    all until message templates have been approved by Meta."""
    assert Settings(**_base_kwargs()).WHATSAPP_ENABLED is False


def test_rejects_an_unsupported_whatsapp_language() -> None:
    """Checked against the platform's own supported set rather than a list of its
    own, so the two cannot drift — and the language also selects *which approved
    localization* of the template is sent."""
    with pytest.raises(ValidationError, match="WHATSAPP_DEFAULT_LANGUAGE"):
        Settings(**_base_kwargs(WHATSAPP_DEFAULT_LANGUAGE="de"))


def test_rejects_a_whatsapp_backoff_ceiling_below_the_base() -> None:
    with pytest.raises(ValidationError, match="WHATSAPP_RETRY_MAX_BACKOFF_SECONDS"):
        Settings(
            **_base_kwargs(
                WHATSAPP_RETRY_BACKOFF_SECONDS=120.0,
                WHATSAPP_RETRY_MAX_BACKOFF_SECONDS=60.0,
            )
        )


def test_rejects_a_whatsapp_stale_threshold_at_or_below_the_send_timeout() -> None:
    """It would reclaim deliveries that are merely slow — and two phone alerts
    about one hearing leave a reader unable to tell which is current."""
    with pytest.raises(ValidationError, match="WHATSAPP_STALE_SENDING_SECONDS"):
        Settings(
            **_base_kwargs(
                WHATSAPP_TIMEOUT_SECONDS=30, WHATSAPP_STALE_SENDING_SECONDS=30
            )
        )


def test_rejects_a_non_numeric_default_country_code() -> None:
    """`normalize_phone` would strip it to nothing and silently stop every
    nationally-formatted number being messaged, which looks exactly like "those
    users have no phone" in the metrics."""
    with pytest.raises(ValidationError, match="WHATSAPP_DEFAULT_COUNTRY_CODE"):
        Settings(**_base_kwargs(WHATSAPP_DEFAULT_COUNTRY_CODE="+2 1 2 (Morocco)"))


def test_a_country_code_may_be_written_with_a_plus() -> None:
    """Because that is how a country code is written everywhere else."""
    assert Settings(**_base_kwargs(WHATSAPP_DEFAULT_COUNTRY_CODE="+212"))


@pytest.mark.parametrize(
    "field",
    [
        "WHATSAPP_WORKER_CONCURRENCY",
        "WHATSAPP_MAX_ATTEMPTS",
        "WHATSAPP_RETRY_BATCH_SIZE",
        "WHATSAPP_TIMEOUT_SECONDS",
    ],
)
def test_rejects_non_positive_whatsapp_settings(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        Settings(**_base_kwargs(**{field: 0}))


def test_blank_whatsapp_credentials_become_none() -> None:
    """A blank value in .env means "unset", not an empty token."""
    settings = Settings(
        **_base_kwargs(WHATSAPP_ACCESS_TOKEN="", WHATSAPP_PHONE_NUMBER_ID="")
    )
    assert settings.WHATSAPP_ACCESS_TOKEN is None
    assert settings.WHATSAPP_PHONE_NUMBER_ID is None


def test_the_whatsapp_base_url_chains_to_the_email_one() -> None:
    """So a deployment states the web application's address once rather than once
    per channel — and the dedicated setting exists for the deployment that really
    does need two."""
    shared = Settings(
        **_base_kwargs(
            EMAIL_BASE_URL="https://legal.example", WHATSAPP_BASE_URL=None
        )
    )
    assert shared.whatsapp_base_url == "https://legal.example"

    split = Settings(
        **_base_kwargs(
            EMAIL_BASE_URL="https://legal.example",
            WHATSAPP_BASE_URL="https://m.legal.example/",
        )
    )
    assert split.whatsapp_base_url == "https://m.legal.example"
