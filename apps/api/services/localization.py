"""Who reads in which language.

``21-localization.md``'s Language Selection section states a priority — *"user
preference stored in Settings, browser language (first login only), application
default language"* — and this module is the server's half of it: given an
account, which language does the platform address that person in?

**It is the seam the platform had been documenting the absence of.** Every
surface that says something to somebody has been able to say it in three
languages since it shipped: :mod:`core.notifications` renders per request,
:mod:`core.email` carries chrome per language, ``apps/api/whatsapp`` carries a
descriptor per language, and the RAG pipeline, the assistant, and the report
agent all take a ``language`` on every request. What none of them had was a
*source* for the reader's own answer — ``EMAIL_DEFAULT_LANGUAGE`` was the whole
of it for outbound mail, recorded as an open question in ``progress-tracker.md``
since the email channel shipped. This module is that source, and it is deliberately
**one method wide**.

**A protocol, not a service class, and that is the load-bearing decision.** The
readers are an email delivery batch and a WhatsApp delivery batch — both of which
run on background worker threads — plus two AI surfaces that run on a request.
The open question the email channel recorded was precisely *"giving a delivery
worker a settings repository is the decision to make deliberately rather than in
passing"*, and this is the deliberate form of it: a channel receives
:class:`LanguageDirectory`, which can answer one question and cannot read a theme,
a dashboard preference, an AI setting, or anything else about an account. It also
cannot **write**, because the repository underneath it has no write method — so a
delivery channel resolving somebody's language can never change it.

**It reads a preference; it never grants anything.** There is no permission check
here and no scope, deliberately: the caller has *already* decided that this person
is being told something, through the Notification Service's per-recipient
authorization or through a case policy. Asking again in a different vocabulary
would be a second rule to keep in step with the first, and the question this
module answers — *which words* — cannot widen the answer to *whether*. That is
the structural half of the spec's *"localization cannot bypass authorization"* and
*"language switching cannot affect application permissions"*.

**Failures resolve rather than raise.** A settings lookup that could not run is
not evidence that somebody wants the default; it is evidence that the platform
does not know. Both are served by the same fallback, and the spec is explicit
about which way to fail: *"failures should gracefully fall back to the default
language"*. A notification that did not go out because a settings query timed out
would be a far worse outcome than one that went out in English.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

import structlog
from sqlalchemy.orm import Session

from core.localization import default_language, normalize_language, resolve_language
from repositories.localization import LocalizationRepository

logger = structlog.get_logger(__name__)


@runtime_checkable
class LanguageDirectory(Protocol):
    """Answers *which language does this account read in?* and nothing else.

    One question, two shapes — singular for a request and plural for a batch —
    because a delivery channel resolving a hundred recipients one at a time is a
    hundred queries, and that is the mistake this protocol exists to make
    unavailable.

    :meth:`language_for` and :meth:`languages_for` **always return a language**.
    There is no ``None`` in either return type and no exception in the contract: a
    caller composing a message must never have to branch on whether the directory
    worked, because there is nothing useful it could do with the answer
    *"unknown"* except fall back — which is what every implementation here already
    did.

    :meth:`chosen_language_for` is the deliberate exception, and its docstring is
    where that distinction is argued.
    """

    def language_for(self, user_id: uuid.UUID) -> str:
        """The language this account reads in."""
        ...

    def languages_for(
        self, user_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, str]:
        """The language each of these accounts reads in, resolved for every one."""
        ...

    def chosen_language_for(self, user_id: uuid.UUID) -> str | None:
        """The language this account **explicitly chose**, or ``None``.

        The one method here that is allowed to answer *"nothing"*, and it exists
        because two kinds of caller need two different questions answered.

        A **delivery channel** composing an email has nothing to fall back on: it
        must write the message in *some* language, so it asks
        :meth:`language_for` and gets the account's choice, then the platform's,
        then the deployment's. An **AI surface** does have something else — the
        question itself. ``core.rag.resolve_answer_language`` detects the language
        of a question written in Arabic, and a user who has never opened the
        Settings page should get an Arabic answer to an Arabic question rather
        than the platform default, which is what
        ``code-standards.md``'s *"AI must detect and respond in the user's selected
        language"* asks for on both halves.

        So this returns a **choice** and never a fallback: a stored preference, or
        ``None`` meaning *ask the question itself*. Conflating the two would make
        detection dead code the day this feature shipped, in a way no test would
        notice.
        """
        ...


class StaticLanguageDirectory:
    """Answers with one language for everybody.

    The null implementation, in the shape :class:`~services.timeline.NullRecorder`
    and :class:`~services.assistant.NullFollowUpSuggester` established: a service
    constructed by a script, a unit test, or a deployment that has not built a
    settings table should not need a database to compose a sentence.

    It is also what a **channel's** own default-language setting becomes: given
    ``EMAIL_DEFAULT_LANGUAGE``, this directory is exactly the behaviour the email
    channel had before Localization shipped, which is what makes the change
    additive rather than a rewrite.
    """

    def __init__(self, language: str | None = None) -> None:
        self._language = resolve_language(language)

    def language_for(self, user_id: uuid.UUID) -> str:  # noqa: ARG002 - protocol shape
        return self._language

    def languages_for(
        self, user_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, str]:
        return {user_id: self._language for user_id in user_ids}

    def chosen_language_for(self, user_id: uuid.UUID) -> str | None:  # noqa: ARG002
        """Always ``None``: a configured constant is nobody's choice.

        The distinction matters exactly where this class is used as a *default*
        rather than as a directory — a script, a unit test, a deployment with no
        settings table. An AI surface asking it gets "nobody chose" and falls
        through to detecting the language of the question, which is the behaviour
        that existed before ``21-localization.md`` and the right one when the
        platform genuinely does not know.
        """
        return None


class SettingsLanguageDirectory:
    """Resolves each account's own stored preference, then the platform's, then the
    deployment's.

    The three candidates are the spec's own selection chain minus its second step:
    *browser language* is something only a browser knows, so the web application
    resolves it and — on first sign-in only — **stores** it, after which it is the
    first candidate here like any other choice. That is what keeps the chain in one
    order rather than two: the server never guesses at a browser, and the browser
    never has to be consulted twice.

    **One query per batch, and the platform default read once.** A delivery batch
    hands over every recipient at once; the platform's configured default is read
    from the same session and applies to whoever has chosen nothing. Both reads are
    wrapped, because this runs on a worker thread composing a message that is
    already authorized and already owed to somebody: an unreachable database costs
    the *preference*, never the message.

    ``channel_default`` is what a channel with an opinion of its own supplies —
    ``EMAIL_DEFAULT_LANGUAGE`` or ``WHATSAPP_DEFAULT_LANGUAGE`` — and it sits
    *between* the platform setting and the application default, which is the only
    order that makes sense: an administrator's platform-wide choice should outrank
    a channel's, and both should outrank the shipped constant.
    """

    def __init__(
        self,
        repository: LocalizationRepository,
        *,
        channel_default: str | None = None,
    ) -> None:
        self._repository = repository
        self._channel_default = normalize_language(channel_default)
        #: Read lazily and once, because a batch resolves many recipients against
        #: one platform answer and a per-recipient read would be a query per
        #: person for a value that cannot differ between them.
        self._platform_default: str | None | _Unset = _UNSET

    # ------------------------------------------------------------ resolving #

    def language_for(self, user_id: uuid.UUID) -> str:
        return self.languages_for([user_id])[user_id]

    def chosen_language_for(self, user_id: uuid.UUID) -> str | None:
        """This account's stored preference, and **only** that.

        Deliberately does not fall through to the platform or application default:
        see :meth:`LanguageDirectory.chosen_language_for` for why an AI surface
        needs to tell a choice apart from a fallback.
        """
        return normalize_language(self._stored_languages([user_id]).get(user_id))

    def languages_for(
        self, user_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, str]:
        stored = self._stored_languages(user_ids)
        platform = self._platform_language()
        return {
            user_id: resolve_language(
                stored.get(user_id), platform, self._channel_default
            )
            for user_id in user_ids
        }

    # ---------------------------------------------------------------- reads #

    def _stored_languages(
        self, user_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, str]:
        """Every stored preference among ``user_ids``, or nothing on failure.

        **Admits rather than excludes**, which is the same choice
        :class:`~services.email_delivery.EmailDeliveryService`'s preference filter
        makes and the opposite of its duplicate filter: a lookup that could not run
        is not evidence about what anybody wants, and the harm of writing to
        somebody in the platform's default language is small and visible, while the
        harm of not writing at all is a missed hearing.
        """
        if not user_ids:
            return {}
        try:
            return self._repository.stored_languages(user_ids)
        except Exception:  # pragma: no cover - defensive
            # No identifiers and no values: this line says a lookup failed, which
            # is all an operator can act on. Which people were being addressed and
            # what languages they read in is exactly the index
            # `code-standards.md` forbids assembling in a log.
            logger.exception("language_preference_lookup_failed", count=len(user_ids))
            return {}

    def _platform_language(self) -> str | None:
        """The administrator's configured default, read once per directory."""
        if self._platform_default is _UNSET:
            try:
                self._platform_default = self._repository.platform_default_language()
            except Exception:  # pragma: no cover - defensive
                logger.exception("platform_default_language_lookup_failed")
                self._platform_default = None
        return self._platform_default  # type: ignore[return-value]


