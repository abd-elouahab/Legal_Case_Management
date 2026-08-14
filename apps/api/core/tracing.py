"""Trace context: identifiers, propagation, and the ambient current span.

``22-monitoring.md`` asks for *"request tracing across the platform"* that
*"follows requests through major components"* and *"prepares the platform for
future distributed deployments"*. This module is the part of that which has to be
agreed on rather than implemented: **what a trace identifier is, how it is
carried, and how a component finds the span it is inside**.

Three decisions are worth stating, because each is what makes the rest cheap.

**The wire format is W3C Trace Context**, not one of the platform's own. A trace
identifier is 16 random bytes and a span identifier is 8, rendered as lowercase
hex, and they travel in a ``traceparent`` header. That costs nothing to produce —
:func:`new_trace_id` is two lines — and it is what makes "prepare for
OpenTelemetry" true rather than aspirational: an OTel SDK dropped in front of
this platform tomorrow would find its own header already there, already
propagated, and already correlated with every log line. Inventing an
``X-Legal-Trace`` would have been the same work and thrown away on the day it
mattered.

**The current span is a context variable**, not a parameter. A span has to be
reachable from the middleware, from a service, from a repository, and from a
SQLAlchemy event handler that nobody calls directly — threading it through every
signature would be exactly the coupling the spec forbids. A
:class:`~contextvars.ContextVar` is inherited by tasks and is restored on the way
out of every ``with`` block, so an async request handler and a worker thread each
see their own.

**An incoming ``traceparent`` is trusted for correlation and for nothing else.**
It joins this request to a caller's trace, which is its whole purpose; it grants
nothing, scopes nothing, and is never used to look anything up. It is
**validated** rather than accepted verbatim — an all-zero identifier, a wrong
length, or a non-hex character produces a fresh trace instead — because the value
ends up in logs and in an exposition endpoint, and unvalidated caller-supplied
text in a log line is how a log-injection ends up in somebody's aggregator.

The module is pure: no I/O, no configuration, no imports from :mod:`services`, so
it can be used by the middleware, the exception handlers, and the engine
instrumentation without any risk of a cycle.
"""

from __future__ import annotations

import secrets
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

__all__ = [
    "TRACEPARENT_HEADER",
    "TRACESTATE_HEADER",
    "SpanKind",
    "SpanStatus",
    "TraceContext",
    "bind_trace_context",
    "current_trace_context",
    "format_traceparent",
    "new_span_id",
    "new_trace_id",
    "parse_traceparent",
    "reset_trace_context",
]

#: The W3C header carrying trace identity across a service boundary.
TRACEPARENT_HEADER: Final[str] = "traceparent"
#: Its companion, carrying vendor-specific state. **Propagated unchanged and
#: never interpreted** — this platform adds nothing to it, and dropping a header
#: it does not understand would break a caller's tracing for no reason.
TRACESTATE_HEADER: Final[str] = "tracestate"

#: The only ``traceparent`` version this platform emits or accepts.
_VERSION: Final[str] = "00"
_TRACE_ID_HEX: Final[int] = 32
_SPAN_ID_HEX: Final[int] = 16
_INVALID_TRACE_ID: Final[str] = "0" * _TRACE_ID_HEX
_INVALID_SPAN_ID: Final[str] = "0" * _SPAN_ID_HEX
_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdef")

#: Sampled flag, per the W3C spec's ``01``.
_FLAG_SAMPLED: Final[int] = 0x01


class SpanKind(StrEnum):
    """The relationship between a span and the work around it.

    The OpenTelemetry vocabulary, used verbatim for the reason the wire format
    is: it is the one a future backend already understands, and the platform's
    own synonyms would have to be translated on the way out.
    """

    #: Handling an inbound request. The root span of an HTTP request.
    SERVER = "server"
    #: Calling something outside this process — the database, Redis, MinIO,
    #: Qdrant, a language model, a relay, the Cloud API.
    CLIENT = "client"
    #: Work inside this process with no boundary crossing: a service method, an
    #: authorization decision.
    INTERNAL = "internal"
    #: Publishing onto a queue or the event dispatcher.
    PRODUCER = "producer"
    #: Consuming from one — a background worker picking up a job.
    CONSUMER = "consumer"


