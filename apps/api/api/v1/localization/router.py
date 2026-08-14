"""Localization endpoints.

Routes are deliberately thin: they validate input via Pydantic schemas, delegate
to :mod:`core.localization` and :mod:`services.localization`, and shape the HTTP
response. No business logic lives here — and **no route here writes a row**, which
is the property that makes this module safe to reason about: localization owns no
table, so there is nothing it could corrupt.

**Three endpoints, and the shape of the set is the spec's own.**
``21-localization.md`` asks for a *language selector* (which needs a catalogue),
for *monitoring* of four figures, and for *logging* of translation loading
failures and unsupported locale requests — two things that happen in a browser
and reach the platform no other way. So:

* ``GET /localization/languages`` — the catalogue, plus the language this caller
  is being addressed in. **No capability**, because a language selector that could
  not list its options would be a selector nobody could use, and the spec is
  explicit that *"language switching cannot affect application permissions"*;
* ``POST /localization/report`` — what a client noticed while rendering. Counted,
  never stored, and it accepts **keys only**;
* ``GET /localization/metrics`` — the operational view, gated on
  ``localization:monitor`` like every other ``*:monitor`` on the platform.

**There is deliberately no ``/localization/messages``.** The interface's
translations are static assets served by the web application, versioned with it
and cached by the browser — putting them behind an authenticated API would make
every page load wait on a database-backed process for text that changes when a
release does, and would put the login screen's own copy behind a login.

**And deliberately no endpoint that sets a language.** A language preference is a
*setting*, written through ``PATCH /settings`` like every other one; a second
write path for one stored value is how two answers to one question start to
disagree, which is the rule ``20-settings.md`` states and this feature had every
opportunity to break.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Response, status

from api.authorization import require_permission
from api.deps import (
    CurrentUser,
    LanguageDirectoryDep,
    LocalizationMetricsDep,
)
from api.deps import get_localization_repository
from core.config import settings
from core.localization import default_language, locale_tag, text_direction
from core.permissions import Permission
from models.user import User
from repositories.localization import LocalizationRepository
from schemas.localization import (
    LanguageCatalogRead,
    LocalizationMetricsRead,
    LocalizationReportCreate,
    supported_language_catalog,
)
from services.localization_metrics import distribution_with_zeros

logger = structlog.get_logger(__name__)

#: Mounted under ``/localization``.
router = APIRouter()

# --------------------------------------------------------------------------- #
# Authorized callers
# --------------------------------------------------------------------------- #

LocalizationMonitor = Annotated[
    User, Depends(require_permission(Permission.LOCALIZATION_MONITOR))
]

#: Documented error responses, merged into each endpoint's OpenAPI entry.
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "Missing, invalid, or expired access token.",
    }
}
_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "description": "The account is disabled or lacks the required permission.",
    }
}
_VALIDATION: dict[int | str, dict[str, object]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Request validation failed."}
}


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


@router.get(
    "/languages",
    response_model=LanguageCatalogRead,
    status_code=status.HTTP_200_OK,
    summary="The languages this platform serves, and the one you read in",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def list_languages(
    actor: CurrentUser,
    languages: LanguageDirectoryDep,
    metrics: LocalizationMetricsDep,
) -> LanguageCatalogRead:
    """Return the supported languages and this caller's resolved one.

    **Codes, directions, and locale tags — never names.** "Français" is
    user-facing text and an API response is a place a translation cannot live, so
    the client's own catalogue names each language. That is the same rule the
    dashboard's widget keys and the settings registry's value identifiers follow,
    and it is what lets a language selector render its own options in *each*
    language rather than in whichever one the server picked.

    ``resolved`` is the server's answer to *"which language am I addressing this
    person in?"* — their stored preference, then the platform's default, then the
    application's. A client uses it to decide which catalogue to load without
    having to re-implement the priority order, which is what keeps an email and
    an interface from disagreeing about somebody's language.

    Recorded as a resolution, which is where the *"active languages"* figure comes
    from: the languages the platform is really serving, as opposed to the ones it
    offers.
    """
    resolved = languages.language_for(actor.id)
    metrics.record_resolution(resolved)

    logger.debug(
        "localization_catalog_served",
        actor_id=str(actor.id),
        role=actor.role.value,
        language=resolved,
    )
    return LanguageCatalogRead(
        languages=supported_language_catalog(),
        default=default_language(),
        resolved=resolved,
        direction=text_direction(resolved),
        locale=locale_tag(resolved),
    )


# --------------------------------------------------------------------------- #
# What a client noticed
# --------------------------------------------------------------------------- #


@router.post(
    "/report",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Report missing translations and catalogue failures",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_VALIDATION},
)
def report_localization_problems(
    payload: LocalizationReportCreate,
    actor: CurrentUser,
    metrics: LocalizationMetricsDep,
) -> Response:
    """Record what a client could not render, and answer 204.

    ``21-localization.md`` asks Logging to record *"translation loading failures"*
    and *"unsupported locale requests"*, and Monitoring to report *"missing
    translations"*. All three happen in a browser — a catalogue is fetched there
    and a key is missed while React renders — so this endpoint is the only path by
    which they reach an operator.

    **Nothing is stored and nothing is authorized against.** The report is counted
    into a process-local recorder, exactly as a RAG run's latency is: a row per
    render would be write amplification derived from a page load, and this endpoint
    would become the busiest write on the platform. It requires authentication
    because every endpoint here does, and for no stronger reason — what it accepts
    is a list of names from this repository.

    **The report never carries text.** :class:`~schemas.localization.
    LocalizationReportCreate` discards anything that is not a short,
    whitespace-free identifier, so a client cannot send the sentence it failed to
    render even by mistake — which matters because that sentence may name a case,
    a court, or a person. The log line below carries **counts** for the same
    reason.

    Answers 204 rather than 200 with a body, because there is nothing to say back:
    a client that has just told the platform something has no use for a
    confirmation, and giving one would invite a retry loop on a page that is
    already rendering.
    """
    if not settings.LOCALIZATION_REPORTING_ENABLED:
        # Switched off is not an error: a deployment that does not want browser
        # reports gets a 204 and a client that keeps sending them costs nothing.
        # Refusing would put an error in every console on a working platform.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    keys = payload.missing_keys[: settings.LOCALIZATION_REPORT_MAX_KEYS]
    if keys:
        metrics.record_missing_translations(keys)
    for reason in payload.failures:
        metrics.record_translation_failure(reason, catalogue=payload.catalogue)

    if keys or payload.failures:
        logger.info(
            "localization_client_report",
            role=actor.role.value,
            language=payload.language,
            catalogue=payload.catalogue,
            missing_keys=len(keys),
            failures=len(payload.failures),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


@router.get(
    "/metrics",
    response_model=LocalizationMetricsRead,
    status_code=status.HTTP_200_OK,
    summary="Platform-wide localization health",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
def read_localization_metrics(
    actor: LocalizationMonitor,
    metrics: LocalizationMetricsDep,
    repository: Annotated[
        LocalizationRepository, Depends(get_localization_repository)
    ],
) -> LocalizationMetricsRead:
    """Report the four figures ``21-localization.md``'s Monitoring section names.

    **Two provenances, and the response says which.** Active languages, load
    failures, missing translations, and unsupported-locale requests accumulate in
    *this process* and carry ``since``; the language distribution and the count of
    accounts following the default are **SQL aggregates** over stored rows, for the
    reason :mod:`services.notification_metrics` and
    :mod:`services.settings_metrics` record: a count of people kept in one API
    instance's memory would reset on deploy and be wrong across replicas.

    **The distribution reports every supported language, including the ones nobody
    chose.** A breakdown that omitted Arabic until somebody switched to it would
    hide exactly the figure a deployment deciding whether to invest in Arabic
    needs — the empty-bucket rule ``19-dashboard-analytics.md`` states.

    An operational view, so it is gated on ``localization:monitor`` and reports
    **counts, language codes, translation keys, and failure reasons only** — never
    an account, never who reads in what, and never a rendered string. A
    per-account breakdown of a language preference would be a live index of who is
    who, which is the same thing ``notifications:monitor`` refuses to build.
    """
    snapshot = metrics.snapshot()

    try:
        distribution = distribution_with_zeros(repository.language_distribution())
        following_default = repository.accounts_following_default()
    except Exception:  # pragma: no cover - defensive
        # The counters are still worth serving: a monitoring view that answered
        # 500 because one of its two halves was unavailable is a view nobody can
        # rely on — the rule `19-dashboard-analytics.md` states for a widget,
        # applied to the two halves of one response.
        logger.exception("localization_distribution_unavailable")
        distribution = distribution_with_zeros({})
        following_default = 0

    logger.debug("localization_metrics_read", actor_id=str(actor.id))
    return LocalizationMetricsRead.from_snapshot(
        snapshot,
        default_language=default_language(),
        distribution=distribution,
        accounts_following_default=following_default,
        reporting_enabled=settings.LOCALIZATION_REPORTING_ENABLED,
    )
