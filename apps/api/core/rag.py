"""Retrieval-Augmented Generation domain utilities.

Small, pure helpers shared by the RAG schemas, prompt library, LLM boundary,
workflow graph, service, and metrics recorder: what counts as an answerable
question, how a question is referred to in a log line *without being written to
it*, how retrieved passages are fitted to a context budget, how a citation
marker is recognised in a generated answer, the failure vocabulary, and the
sentinel the model uses to say "the context does not support an answer".

They live here rather than inside a service method for the same reason
:mod:`core.cases`, :mod:`core.documents`, :mod:`core.timeline`, :mod:`core.ocr`,
:mod:`core.indexing`, and :mod:`core.search` exist — the same rules must apply
however a question arrives, and they can be unit-tested without a database, a
request, a running Qdrant, a downloaded embedding model, or an API key.

**Nothing here retrieves, renders a template, or calls a model.** Retrieval is
:class:`~services.search.SearchService`'s (and *only* its — see
:mod:`services.rag`), prompt rendering is :mod:`services.prompts`', model
invocation is :mod:`services.llm`', and the order they happen in is
:mod:`services.rag_graph`'s. This module holds only the decisions that are the
*platform's* rather than a library's.

**And nothing here manages a conversation.** ``ai-architecture.md`` states that
the RAG pipeline must never manage conversations; the boundary is structural,
because no module in this feature imports a conversation model, a message, or a
session.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType

from core.config import settings
from core.indexing import (
    LANGUAGE_ARABIC,
    LANGUAGE_ENGLISH,
    LANGUAGE_FRENCH,
    LANGUAGE_UNKNOWN,
    detect_language,
)
from core.localization import default_language
from core.search import normalize_query, query_fingerprint

#: Shortest question worth answering, in characters after normalisation.
#:
#: The same floor :data:`~core.search.MIN_QUERY_LENGTH` sets for a search, and
#: for a stronger reason: a one-character question retrieves arbitrary passages
#: *and* then spends a model call rewriting them into a confident-sounding
#: paragraph. Refusing is much cheaper than answering badly.
MIN_QUESTION_LENGTH = 2

#: Shortest usable remainder of a context budget, in characters.
#:
#: A passage clipped below this is a sentence fragment: it cannot support an
#: answer, and quoting a fragment as evidence is worse than omitting the passage.
#: So the last passage is either truncated to something readable or dropped.
MIN_PASSAGE_CHARACTERS = 200

#: What the model is told to reply, alone, when the retrieved context does not
#: support an answer.
#:
#: A sentinel rather than a phrase to match, because the phrase would have to be
#: matched in Arabic, French, and English — and a model that paraphrased it
#: slightly would be read as a confident answer. An exact token the prompt names
#: is unambiguous in every language, and its absence is equally unambiguous.
INSUFFICIENT_EVIDENCE_MARKER = "INSUFFICIENT_EVIDENCE"

#: Recognises the ``[3]`` citation markers the prompt asks the model to use.
#:
#: Compiled once, and deliberately narrow: only digits inside square brackets,
#: so a bracketed statutory reference (``[Article 12 bis]``) in the source text
#: is not mistaken for a citation the model made.
CITATION_MARKER_PATTERN = re.compile(r"\[(\d{1,3})\]")

#: Human-readable names for the languages an answer may be written in, keyed by
#: the ISO 639-1 code :func:`~core.indexing.detect_language` produces.
#:
#: Named in the prompt rather than passed as a code, because "answer in fr" is a
#: weaker instruction to a language model than "answer in French" — and because
#: the value is written into a template, where a code would read as noise.
LANGUAGE_NAMES: Mapping[str, str] = MappingProxyType(
    {
        LANGUAGE_ARABIC: "Arabic",
        LANGUAGE_FRENCH: "French",
        LANGUAGE_ENGLISH: "English",
    }
)

#: Languages a request may explicitly ask for. Deliberately the three the
#: platform serves (`project-overview.md`: Arabic and French interfaces, English
#: as the third OCR/embedding language) — not every language the model speaks.
SUPPORTED_ANSWER_LANGUAGES: frozenset[str] = frozenset(LANGUAGE_NAMES)


class RagFailureCode(StrEnum):
    """Why a question could not be answered.

    Machine-readable, so the monitoring view can group failures and a client can
    branch on the cause without parsing a sentence. The members cover exactly
    the failures ``12-rag-pipeline.md`` lists under "Errors" — retrieval
    failures, LLM failures, timeout, malformed responses, context overflow —
    plus the catch-all every other stage of this pipeline carries.

    **There is deliberately no member for "no supporting evidence".** That is a
    successful outcome of the pipeline, not a failure of it: the corpus does not
    contain the answer, which is itself an answer, and counting it here would
    make the failure rate a measure of the corpus rather than of the platform.
    """

    #: Retrieval could not run — the embedding model or the vector database was
    #: unavailable. Nothing about the question is wrong; the same question will
    #: work once retrieval is healthy.
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"
    #: No language model is configured, or the configured one cannot be reached.
    LLM_UNAVAILABLE = "llm_unavailable"
    #: A deadline passed — either the model's own, or the whole run's. One code
    #: for both, deliberately: the caller's remedy is identical (ask again, or
    #: ask something narrower), and splitting it would suggest a distinction they
    #: can act on. Which deadline it was is in the log, where an operator is.
    TIMEOUT = "timeout"
    #: The model was reached and refused, or failed mid-generation.
    LLM_FAILURE = "llm_failure"
    #: The model answered with something unusable — empty, or truncated to
    #: nothing. Distinct from a failure to reach it, because the remedy is
    #: different: a retry may help here, a page of configuration will not.
    MALFORMED_RESPONSE = "malformed_response"
    #: The question alone exceeds what can be sent to the model, so no context
    #: would fit beside it. Distinct from an over-long question rejected by
    #: validation: this is the *deployment's* budget being too small.
    CONTEXT_OVERFLOW = "context_overflow"
    #: Anything the branches above do not describe.
    UNKNOWN = "unknown"


#: Human-readable explanation per failure code.
#:
#: Kept here rather than raised with the exception so the message a user reads is
#: written once and cannot vary between call sites. Every one is deliberately
#: free of the *question* and of any document contents — it says what went wrong
#: with the request, never what was being asked or read.
FAILURE_MESSAGES: Mapping[RagFailureCode, str] = MappingProxyType(
    {
        RagFailureCode.RETRIEVAL_UNAVAILABLE: (
            "Supporting documents could not be retrieved. Indexed documents are unaffected."
        ),
        RagFailureCode.LLM_UNAVAILABLE: (
            "The AI service is unavailable. Documents and search are unaffected."
        ),
        RagFailureCode.TIMEOUT: (
            "The answer took too long to produce. Try asking again, or ask something narrower."
        ),
        RagFailureCode.LLM_FAILURE: "The AI service could not answer this question.",
        RagFailureCode.MALFORMED_RESPONSE: (
            "The AI service returned an unusable answer. Try asking again."
        ),
        RagFailureCode.CONTEXT_OVERFLOW: (
            "This question is too long to answer with supporting documents. Shorten it and ask "
            "again."
        ),
        RagFailureCode.UNKNOWN: "The question could not be answered.",
    }
)

#: What a user is told when retrieval found nothing to ground an answer in, per
#: language.
#:
#: Written by the platform rather than generated, and that is the whole of the
#: spec's "No Evidence Handling": a model asked to explain that it found nothing
#: will sometimes explain it *and then answer anyway* from its own training,
#: which is exactly the fabrication this pipeline exists to prevent. A fixed
#: sentence cannot speculate.
NO_EVIDENCE_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        LANGUAGE_FRENCH: (
            "Je n'ai trouvé aucun document justificatif permettant de répondre à cette question. "
            "Reformulez la question, ou vérifiez que les documents concernés ont bien été indexés."
        ),
        LANGUAGE_ARABIC: (
            "لم أعثر على أي مستند داعم للإجابة عن هذا السؤال. "
            "يُرجى إعادة صياغة السؤال أو التأكد من فهرسة المستندات ذات الصلة."
        ),
        LANGUAGE_ENGLISH: (
            "I could not find any supporting document that answers this question. Rephrase the "
            "question, or check that the relevant documents have been indexed."
        ),
    }
)


def failure_message(code: RagFailureCode) -> str:
    """The message shown for a failure code.

    Falls back to the generic explanation for a code with no entry, so a future
    member added without a message still reads as a sentence rather than as an
    identifier.
    """
    return FAILURE_MESSAGES.get(code, FAILURE_MESSAGES[RagFailureCode.UNKNOWN])


def no_evidence_message(language: str) -> str:
    """What the user is told when nothing was retrieved to ground an answer in.

    Falls back to English for a language with no entry, which is the same
    posture :func:`failure_message` takes: an unfamiliar code must still produce
    a sentence.
    """
    return NO_EVIDENCE_MESSAGES.get(language, NO_EVIDENCE_MESSAGES[LANGUAGE_ENGLISH])


# --------------------------------------------------------------------------- #
# The question
# --------------------------------------------------------------------------- #


def normalize_question(value: str) -> str:
    """Reduce a question to the exact text that is retrieved and prompted with.

    Delegates to :func:`~core.search.normalize_query` rather than repeating it,
    because the question **is** the search query one step later: NFC, collapsed
    whitespace, control characters dropped. Two normalisers would be two chances
    for a French or Arabic question to embed differently here than it does
    there, which would make the retrieved passages a function of which module
    normalised last.
    """
    return normalize_query(value)


def is_answerable_question(value: str) -> bool:
    """Whether a normalised question carries enough to answer.

    Same rule as :func:`~core.search.is_searchable_query`, and named separately
    because the two can legitimately diverge later — a question has a floor a
    keyword search does not.
    """
    trimmed = value.strip()
    if len(trimmed) < MIN_QUESTION_LENGTH:
        return False
    return any(character.isalnum() for character in trimmed)


def question_fingerprint(value: str) -> str:
    """A stable, non-reversible handle for a question, for logs and metrics.

    Delegates to :func:`~core.search.query_fingerprint`, deliberately: a
    question asked of the assistant and the same text typed into search must
    produce the **same** fingerprint, or an operator cannot correlate "this
    question always fails" across the two surfaces. Salted with the deployment
    secret there, for the reason given there.
    """
    return query_fingerprint(value)


def loggable_question(value: str) -> str | None:
    """The question text, only if this deployment has explicitly opted in.

    ``RAG_LOG_QUESTIONS`` is the RAG half of ``SEARCH_LOG_QUERIES``: a separate
    switch rather than a shared one, because the two carry different risk. A
    search query is a phrase; a question put to the assistant is a sentence about
    a client's matter, and an operator who wants query text for tuning retrieval
    should not have to accept the second to get the first.

    Off by default, and every call site logs :func:`question_fingerprint`
    regardless — so turning it on adds the text beside the handle rather than
    replacing it.
    """
    if not settings.RAG_LOG_QUESTIONS:
        return None
    return normalize_question(value)


def resolve_answer_language(question: str, requested: str | None = None) -> str:
    """Decide which language the answer must be written in.

    ``code-standards.md``: *"AI must detect and respond in the user's selected
    language (Arabic or French)."* Both halves of that are honoured, in order:

    * an **explicit** request wins, because it is the user's stated selection —
      a lawyer reading a French interface may well ask about an Arabic filing
      and want the answer in French. It is also what the localized frontend will
      send, so detection is a *fallback* rather than the primary path;
    * otherwise the language is **detected from the question** with the same
      script heuristic that labelled every indexed passage
      (:func:`~core.indexing.detect_language`), so one rule serves both sides;
    * and anything the heuristic does not resolve confidently falls back to the
      **application default** (:func:`~core.localization.default_language`).

    **The detected-``en``/unknown branch is the one judgement call in this
    function, and ``21-localization.md`` changed its answer.**
    ``detect_language`` tells French from English by *diacritics*, which the
    codebase already recorded as producing ``en`` for accent-free French. On a
    page of prose that is harmless — the label is a filter hint. On a **question**
    it is not, and it is not even rare: *"Quand le loyer est-il payable ?"* is
    impeccable French containing no accented character at all, so the heuristic
    cannot do better than a coin flip on short French questions.

    Until Localization shipped, French was hard-coded here as the safer of the
    two, because English was not an interface language and a French-speaking user
    was overwhelmingly the likelier author of an ambiguous question. Two things
    changed. English is now a first-class interface language *and the shipped
    application default*, so a deployment's own answer is a better guess than this
    module's; and the detection branch is now **nearly unreachable**, because
    :class:`~services.assistant.AssistantService` supplies the asker's stored
    language preference as ``requested`` on every message. The heuristic is what
    remains for an API client that sends neither — and for that caller the honest
    answer is the deployment's default rather than a language this function
    picked for it.
    """
    if requested:
        wanted = requested.strip().lower()
        if wanted in SUPPORTED_ANSWER_LANGUAGES:
            return wanted

    detected = detect_language(question)
    if detected in {LANGUAGE_UNKNOWN, LANGUAGE_ENGLISH}:
        return default_language()
    return detected


def language_name(language: str) -> str:
    """The prompt-facing name of a language code."""
    return LANGUAGE_NAMES.get(language, LANGUAGE_NAMES[LANGUAGE_ENGLISH])


# --------------------------------------------------------------------------- #
# The context
# --------------------------------------------------------------------------- #


def clip_passage(text: str, budget: int) -> str:
    """Shorten a passage to ``budget`` characters, at a word boundary where one exists.

    Cutting mid-word produces a fragment that reads as a transcription error in a
    citation, so the clip is pulled back to the last space when that space is not
    absurdly early. The ellipsis is added so a reader can see the passage
    continues — an excerpt that ends flush looks complete and is not.
    """
    if budget <= 0 or len(text) <= budget:
        return text

    clipped = text[:budget]
    boundary = clipped.rfind(" ")
    # Only honoured when the boundary keeps most of the budget: an Arabic or
    # Chinese passage may have no space at all in the window, and pulling back to
    # character 3 would throw the excerpt away to avoid splitting a word.
    if boundary > budget * 3 // 4:
        clipped = clipped[:boundary]
    return clipped.rstrip() + "…"


def fit_to_budget(
    lengths: Sequence[int],
    *,
    budget: int,
    max_passage: int,
) -> list[int]:
    """Decide how many characters of each passage fit inside the context budget.

    Returns one entry per input, in the same order: the number of characters to
    keep, or ``0`` for a passage that does not fit at all. Pure arithmetic over
    *lengths* rather than over the passages themselves, so the rule can be
    unit-tested without inventing legal prose, and so the caller — which owns
    the passages — is the only thing that ever touches their text.

    The rule, and why each part of it:

    * every passage is first capped at ``max_passage``, because one enormous
      chunk must not consume a budget that five passages would have used better
      — retrieval quality comes from *breadth* of evidence;
    * passages are consumed in the order given, which is relevance order, so the
      passage dropped by an exhausted budget is always the least relevant one;
    * a passage that would overflow is **truncated** if at least
      :data:`MIN_PASSAGE_CHARACTERS` remain, and **dropped** otherwise — a
      50-character tail is not evidence, and quoting it as though it were is the
      failure mode this whole feature is built to avoid;
    * and once a passage is dropped, the ones after it are dropped too, rather
      than scanning ahead for a shorter passage that would still fit. Skipping
      forward would silently reorder the evidence by length instead of by
      relevance.
    """
    remaining = max(0, budget)
    kept: list[int] = []

    for length in lengths:
        allowance = min(length, max(0, max_passage))
        if allowance <= 0:
            kept.append(0)
            continue

        if allowance <= remaining:
            kept.append(allowance)
            remaining -= allowance
            continue

        if remaining >= MIN_PASSAGE_CHARACTERS:
            kept.append(remaining)
            remaining = 0
            continue

        kept.append(0)
        remaining = 0

    return kept


def cited_markers(answer: str) -> set[int]:
    """Every citation marker the model actually wrote, as integers.

    Used to report which sources an answer *leaned on*, distinct from which
    sources it was *given*. Both are reported: the second is what makes the
    spec's "include citations whenever supporting context exists" true even when
    a model forgets to cite, and the first is what makes the citation list
    honest about which references the sentence in front of the reader rests on.
    """
    return {int(match) for match in CITATION_MARKER_PATTERN.findall(answer)}


def unknown_markers(answer: str, *, source_count: int) -> set[int]:
    """Citation markers that point at no source.

    A model that writes ``[9]`` when it was handed six passages has invented a
    reference. The answer is not discarded for it — the prose may be perfectly
    grounded and only the numbering wrong — but the marker is never resolved to a
    source, and it is counted, because a rising count is the earliest signal that
    a prompt or a model change has degraded grounding.
    """
    return {marker for marker in cited_markers(answer) if marker < 1 or marker > source_count}


def is_insufficient_evidence(answer: str) -> bool:
    """Whether the model reported that the context does not support an answer.

    Matched on the sentinel the prompt names, case-insensitively and anywhere in
    the reply: a model that prefixes it with "Answer: " or wraps it in quotes has
    still said the same thing, and treating that as prose would show the user a
    bare identifier.
    """
    return INSUFFICIENT_EVIDENCE_MARKER.casefold() in answer.casefold()


def sentinel_prefix_pending(accumulated: str) -> bool:
    """Whether the text produced so far could still turn out to be the refusal alone.

    Used only when an answer is **streamed**. A streamed answer is emitted as it
    arrives, but the platform replaces the refusal sentinel with its own sentence
    — so a naive relay would show a reader the characters ``INSUFFICIENT_EVID…``
    and then swap them for a paragraph of French, which looks like a malfunction
    and briefly exposes an internal token.

    So a stream withholds its fragments while the accumulated text is still a
    *prefix* of the sentinel, and releases them the moment it cannot be one. The
    cost is bounded and tiny: at most the sentinel's length of text is held back,
    and only until the model writes a character that diverges from it — which,
    for any real answer, is the first one.

    **Both directions are withheld**, and the second is the one a naive
    implementation gets wrong: text that is still *becoming* the sentinel
    (``INSUFFICIENT_EVID``) and text that *already is* it, with or without
    anything after (``INSUFFICIENT_EVIDENCE.``). Releasing the second would emit
    the token in full the instant it completed, which is precisely the flash this
    exists to prevent.

    What it does **not** catch is a model that prefixes the sentinel with prose —
    ``Answer: INSUFFICIENT_EVIDENCE`` — because by the time the token appears the
    prose has already been released. :func:`is_insufficient_evidence` still
    recognises that reply and the final answer is still replaced; the reader
    briefly saw ``Answer: ``. That is the honest limit of a guard that cannot see
    the future, and the prompt asks for the sentinel *alone*.

    Recognised the same way :func:`is_insufficient_evidence` recognises the
    finished reply: case-insensitively, and tolerant of the opening quotation
    mark a model sometimes leads with.
    """
    trimmed = accumulated.lstrip().lstrip("\"'").casefold()
    marker = INSUFFICIENT_EVIDENCE_MARKER.casefold()
    return marker.startswith(trimmed) or trimmed.startswith(marker)


def is_usable_answer(answer: str) -> bool:
    """Whether a generated answer carries anything to show a user.

    Deliberately minimal — whitespace and nothing else is the only rejection.
    Judging an answer's *quality* here would be the platform second-guessing the
    model, which is not this module's business; judging that there is no answer
    at all is.
    """
    return bool(answer.strip())


def total_characters(values: Iterable[int]) -> int:
    """Sum of a set of passage lengths. Named so call sites read as intent."""
    return sum(values)


__all__ = [
    "CITATION_MARKER_PATTERN",
    "FAILURE_MESSAGES",
    "INSUFFICIENT_EVIDENCE_MARKER",
    "LANGUAGE_NAMES",
    "MIN_PASSAGE_CHARACTERS",
    "MIN_QUESTION_LENGTH",
    "NO_EVIDENCE_MESSAGES",
    "SUPPORTED_ANSWER_LANGUAGES",
    "RagFailureCode",
    "cited_markers",
    "clip_passage",
    "failure_message",
    "fit_to_budget",
    "is_answerable_question",
    "is_insufficient_evidence",
    "is_usable_answer",
    "language_name",
    "loggable_question",
    "no_evidence_message",
    "normalize_question",
    "question_fingerprint",
    "resolve_answer_language",
    "sentinel_prefix_pending",
    "total_characters",
    "unknown_markers",
]
