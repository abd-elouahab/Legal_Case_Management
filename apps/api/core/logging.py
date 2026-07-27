"""Structured logging configuration using ``structlog``.

Emits JSON logs in production/testing and human-readable colored logs in
development. The standard library ``logging`` module is routed through
``structlog`` so that libraries (uvicorn, SQLAlchemy, etc.) share the same
output format. ``print()`` must never be used in application code.
"""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog

from core.config import settings

_configured = False


def configure_logging() -> None:
    """Configure ``structlog`` and the standard-library logging bridge.

    Safe to call multiple times; configuration is applied only once.
    """
    global _configured
    if _configured:
        return

    log_level = logging.getLevelNamesMapping()[settings.LOG_LEVEL]

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.LOG_JSON:
        renderer: structlog.typing.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route the standard library through structlog's renderer for uniform output.
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level)

    # Let uvicorn/SQLAlchemy propagate to the configured root handler.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(noisy).handlers = []
        logging.getLogger(noisy).propagate = True

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
