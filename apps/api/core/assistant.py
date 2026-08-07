"""AI Legal Assistant domain utilities.

Small, pure helpers shared by the assistant's schemas, repository, service, and
metrics recorder: how a conversation gets its title, how a follow-up question is
resolved against what was asked before it, how the model's suggested next
questions are parsed, and what a conversation's preview line says.

:class:`~models.conversation.ConversationRole` is **re-exported** here rather
than redeclared, exactly as :mod:`core.roles` re-exports
:class:`~models.user.UserRole`: the enum is persisted, so the storage definition
is the canonical one, and domain code still gets a single intention-revealing
import site.

They live here rather than inside a service method for the same reason
:mod:`core.cases`, :mod:`core.documents`, :mod:`core.timeline`, :mod:`core.ocr`,
:mod:`core.indexing`, :mod:`core.search`, and :mod:`core.rag` exist — the same
rules must apply however a message arrives, and they can be unit-tested without a
database, a request, a running Qdrant, an embedding model, or an API key.

**Nothing here retrieves, prompts, cites, or calls a model.** All four belong to
the RAG pipeline, which this feature consumes rather than reimplements
(``13-ai-legal-assistant.md``: *"The AI Assistant must not duplicate retrieval,
prompt construction, or orchestration logic already implemented by the RAG
Pipeline"*). What this module owns is the part that is genuinely the
*assistant's*: a conversation is a sequence of turns, and turns refer to one
another.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from core.config import settings
from core.indexing import LANGUAGE_ARABIC, LANGUAGE_ENGLISH, LANGUAGE_FRENCH
from core.rag import normalize_question
from models.conversation import ConversationRole

#: Shortest conversation title worth storing, in characters after normalisation.
MIN_TITLE_LENGTH = 1

#: Longest preview of a conversation's last message, in characters.
#:
#: A list row, not a summary: enough to recognise the thread, short enough that
#: twenty of them fit on a screen. Computed server-side so every surface — the
#: sidebar, the mobile drawer, a future notification — cuts at the same place.
PREVIEW_LENGTH = 140

#: Below this many characters, a question is treated as possibly *dependent* on
#: the turn before it and is resolved against the conversation (see
#: :func:`resolve_question`).
#:
#: Length is the signal because it is the only one that is honest across three
#: languages. A list of anaphoric words ("celui-ci", "هذا", "it") would have to be
#: maintained per language and would still miss *"Et le délai ?"*, which contains
#: no pronoun at all. A short question, by contrast, is almost never
#: self-contained in a legal conversation — and the cost of resolving one that
#: was is bounded: it broadens the retrieval query with terms from the same
#: matter, which adds candidate passages rather than replacing them, and every
#: candidate is still scoped to the caller's cases by the search service.
FOLLOWUP_QUESTION_MAX_LENGTH = 90

#: Strips a leading bullet, dash, or "1." from a suggested follow-up.
#:
#: RUF001 flags the en and em dashes as "ambiguous" — it is warning that they
#: resemble a hyphen, which is precisely why they are here: a model writing a
#: bulleted list uses whichever of the three it feels like, and the stripping has
#: to cover all of them.
_SUGGESTION_PREFIX = re.compile(r"^\s*(?:[-*•–—]|\d{1,2}[.)])\s*")  # noqa: RUF001

#: Strips the surrounding quotation marks a model sometimes wraps a suggestion
#: in, in the three scripts the platform serves. The guillemets are French, the
#: low-9 quote is used in Arabic typesetting, and RUF001's complaint about the
#: single angle marks is the same one: they look like `<` and are not.
_SUGGESTION_QUOTES = "\"'«»“”„‹›"  # noqa: RUF001


#: The label a conversation carries before its first message names it, per
#: language.
#:
#: A conversation is created before anything has been said in it, so it needs a
#: name that is honest about that rather than a guess at a subject.
UNTITLED_CONVERSATION: Mapping[str, str] = MappingProxyType(
    {
        LANGUAGE_FRENCH: "Nouvelle conversation",
        LANGUAGE_ARABIC: "محادثة جديدة",
        LANGUAGE_ENGLISH: "New conversation",
    }
)

#: How a resolved follow-up names the earlier question it is being read against,
#: per language.
#:
#: Written into the text handed to the pipeline, so it is phrased for a *reader*
#: — the model receives it as prose, exactly as it receives the question.
CONTEXT_PREAMBLE: Mapping[str, str] = MappingProxyType(
    {
        LANGUAGE_FRENCH: "Dans la continuité de",
        LANGUAGE_ARABIC: "في سياق",
        LANGUAGE_ENGLISH: "Following on from",
    }
)

#: How that resolved text introduces the question itself, per language.
CONTEXT_QUESTION_LABEL: Mapping[str, str] = MappingProxyType(
    {
        LANGUAGE_FRENCH: "Question",
        LANGUAGE_ARABIC: "السؤال",
        LANGUAGE_ENGLISH: "Question",
    }
)


def untitled_conversation(language: str) -> str:
    """The placeholder title for a conversation with nothing in it yet."""
    return UNTITLED_CONVERSATION.get(language, UNTITLED_CONVERSATION[LANGUAGE_FRENCH])


# --------------------------------------------------------------------------- #
# Titles
# --------------------------------------------------------------------------- #


def normalize_title(value: str) -> str:
    """Reduce a title to what is actually stored.

    Uses the same normaliser the question does — NFC, collapsed whitespace,
    control characters dropped — because a title is displayed beside Arabic and
    French text and a decomposed form would render inconsistently next to the
    composed one. Then clipped to the configured ceiling at a word boundary, so a
    long title becomes a short one rather than a rejected request.
    """
    normalized = normalize_question(value)
    return _clip(normalized, settings.ASSISTANT_TITLE_MAX_LENGTH)


def derive_title(question: str, *, language: str) -> str:
    """Name a conversation after the question that opened it.

    ``13-ai-legal-assistant.md`` asks for titles that are *short, descriptive,
    and editable*, and all three are satisfied by the user's own first sentence.

    **Deliberately not generated by a model**, and the reasons compound:

    * a title is the one place a hallucination would be invisible — nobody
      re-reads a list row against the conversation it names, so a plausible wrong
      subject would simply become what that thread is called;
    * it would double the model calls the first message of every conversation
      costs, on a provider whose free tier allows twenty a day
      (``progress-tracker.md``);
    * and the user's own words are, by construction, the most faithful
      description of what they asked.

    The result is editable, which is where a user who wants a different title
    gets one — the spec's own remedy.
    """
    title = normalize_title(question)
    return title if len(title) >= MIN_TITLE_LENGTH else untitled_conversation(language)


def message_preview(text: str) -> str:
    """The single line a conversation list shows beneath its title."""
    return _clip(normalize_question(text), PREVIEW_LENGTH)


# --------------------------------------------------------------------------- #
# Conversational context
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ResolvedQuestion:
    """A question as the pipeline will receive it, and what went into it.

    ``text`` is what is retrieved with and prompted with; ``turns`` is how many
    earlier questions were carried into it. Both are reported: the second is what
    makes "this answer used conversational context" visible to a reader of one
    message rather than only to someone reading the whole thread.
    """

    text: str
    turns: int

    @property
    def used_history(self) -> bool:
        """Whether anything from earlier in the conversation was carried."""
        return self.turns > 0


def is_followup_question(question: str) -> bool:
    """Whether a question is short enough to be read against what came before it.

    See :data:`FOLLOWUP_QUESTION_MAX_LENGTH` for why length is the signal.
    """
    return len(question.strip()) <= FOLLOWUP_QUESTION_MAX_LENGTH


def resolve_question(
    question: str,
    *,
    history: Sequence[str],
    language: str,
    max_turns: int | None = None,
    max_characters: int | None = None,
) -> ResolvedQuestion:
    """Turn a follow-up into a question that stands on its own.

    ``13-ai-legal-assistant.md`` requires conversation history to reach the RAG
    pipeline *"when appropriate"*, to preserve context, to support follow-up
    questions, and to **avoid unnecessary history growth**. This function is all
    four, and the design follows from one constraint the pipeline imposes:

    **the pipeline's ``question`` is both the retrieval query and the text the
    model is asked to answer.** So history cannot simply be prepended — that
    would make the model answer the *previous* question again. What is prepended
    instead is a short, labelled reference to the earlier question, which reads
    to a model as "this follows on from X, now answer Y" and reads to the
    embedder as "X and Y are the same subject". One string, correct for both uses.

    Only earlier **user questions** are carried, never answers. An answer is a
    paragraph: it would dominate both the embedding and the prompt, and it is
    already the pipeline's own output rather than anything the user asked for.

    Two independent bounds, because they limit different things — ``max_turns``
    bounds *how far back* (a question resolved against a thread from an hour ago
    retrieves that thread's subject), and ``max_characters`` bounds *how much*
    (one long earlier question must not fill the retrieval query on its own).

    A self-contained question is returned unchanged, with ``turns`` of zero: the
    honest statement that no context was needed.

    **Where a model-based rewriter goes.** Resolving *"Et pour celui-ci ?"* into
    a genuinely standalone sentence needs a model. This is deliberately not that:
    it is a bounded, deterministic, free, and inspectable approximation that
    broadens rather than replaces, which is the safe direction — a broadened
    query retrieves a superset of candidates, all of them still scoped to the
    caller's cases by the search service. A rewriter would substitute for this
    function and change nothing above it.
    """
    asked = question.strip()
    turn_budget = settings.ASSISTANT_CONTEXT_MESSAGES if max_turns is None else max_turns
    character_budget = (
        settings.ASSISTANT_CONTEXT_MAX_CHARACTERS if max_characters is None else max_characters
    )

    if turn_budget <= 0 or character_budget <= 0 or not history or not is_followup_question(asked):
        return ResolvedQuestion(text=asked, turns=0)

    # Consumed most-recent-first so the *nearest* turn is the one that survives a
    # tight budget — a follow-up refers to what was just said, not to the opening
    # of the thread.
    carried: list[str] = []
    remaining = character_budget

    for earlier in reversed(list(history)[-turn_budget:]):
        candidate = normalize_question(earlier)
        if not candidate or len(candidate) > remaining:
            break
        carried.append(candidate)
        remaining -= len(candidate)

    if not carried:
        return ResolvedQuestion(text=asked, turns=0)

    # Reversed back into chronological order: the reference reads as a thread.
    preamble = CONTEXT_PREAMBLE.get(language, CONTEXT_PREAMBLE[LANGUAGE_FRENCH])
    label = CONTEXT_QUESTION_LABEL.get(language, CONTEXT_QUESTION_LABEL[LANGUAGE_FRENCH])
    joined = " ".join(reversed(carried))

    return ResolvedQuestion(text=f"{preamble} : {joined}\n{label} : {asked}", turns=len(carried))


def history_questions(pairs: Sequence[tuple[str, str]]) -> list[str]:
    """The user questions out of a transcript, oldest first.

    Takes ``(role, content)`` pairs so the caller does not have to import an ORM
    model to use this, which is what keeps this module free of one.
    """
    return [
        content
        for role, content in pairs
        if role == ConversationRole.USER.value and content.strip()
    ]


# --------------------------------------------------------------------------- #
# Suggested follow-up questions
# --------------------------------------------------------------------------- #


def parse_suggestions(
    text: str,
    *,
    limit: int | None = None,
    max_length: int | None = None,
    exclude: Sequence[str] = (),
    truncated: bool = False,
) -> list[str]:
    """Read the model's suggested next questions out of its reply.

    One per line, because that is what the template asks for and because any
    richer format (JSON, XML) is one more thing a model can get subtly wrong in a
    way that discards a perfectly good list.

    Everything here is a *rejection* rule rather than a repair rule, and that is
    the point: a suggestion is a question the user will send verbatim, so one
    that has to be fixed up before it can be sent is one that should not be
    offered.

    * bullets, dashes, and numbering are stripped — they are formatting the model
      added, not part of the question;
    * a suggestion longer than ``max_length`` is **dropped, never clipped**: a
      truncated question changes meaning, and offering one would be worse than
      offering none;
    * **when the reply itself was truncated, the last line goes** — see below;
    * duplicates are dropped case-insensitively, as is anything matching a
      question already asked — re-suggesting what was just answered is the most
      common failure of this feature and the most annoying;
    * and the list is capped, because a menu of ten is something to read rather
      than a shortcut to take.

    ``truncated`` is the provider's own report that generation stopped at the
    output ceiling, and honouring it closes a hole that no length rule can. A
    cut-off reply ends **mid-line**, so its final entry is half a question —
    *"Quel est le domicile du bailleur pour le"* — which is short, unique, and
    otherwise indistinguishable from a real one. Offering that as something to
    send is exactly the failure the "dropped, never clipped" rule exists to
    prevent, arriving by a route that rule cannot see. Found by a live run
    against the real model, not by a hermetic test: the doubles return whatever
    string a test wrote, and none of them had ever been cut off.
    """
    wanted = settings.ASSISTANT_SUGGESTION_COUNT if limit is None else limit
    ceiling = settings.ASSISTANT_SUGGESTION_MAX_LENGTH if max_length is None else max_length

    if wanted <= 0:
        return []

    seen = {normalize_question(item).casefold() for item in exclude if item.strip()}
    suggestions: list[str] = []

    lines = text.splitlines()
    if truncated and lines:
        # Only the final line can have been cut, and it always was: the ones
        # before it were complete enough for the model to have started another.
        lines = lines[:-1]

    for line in lines:
        candidate = normalize_question(_SUGGESTION_PREFIX.sub("", line)).strip(_SUGGESTION_QUOTES)
        candidate = candidate.strip()

        if not candidate or len(candidate) > ceiling:
            continue
        if not any(character.isalnum() for character in candidate):
            continue

        key = candidate.casefold()
        if key in seen:
            continue

        seen.add(key)
        suggestions.append(candidate)

        if len(suggestions) >= wanted:
            break

    return suggestions


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _clip(text: str, budget: int) -> str:
    """Shorten ``text`` to ``budget`` characters, at a word boundary where one exists.

    The same rule :func:`~core.rag.clip_passage` applies to an excerpt, and for
    the same reason: a cut mid-word reads as a transcription error. No ellipsis
    is appended here — a title and a preview are already understood to be short
    forms, and a trailing "…" in a list of twenty rows is noise.
    """
    if budget <= 0 or len(text) <= budget:
        return text

    clipped = text[:budget]
    boundary = clipped.rfind(" ")
    # Only honoured when the boundary keeps most of the budget: an Arabic title
    # may contain no space in the window at all, and pulling back to character 3
    # would throw the title away to avoid splitting a word.
    if boundary > budget * 3 // 4:
        clipped = clipped[:boundary]
    return clipped.rstrip()


__all__ = [
    "CONTEXT_PREAMBLE",
    "CONTEXT_QUESTION_LABEL",
    "FOLLOWUP_QUESTION_MAX_LENGTH",
    "MIN_TITLE_LENGTH",
    "PREVIEW_LENGTH",
    "UNTITLED_CONVERSATION",
    "ConversationRole",
    "ResolvedQuestion",
    "derive_title",
    "history_questions",
    "is_followup_question",
    "message_preview",
    "normalize_title",
    "parse_suggestions",
    "resolve_question",
    "untitled_conversation",
]