class SpanStatus(StrEnum):
    """How a span ended.

    ``UNSET`` is the default and is **not** a synonym for success: a span that
    was never explicitly marked simply carries no claim, which is the honest
    reading and is what OpenTelemetry means by it. Only ``ERROR`` is counted as a
    failure anywhere.
    """

    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Where in a trace the current work sits.

    Frozen, because a context is a *value*: entering a child span produces a new
    one rather than mutating the caller's, which is what makes restoring the
    parent on the way out a matter of resetting a token instead of remembering
    what was overwritten.
    """

    #: 32 lowercase hex characters. Identifies the whole trace.
    trace_id: str
    #: 16 lowercase hex characters. Identifies this span.
    span_id: str
    #: The span this one is inside, if any.
    parent_span_id: str | None = None
    #: Whether this trace is being recorded. Present because the W3C format
    #: carries it and a future sampler will need it; this platform records
    #: everything it starts, so it is always true today.
    sampled: bool = True
    #: An upstream ``tracestate``, carried through untouched.
    trace_state: str | None = None
    #: Whether the trace identity came from a caller rather than being minted
    #: here. Reported on the root span so an operator can tell a trace that spans
    #: two services from one that begins at this API.
    remote: bool = False

    def child(self, span_id: str | None = None) -> TraceContext:
        """Return the context for a span nested inside this one."""
        return replace(
            self,
            span_id=span_id or new_span_id(),
            parent_span_id=self.span_id,
            remote=False,
        )

    @property
    def traceparent(self) -> str:
        """This context as a ``traceparent`` header value."""
        return format_traceparent(self)


def new_trace_id() -> str:
    """Mint a trace identifier: 16 random bytes as 32 lowercase hex characters."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """Mint a span identifier: 8 random bytes as 16 lowercase hex characters."""
    return secrets.token_hex(8)


def _is_hex(value: str, length: int) -> bool:
    """Whether ``value`` is exactly ``length`` lowercase hex characters."""
    return len(value) == length and all(char in _HEX_DIGITS for char in value)


def parse_traceparent(header: str | None, *, trace_state: str | None = None) -> TraceContext | None:
    """Parse an inbound ``traceparent``, or return ``None`` if it is unusable.

    ``None`` rather than an exception, and rather than a partially-trusted
    result: a malformed header is a caller's problem, and the right response is
    to start a fresh trace and carry on serving the request. Monitoring must
    never be the reason something fails, and a 400 for a bad tracing header would
    be exactly that.

    Rejects — deliberately, and each for a stated reason:

    * a version this module does not emit, because a future version may change
      the field layout and guessing at it produces confident nonsense;
    * an all-zero trace or span identifier, which the W3C spec defines as invalid
      and which is the value a broken instrumentation library emits;
    * anything that is not lowercase hex of the exact length, which is both the
      spec's rule and the thing that keeps caller-supplied text out of logs.
    """
    if not header:
        return None

    parts = header.strip().split("-")
    if len(parts) < 4:
        return None

    version, trace_id, span_id, flags = parts[0], parts[1], parts[2], parts[3]
    if version != _VERSION:
        return None
    if not _is_hex(trace_id, _TRACE_ID_HEX) or trace_id == _INVALID_TRACE_ID:
        return None
    if not _is_hex(span_id, _SPAN_ID_HEX) or span_id == _INVALID_SPAN_ID:
        return None
    if not _is_hex(flags, 2):
        return None

    return TraceContext(
        trace_id=trace_id,
        # The caller's span is *this* span's parent; this process mints its own.
        span_id=new_span_id(),
        parent_span_id=span_id,
        sampled=bool(int(flags, 16) & _FLAG_SAMPLED),
        trace_state=_clean_trace_state(trace_state),
        remote=True,
    )


def _clean_trace_state(value: str | None) -> str | None:
    """Bound and sanitise an upstream ``tracestate``.

    Propagated rather than parsed — its contents belong to whoever set them — but
    it is length-bounded and stripped of control characters before it can reach a
    log line or a response header. The W3C spec's own guidance is that
    implementations may drop it; a bounded copy is strictly more useful than that
    and strictly safer than passing arbitrary bytes through.
    """
    if not value:
        return None
    cleaned = "".join(char for char in value if char.isprintable() and char not in "\r\n")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    return cleaned[:512]


def format_traceparent(context: TraceContext) -> str:
    """Render ``context`` as a ``traceparent`` header value."""
    flags = _FLAG_SAMPLED if context.sampled else 0
    return f"{_VERSION}-{context.trace_id}-{context.span_id}-{flags:02x}"


# --------------------------------------------------------------------------- #
# The ambient current context
# --------------------------------------------------------------------------- #

#: The span the calling code is inside, if any.
#:
#: ``None`` is a normal and common value — a management script, a unit test, a
#: worker thread started before any request — and every reader below treats it as
#: "not traced" rather than as an error. Instrumentation that required a context
#: to exist would be instrumentation that could break something.
_current: ContextVar[TraceContext | None] = ContextVar("trace_context", default=None)


def current_trace_context() -> TraceContext | None:
    """Return the trace context for the calling code, or ``None``."""
    return _current.get()


def bind_trace_context(context: TraceContext) -> Token[TraceContext | None]:
    """Make ``context`` current, returning the token that restores its predecessor.

    Callers are expected to pass the token to :func:`reset_trace_context` in a
    ``finally``. That pairing — rather than "set it back to what it was" — is what
    makes nesting correct in the presence of concurrent tasks, which each carry
    their own copy of the variable.
    """
    return _current.set(context)


def reset_trace_context(token: Token[TraceContext | None]) -> None:
    """Restore the context that was current before ``token`` was issued.

    Never raises. A token from a different context — which can only happen if a
    caller has mismatched its pairs — is discarded rather than propagated,
    because a tracing bookkeeping error must not become the exception a request
    fails with.
    """
    try:
        _current.reset(token)
    except (ValueError, RuntimeError):  # pragma: no cover - defensive
        _current.set(None)
