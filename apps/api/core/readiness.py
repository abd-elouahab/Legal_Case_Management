"""Shared dependency readiness probing.

Defines the single registry of downstream dependency connectivity checks and a
helper that runs them concurrently off the event loop. Used by both the
readiness endpoint and the startup lifespan so the probe set never diverges.

``22-monitoring.md`` adds a second kind of check to this module and is explicit
about how it must differ. The four **backing services** below are probed by
opening a connection: they are required, an unreachable one means requests are
failing, and a readiness endpoint that did not touch them would be reporting
nothing. The **external services** — the language model, the mail relay, the
WhatsApp Cloud API — are probed from *configuration only*, because the spec says
so twice (*"the implementation should avoid expensive network operations"*,
*"checks should remain lightweight"*) and because the alternative is worse than
slow: a readiness probe that called a metered language model would spend a
deployment's token budget on Kubernetes' health checking, and one that opened an
SMTP connection every few seconds is how a relay starts greylisting the platform.

So the question asked of an external service is *"is this configured and switched
on?"* rather than *"is it up?"*. That is the honest question for an optional,
outward-facing side effect, and the answer to the second one is already recorded:
every send that fails writes a delivery row with a failure code, which is a far
better signal than a synthetic probe because it reflects traffic the platform
actually cares about.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import anyio
import structlog

from core.cache import check_redis_connection
from core.config import settings
from core.observability import HealthState
from core.storage import check_minio_connection
from core.vector import check_qdrant_connection
from db.session import check_database_connection

logger = structlog.get_logger(__name__)

# Dependency name -> synchronous, blocking connectivity probe.
DEPENDENCY_CHECKS: dict[str, Callable[[], None]] = {
    "postgres": check_database_connection,
    "redis": check_redis_connection,
    "minio": check_minio_connection,
    "qdrant": check_qdrant_connection,
}

#: Dependencies whose absence makes the platform unable to serve, as opposed to
#: unable to serve *one feature*.
#:
#: Qdrant is deliberately **not** here: semantic search, the assistant, and report
#: generation refuse while it is down, and cases, documents, users, notifications,
#: and the timeline are entirely unaffected. Marking it required would make a
#: platform that is 80 % working report itself as down, and an orchestrator would
#: take it out of rotation — which is the one response that helps nobody.
REQUIRED_DEPENDENCIES: frozenset[str] = frozenset({"postgres", "redis", "minio"})


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single dependency probe."""

    healthy: bool
    error: str | None = None


async def probe_dependencies() -> dict[str, ProbeResult]:
    """Run every dependency check concurrently in worker threads.

    Total latency approximates the slowest single probe rather than their sum.
    """
    results: dict[str, ProbeResult] = {}

    async def _run(name: str, check: Callable[[], None]) -> None:
        try:
            await anyio.to_thread.run_sync(check)
            results[name] = ProbeResult(healthy=True)
        except Exception as exc:  # surface any failure as an unhealthy dependency
            results[name] = ProbeResult(healthy=False, error=str(exc))

    async with anyio.create_task_group() as task_group:
        for name, check in DEPENDENCY_CHECKS.items():
            task_group.start_soon(_run, name, check)

    return results


@dataclass(frozen=True, slots=True)
class ExternalServiceStatus:
    """Whether one outward-facing integration is usable, from configuration alone.

    ``configured`` and ``enabled`` are separate because they fail differently and
    an operator's next action differs: a service that is enabled but not
    configured is a deployment that turned something on and forgot a setting,
    which is a mistake worth surfacing loudly; one that is simply switched off is
    a decision.
    """

    name: str
    enabled: bool
    configured: bool
    state: HealthState
    #: What is missing, by setting **name** — never by value. Naming the setting
    #: is what turns "WhatsApp is not working" into a fix; naming the value would
    #: put a token in a monitoring response.
    detail: str | None = None


def probe_external_services() -> dict[str, ExternalServiceStatus]:
    """Report the configured state of every external integration. Never raises.

    Synchronous and instantaneous — it reads configuration and asks each
    provider's own ``is_available`` — so it can be called from a readiness
    endpoint an orchestrator hits every few seconds without any of the costs the
    module docstring names.

    The providers are imported **inside the function**, for the reason
    :func:`~services.system_metrics.worker_pools` gives: importing them at module
    scope would mean a readiness module pulls the language-model SDK and the mail
    stack into every process that touches it, including the ones that never send
    anything.
    """
    statuses: dict[str, ExternalServiceStatus] = {}

    def _record(
        name: str, *, enabled: bool, available: bool, detail: str | None = None
    ) -> None:
        if not enabled:
            statuses[name] = ExternalServiceStatus(
                name=name, enabled=False, configured=available, state=HealthState.DISABLED
            )
            return
        statuses[name] = ExternalServiceStatus(
            name=name,
            enabled=True,
            configured=available,
            # Degraded rather than unhealthy: the platform is serving, and one
            # optional integration is not. Reporting this as unhealthy would make
            # `/ready` fail on a deployment that has simply not finished
            # configuring email, and an orchestrator would refuse to route to it.
            state=HealthState.HEALTHY if available else HealthState.DEGRADED,
            detail=detail,
        )

    try:
        from services.llm import get_llm_provider

        available = get_llm_provider().is_available()
        _record(
            "llm",
            enabled=settings.RAG_ENABLED,
            available=available,
            detail=None if available else "LLM_API_KEY is not configured.",
        )
    except Exception:  # pragma: no cover - defensive
        statuses["llm"] = ExternalServiceStatus(
            name="llm", enabled=settings.RAG_ENABLED, configured=False, state=HealthState.UNKNOWN
        )

    try:
        from services.email_provider import get_email_provider

        available = get_email_provider().is_available()
        _record(
            "email",
            enabled=settings.EMAIL_ENABLED,
            available=available,
            detail=None if available else "SMTP_HOST is not configured.",
        )
    except Exception:  # pragma: no cover - defensive
        statuses["email"] = ExternalServiceStatus(
            name="email", enabled=settings.EMAIL_ENABLED, configured=False, state=HealthState.UNKNOWN
        )

    try:
        from services.whatsapp_provider import get_whatsapp_provider

        available = get_whatsapp_provider().is_available()
        _record(
            "whatsapp",
            enabled=settings.WHATSAPP_ENABLED,
            available=available,
            detail=(
                None
                if available
                else "WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID is not configured."
            ),
        )
    except Exception:  # pragma: no cover - defensive
        statuses["whatsapp"] = ExternalServiceStatus(
            name="whatsapp",
            enabled=settings.WHATSAPP_ENABLED,
            configured=False,
            state=HealthState.UNKNOWN,
        )

    return statuses