class _Unset:
    """Sentinel for "not read yet", distinct from a genuine ``None``."""

    __slots__ = ()


_UNSET = _Unset()


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def build_language_directory(
    session: Session, *, channel_default: str | None = None
) -> LanguageDirectory:
    """A directory backed by this session's settings tables.

    The **worker-thread counterpart** of ``api.deps.get_language_directory``, in
    the shape :func:`~services.email_delivery.build_delivery_service` established
    and for the same reason: a background worker has no request to resolve a
    dependency from, so both paths have to exist and both have to be right.
    """
    return SettingsLanguageDirectory(
        LocalizationRepository(session), channel_default=channel_default
    )


def chosen_language(
    directory: LanguageDirectory | None, user_id: uuid.UUID
) -> str | None:
    """The language this account explicitly chose, or ``None``.

    The helper the two surfaces that have *text to detect from* use — the RAG
    endpoint and the assistant. ``None`` for a directory that is absent or that has
    nothing stored, which is what lets
    :func:`~core.rag.resolve_answer_language` do its job for an account that has
    never opened the Settings page.

    Never raises: a settings lookup that failed is not evidence that somebody
    wants the default, and a question must not be refused over a preference.
    """
    if directory is None:
        return None
    try:
        return directory.chosen_language_for(user_id)
    except Exception:  # pragma: no cover - defensive
        logger.exception("chosen_language_lookup_failed")
        return None


def resolve_actor_language(
    directory: LanguageDirectory | None,
    user_id: uuid.UUID,
    *,
    requested: str | None = None,
) -> str:
    """The language one interaction happens in: an explicit request, then the
    account's own preference.

    The helper the two AI surfaces share, and the whole of
    ``21-localization.md``'s *"respond in the user's preferred language by
    default; an explicit request overrides the default for that interaction
    only"*. The override is *for that interaction* precisely because it is a
    **parameter** here rather than a write: nothing in this function stores
    anything, so asking for one answer in English cannot make a lawyer's account
    English.

    A ``None`` directory resolves the request alone against the application
    default, which is what a service constructed without one — a script, a unit
    test — should do rather than fail.
    """
    if normalize_language(requested) is not None:
        return resolve_language(requested)
    if directory is None:
        return default_language()
    return directory.language_for(user_id)


__all__ = [
    "LanguageDirectory",
    "chosen_language",
    "SettingsLanguageDirectory",
    "StaticLanguageDirectory",
    "build_language_directory",
    "resolve_actor_language",
]
