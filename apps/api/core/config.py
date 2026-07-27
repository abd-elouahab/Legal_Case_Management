"""Application configuration.

Settings are loaded from environment variables (and an optional ``.env`` file)
using ``pydantic-settings``. Validation runs when :data:`settings` is
instantiated at import time, so an invalid configuration fails fast during
startup rather than at first use.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, ValidationInfo, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repo root (apps/api/core/config.py -> parents[3]). The .env lives here, so it
# resolves the same way regardless of the process working directory (the API
# runs from apps/api, tests from the repo root, Alembic from apps/api).
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Development-only JWT secret. Never used in production: the model validator
# below rejects it, so a real secret must be supplied via JWT_SECRET_KEY.
DEV_JWT_SECRET_PLACEHOLDER = "dev-only-insecure-jwt-secret-change-me"

# Minimum acceptable length for the production signing secret (bytes of entropy
# for HMAC-SHA256; 32+ chars is the widely recommended floor).
MIN_JWT_SECRET_LENGTH = 32


class Environment(StrEnum):
    """Supported runtime environments."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"
    TESTING = "testing"


class Settings(BaseSettings):
    """Strongly-typed application settings sourced from the environment."""

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Application ---
    ENVIRONMENT: Environment = Environment.DEVELOPMENT
    PROJECT_NAME: str = "Legal Case Management Platform API"
    VERSION: str = "0.1.0"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = False

    # --- Server ---
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # --- Logging ---
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_JSON: bool = True

    # --- API documentation ---
    ENABLE_DOCS: bool = True

    # --- CORS / trusted hosts ---
    # NoDecode: skip pydantic-settings' JSON decoding so the values can be given
    # as a plain comma-separated string in .env (parsed by the validator below).
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    ALLOWED_HOSTS: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])

    # --- PostgreSQL ---
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "legal_platform"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_CONNECT_TIMEOUT: int = 3
    DB_ECHO: bool = False

    # --- Redis ---
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_MAX_CONNECTIONS: int = 10
    REDIS_SOCKET_TIMEOUT: int = 3

    # --- MinIO ---
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_SECURE: bool = False
    MINIO_REGION: str | None = None
    MINIO_CONNECT_TIMEOUT: int = 3

    # --- Qdrant ---
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_API_KEY: str | None = None
    QDRANT_HTTPS: bool = False
    QDRANT_TIMEOUT: int = 3

    # --- Authentication (JWT) ---
    # The signing secret MUST come from the environment. The default below is a
    # clearly-marked development placeholder and is rejected in production.
    JWT_SECRET_KEY: str = DEV_JWT_SECRET_PLACEHOLDER
    JWT_ALGORITHM: Literal["HS256", "HS384", "HS512"] = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # Audience/issuer claims are validated on every token to reject tokens that
    # were minted for a different service.
    JWT_ISSUER: str = "legal-platform-api"
    JWT_AUDIENCE: str = "legal-platform-web"

    # --- Password hashing (bcrypt) ---
    BCRYPT_ROUNDS: int = 12

    # --- Login throttling (brute-force protection) ---
    # After MAX_FAILED_LOGIN_ATTEMPTS consecutive failures inside
    # LOGIN_FAILURE_WINDOW_MINUTES, further attempts are refused with HTTP 429
    # for LOGIN_LOCKOUT_MINUTES. Counted per account and per client IP.
    MAX_FAILED_LOGIN_ATTEMPTS: int = 5
    LOGIN_FAILURE_WINDOW_MINUTES: int = 15
    LOGIN_LOCKOUT_MINUTES: int = 15
    # Whether to derive the client IP from X-Forwarded-For. Enable ONLY when the
    # app sits behind a trusted reverse proxy (e.g. Nginx) that overwrites the
    # header — otherwise clients can spoof it to evade per-IP throttling or to
    # get another user's IP blocked.
    TRUST_PROXY_HEADERS: bool = False

    # --- Refresh token cookie ---
    # The refresh token is delivered as an httpOnly cookie so browser JavaScript
    # cannot read it (XSS-resistant), with SameSite protection against CSRF.
    REFRESH_COOKIE_NAME: str = "legal_platform_refresh"
    REFRESH_COOKIE_SECURE: bool = False  # MUST be true in production (HTTPS)
    REFRESH_COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "strict"
    REFRESH_COOKIE_DOMAIN: str | None = None

    @field_validator("CORS_ORIGINS", "ALLOWED_HOSTS", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        """Parse a comma-separated string into a list (a real list passes through)."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("REDIS_PASSWORD", "MINIO_REGION", "QDRANT_API_KEY", "REFRESH_COOKIE_DOMAIN", mode="before")
    @classmethod
    def _empty_str_to_none(cls, value: object) -> object:
        """Treat a blank value in .env (e.g. ``REDIS_PASSWORD=``) as unset."""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("POSTGRES_PASSWORD")
    @classmethod
    def _require_strong_db_password_in_production(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("ENVIRONMENT") is Environment.PRODUCTION and value in {"", "postgres"}:
            raise ValueError("POSTGRES_PASSWORD must be set to a non-default value in production")
        return value

    @field_validator(
        "ACCESS_TOKEN_EXPIRE_MINUTES",
        "REFRESH_TOKEN_EXPIRE_DAYS",
        "BCRYPT_ROUNDS",
        "MAX_FAILED_LOGIN_ATTEMPTS",
        "LOGIN_FAILURE_WINDOW_MINUTES",
        "LOGIN_LOCKOUT_MINUTES",
    )
    @classmethod
    def _require_positive(cls, value: int, info: ValidationInfo) -> int:
        if value <= 0:
            raise ValueError(f"{info.field_name} must be a positive integer")
        return value

    @model_validator(mode="after")
    def _validate_production_invariants(self) -> Settings:
        """Enforce safe defaults for production deployments (fail fast)."""
        if self.ENVIRONMENT is Environment.PRODUCTION:
            if self.DEBUG:
                raise ValueError("DEBUG must be disabled in production")
            if "*" in self.ALLOWED_HOSTS:
                raise ValueError("ALLOWED_HOSTS must not use the '*' wildcard in production")
            if self.MINIO_SECRET_KEY == "minioadmin":
                raise ValueError("MINIO_SECRET_KEY must be set to a non-default value in production")
            if self.JWT_SECRET_KEY == DEV_JWT_SECRET_PLACEHOLDER:
                raise ValueError("JWT_SECRET_KEY must be set to a real secret in production")
            if len(self.JWT_SECRET_KEY) < MIN_JWT_SECRET_LENGTH:
                raise ValueError(
                    f"JWT_SECRET_KEY must be at least {MIN_JWT_SECRET_LENGTH} characters in production"
                )
            if not self.REFRESH_COOKIE_SECURE:
                raise ValueError("REFRESH_COOKIE_SECURE must be true in production (HTTPS only)")
            if self.REFRESH_COOKIE_SAMESITE == "none" and not self.REFRESH_COOKIE_SECURE:
                raise ValueError("REFRESH_COOKIE_SAMESITE='none' requires REFRESH_COOKIE_SECURE=true")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT is Environment.PRODUCTION

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_testing(self) -> bool:
        return self.ENVIRONMENT is Environment.TESTING

    @computed_field  # type: ignore[prop-decorator]
    @property
    def DATABASE_URL(self) -> str:
        """SQLAlchemy connection string for PostgreSQL (psycopg 3 driver)."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+psycopg",
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD,
                host=self.POSTGRES_HOST,
                port=self.POSTGRES_PORT,
                path=self.POSTGRES_DB,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def REDIS_URL(self) -> str:
        """Connection string for Redis."""
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def access_token_ttl(self) -> timedelta:
        """Lifetime of an access token."""
        return timedelta(minutes=self.ACCESS_TOKEN_EXPIRE_MINUTES)

    @property
    def refresh_token_ttl(self) -> timedelta:
        """Lifetime of a refresh token."""
        return timedelta(days=self.REFRESH_TOKEN_EXPIRE_DAYS)

    @property
    def login_failure_window(self) -> timedelta:
        """How long consecutive failed login attempts are remembered."""
        return timedelta(minutes=self.LOGIN_FAILURE_WINDOW_MINUTES)

    @property
    def login_lockout_duration(self) -> timedelta:
        """How long login is refused once the failure threshold is reached."""
        return timedelta(minutes=self.LOGIN_LOCKOUT_MINUTES)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()


settings = get_settings()
