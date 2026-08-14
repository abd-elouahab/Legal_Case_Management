"""FastAPI application entry point.

Builds and configures the application: middleware, exception handlers, router
registration, lifespan events, and API documentation. Contains no business
logic — only infrastructure wiring.

Run locally with: ``uvicorn main:app --reload`` (from ``apps/api``).
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from api.health import router as system_router
from api.v1.router import api_router as api_v1_router
from core.config import settings
from core.exceptions import register_exception_handlers
from core.lifespan import lifespan
from core.logging import configure_logging
from core.middleware import ObservabilityMiddleware


def create_app() -> FastAPI:
    """Application factory: assemble and return the configured FastAPI app."""
    configure_logging()

    docs_enabled = settings.ENABLE_DOCS
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        lifespan=lifespan,
        debug=settings.DEBUG,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    _register_middleware(app)
    register_exception_handlers(app)
    _register_routers(app)

    return app


def _register_middleware(app: FastAPI) -> None:
    """Register middleware. Added last runs outermost (first on each request)."""
    # Innermost: per-request correlation id, trace, log context, and metrics.
    #
    # Innermost deliberately, and it is the one ordering decision here that
    # matters: a latency measured outside CORS and host validation would include
    # the time spent rejecting a request that never reached the application, and
    # the route template — which every metric label depends on — is only resolved
    # by the router underneath this.
    app.add_middleware(ObservabilityMiddleware)

    # CORS for the web frontend.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Outermost: reject requests with an untrusted Host header.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)


def _register_routers(app: FastAPI) -> None:
    """Register system and versioned API routers."""
    app.include_router(system_router)
    app.include_router(api_v1_router, prefix=settings.API_V1_PREFIX)


app = create_app()
