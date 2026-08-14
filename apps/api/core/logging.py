"""Structured logging configuration using ``structlog``.

Emits JSON logs in production/testing and human-readable colored logs in
development. The standard library ``logging`` module is routed through
``structlog`` so that libraries (uvicorn, SQLAlchemy, etc.) share the same
output format. ``print()`` must never be used in application code.

``22-monitoring.md`` asks for three things this module owns, and each is a
processor in the chain below rather than a rule call sites have to remember:

* **Consistent context on every entry.** ``timestamp``, ``level``, ``logger``,
  and — through :func:`structlog.contextvars.merge_contextvars` — the request
  identifier, method, route, user, and role that :mod:`core.middleware` binds
  once per request. A module logs ``case_created`` with a case id; everything
  needed to place that line in a request, a session, and a person's afternoon is
  merged in underneath it.
* **Trace correlation.** :func:`_add_trace_context` copies the current
  :class:`~core.tracing.TraceContext` onto every entry, so a log line and a span
  can be joined without either knowing about the other. This is what makes the
  three pillars actually complementary rather than three separate views.
* **The Logging Policy, enforced by the pipeline.** :func:`_redact_sensitive`
  runs over every entry, from every module, including the ones libraries emit,
  and replaces the value of any field whose *name* suggests a credential or
  document content. Passwords, tokens, API secrets, prompts, extracted text, and
  report bodies therefore cannot be logged by accident — not because every call
  site is careful, but because the last thing that touches an entry before it is
  rendered removes them.

The renderer is chosen by configuration, and both branches share the same
processor chain, so a development console log and a production JSON log contain
exactly the same fields.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, cast

import structlog

from core.config import settings
from core.observability import redact_mapping
from core.tracing import current_trace_context

_configured = False


def _add_trace_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Copy the current trace identifiers onto the entry, if there is a trace.

    Absent rather than null when there is none — a management script, a startup
    routine, a worker running outside any request — because a field that is
    always present and usually empty is a field readers learn to skip.

    Never raises: a logging processor that could fail would make every log call a
    possible exception, which is the opposite of what a monitoring feature is for.
    """
    try:
        context = current_trace_context()
        if context is not None:
            event_dict.setdefault("trace_id", context.trace_id)
            event_dict.setdefault("span_id", context.span_id)
    except Exception:  # pragma: no cover - defensive
        pass
    return event_dict


def _redact_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Remove the value of every field whose name marks it sensitive.

    Placed **last** in the shared chain, immediately before the renderer, so it
    sees everything every earlier processor added — including the fields bound
    into the context variables and the ones a library put there. A scrubber that
    ran early would be a scrubber that only covered its own call site.

    Never raises, for the reason :func:`_add_trace_context` does not.
    """
    try:
        return redact_mapping(event_dict)
    except Exception:  # pragma: no cover - defensive
        return event_dict


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
        _add_trace_context,
        # Last, and deliberately after `format_exc_info`: a formatted traceback is
        # a field like any other, and a credential that appeared in an exception's
        # arguments must not survive because it arrived by that route.
        _redact_sensitive,
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
