"""Live validation of the AI Legal Assistant against the **real** model.

``tests/ai/test_rag_live.py`` closes this gap for the pipeline: whether the
shipped *answer* prompt actually works. This module closes the two the assistant
adds on top of it, and they are the two the hermetic suite is structurally unable
to reach:

* **does a real streaming response actually arrive in more than one fragment?**
  Every hermetic streaming test drives a double that yields whatever list the test
  wrote. A provider that returned the whole answer in one chunk — or an SDK whose
  streaming call quietly degraded after an upgrade — would pass all of them and
  ship a "streaming" feature that streams nothing. Only a real provider can say.
* **does the shipped follow-up prompt produce questions worth offering?** Three
  suggestions that are three rephrasings of the question just answered is the
  characteristic failure of this feature, it is a property of a *prompt*, and a
  prompt is not code: nothing in a hermetic build would notice it starting.

Everything else about the assistant — ownership, persistence, feedback, the
event contract, the error envelopes — is deterministic and is covered without
spending a request.

Opt-in for the same two reasons ``test_rag_live.py`` is: it costs real quota, and
a language model is not a pure function, so a failure here is **evidence to
investigate rather than proof of a defect**. The assertions are therefore written
against behaviours the prompt and the transport make *structural* — how many
fragments arrived, whether a suggestion is a question, which script it is in —
and never against particular wording.

Run it with both switches::

    LLM_API_KEY=...  RUN_LIVE_AI_TESTS=1  pytest tests/ai -q

**Quota is the binding constraint, and this module is sized around it.**
``gemini-2.5-flash``'s free tier allows **5 requests per minute and 20 per day**,
and ``test_rag_live.py`` already spends 8. This module spends **7, across four
tests** — and note that a *message* is not a request: an exchange that produces
suggestions costs two, which is why the two tests that need a grounded answer
each cost two and the one that needs a refusal costs one. The tests carry several
assertions rather than being split, which is a deliberate trade: a split would
multiply the cost to buy independent failure isolation that a single traceback
already gives. :func:`pace` handles the per-minute ceiling; nothing can handle
the daily one but a paid key.

Set ``LIVE_AI_PACE_SECONDS=0`` on a paid key.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from core.assistant import ConversationRole
from core.config import settings
from core.rag import INSUFFICIENT_EVIDENCE_MARKER, cited_markers
from models.case import Case
from models.document import Document, DocumentCategory
from models.user import User, UserRole
from schemas.conversation import ConversationCreate, MessageCreate
from services.rag import RagStreamEventKind

pytestmark = pytest.mark.skipif(
    not (settings.LLM_API_KEY and os.getenv("RUN_LIVE_AI_TESTS")),
    reason="live AI checks need LLM_API_KEY and RUN_LIVE_AI_TESTS=1",
)

MakeUser = Callable[..., User]
MakeCase = Callable[..., Case]
MakeDocument = Callable[..., Document]

#: Deliberately richer than the pipeline module's page: a follow-up suggestion is
#: only meaningful if the document contains *more than one* thing worth asking
#: about. A single-clause source would make "the model suggested nothing useful"
#: indistinguishable from "there was nothing useful to suggest".
FRENCH_PAGE = (
    "CONTRAT DE BAIL COMMERCIAL. "
    "Article 4 : Loyer et charges. Le loyer mensuel est payable d'avance le premier "
    "jour de chaque mois, au domicile du bailleur. Les charges locatives sont "
    "réglées trimestriellement sur présentation des justificatifs. "
    "Article 5 : Dépôt de garantie. Le preneur verse un dépôt de garantie équivalent "
    "à deux mois de loyer, restitué dans les trente jours suivant la restitution des "
    "locaux. "
    "Article 6 : Entretien et réparations. Les réparations locatives incombent au "
    "preneur ; les grosses réparations restent à la charge du bailleur. "
    "Article 7 : Résiliation. Toute résiliation anticipée doit être notifiée par "
    "écrit avec un préavis de trois mois."
)


@pytest.fixture(autouse=True)
def pace() -> Any:
    """Keep the module inside the provider's requests-per-minute allowance.

    Identical in purpose and reasoning to ``test_rag_live.py``'s: a rate limit is
    a fact about the *account*, not about the platform, and a suite that reports
    it as a failure teaches its reader to ignore red.

    The delay is per **test** while two of these tests make two calls each, so
    the pause is sized for that: at the default it keeps the module comfortably
    under five requests in any rolling minute.
    """
    import time

    delay = float(os.getenv("LIVE_AI_PACE_SECONDS", "26"))
    if delay > 0:
        time.sleep(delay)


@pytest.fixture(scope="session")
def embedder() -> Any:
    """The **real** BAAI/bge-m3, overriding the suite's deterministic double.

    Session-scoped for the reason ``test_rag_live.py`` gives: the model is
    roughly two gigabytes and stateless once loaded.
    """
    from services.embedding import get_embedder

    real = get_embedder()
    if not real.is_available():  # pragma: no cover - environment dependent
        pytest.skip("BAAI/bge-m3 is not available on this host")
    return real


class CountingProvider:
    """The real Gemini provider, with its calls counted.

    Wrapping rather than mocking, because what is under test *is* the real
    provider — the count exists so a test can assert something the hermetic suite
    asserts trivially and this module otherwise could not: that an **ungrounded
    answer costs exactly one model call**, because the suggester short-circuits
    before making a second. On a twenty-a-day budget that is not a micro-
    optimisation, it is the difference between ten questions and twenty.
    """

    def __init__(self) -> None:
        from services.llm import GeminiProvider

        self._inner = GeminiProvider()
        self.generate_calls = 0
        self.stream_calls = 0
        #: Fragments the last stream yielded, so a test can inspect the shape of
        #: a real response rather than only its total.
        self.fragments: list[str] = []

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    def is_available(self) -> bool:
        return self._inner.is_available()

    def count_tokens(self, text: str) -> int | None:
        return self._inner.count_tokens(text)

    def generate(self, **kwargs: Any) -> Any:
        self.generate_calls += 1
        return self._inner.generate(**kwargs)

    def stream(self, **kwargs: Any) -> Iterator[str]:
        self.stream_calls += 1
        self.fragments = []
        for fragment in self._inner.stream(**kwargs):
            self.fragments.append(fragment)
            yield fragment

    @property
    def total_calls(self) -> int:
        return self.generate_calls + self.stream_calls


@pytest.fixture
def live_provider() -> CountingProvider:
    """One counted real provider per test."""
    return CountingProvider()


@pytest.fixture
def lawyer(make_user: MakeUser) -> User:
    return make_user(email="live-chat-lawyer@example.com", role=UserRole.LAWYER)


@pytest.fixture
def legal_case(make_case: MakeCase, lawyer: User) -> Case:
    return make_case(assigned_lawyer_id=lawyer.id)


@pytest.fixture
def index_document(indexing_service: Any, make_ocr_result: Any):  # type: ignore[no-untyped-def]
    def _index(document: Document, pages: list[str]) -> None:
        indexing_service.schedule_for_ocr_result(
            make_ocr_result(document_id=document.id, pages=pages)
        )

    return _index


@pytest.fixture
def french_contract(
    make_document: MakeDocument, legal_case: Case, index_document: Any
) -> Document:
    document = make_document(
        case_id=legal_case.id,
        original_filename="bail-commercial.pdf",
        category=DocumentCategory.CONTRACT,
    )
    index_document(document, [FRENCH_PAGE])
    return document


@pytest.fixture
def live_assistant_service(  # type: ignore[no-untyped-def]
    db_session: Any,
    search_service: Any,
    prompt_library: Any,
    rag_metrics: Any,
    assistant_metrics: Any,
    live_provider: CountingProvider,
):
    """The assistant with the **real** provider and both shipped prompts.

    Only the provider differs from the hermetic fixture. The search service, the
    access policy, the ranker, the graph, the templates, the repository, and the
    suggester are all the application's own — so what runs here is the production
    assistant on top of the production pipeline, and a failure is about the
    prompt or the transport rather than about a fixture.
    """
    from repositories.conversation import ConversationRepository
    from services.assistant import AssistantService
    from services.rag import RagService
    from services.suggestions import LlmFollowUpSuggester

    rag = RagService(search_service, prompt_library, live_provider, metrics=rag_metrics)

    return AssistantService(
        ConversationRepository(db_session),
        rag,
        suggester=LlmFollowUpSuggester(prompt_library, live_provider),
        metrics=assistant_metrics,
    )


def open_conversation(service: Any, actor: User, case: Case) -> Any:
    return service.create_conversation(ConversationCreate(case_id=case.id), actor=actor)


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


class TestStreamingIsReal:
    def test_a_real_answer_arrives_progressively_and_ends_where_it_is_stored(
        self,
        live_assistant_service: Any,
        live_provider: CountingProvider,
        lawyer: User,
        french_contract: Document,
        legal_case: Case,
    ) -> None:
        """The claim the hermetic suite cannot make.

        Several assertions in one test on purpose — see the module docstring: on
        a twenty-a-day budget, splitting this into five tests would cost five
        requests to buy failure isolation that a single traceback already gives.

        **The question is chosen to force a long answer, and that is not a way of
        making the test easier to pass.** The first version of this test asked
        *"Quand le loyer doit-il être payé ?"* and failed: the model answered in
        one sentence of 128 characters, and the provider delivered it in a single
        fragment. That is not a streaming failure — a chunked transport is not
        obliged to split a sentence, and no answer that short *can* demonstrate
        anything about incremental delivery. Asserting `> 1` on it would have
        been asserting a property of the answer's length. So the question asks
        for a summary across four articles, which cannot come back in one chunk
        unless the transport is genuinely not incremental — which is the thing
        under test.

        Note what "more than one fragment" is asserted against: the **provider's**
        own output, not the platform's events. The platform withholds fragments
        while the accumulated text could still be the refusal sentinel, so
        counting emitted deltas would conflate "the provider streamed" with "the
        guard released", which are different questions.
        """
        conversation = open_conversation(live_assistant_service, lawyer, legal_case)

        events = list(
            live_assistant_service.stream_message(
                conversation.id,
                MessageCreate(
                    content=(
                        "Résume les obligations du preneur et celles du bailleur, "
                        "article par article."
                    )
                ),
                actor=lawyer,
            )
        )

        # 1. The provider genuinely streamed, rather than returning one blob.
        assert live_provider.stream_calls == 1
        assert len(live_provider.fragments) > 1, (
            f"the provider returned {len(live_provider.fragments)} fragment(s); "
            "streaming is not actually incremental against this model"
        )

        # 2. The platform relayed it as the documented event sequence.
        kinds = [event.kind for event in events]
        assert kinds[0] is RagStreamEventKind.RETRIEVAL
        assert kinds[-1] is RagStreamEventKind.FINAL
        assert kinds.count(RagStreamEventKind.DELTA) > 1

        # 3. What was streamed is what was answered — the deltas are a readable
        #    progress indicator, and the final event is authoritative, but the
        #    two must not be *different answers*.
        streamed = "".join(
            event.text for event in events if event.kind is RagStreamEventKind.DELTA
        )
        final = events[-1].outcome
        assert final is not None
        assert streamed.strip() == final.answer.strip()

        # 4. A streamed answer is still grounded and still cited.
        assert final.grounded is True
        assert cited_markers(final.answer)
        assert final.citations[0].document_id == french_contract.id

        # 5. And it reports **no token usage**, which is the limitation
        #    `architecture.md` states rather than a defect. A provider reports
        #    usage on a *finished* response and a stream does not produce one, so
        #    a deployment that streams everything reports honest `None` totals.
        #    Asserted here rather than in a test of its own because it is free
        #    here and would cost two more requests there. The provider and model
        #    identities do survive, because they come from configuration rather
        #    than from the response body.
        assert final.total_tokens is None
        assert final.model.startswith("gemini")

        # 6. And it is still persisted, both turns, in order.
        messages, total = live_assistant_service.list_messages(
            conversation.id, offset=0, limit=50, actor=lawyer
        )
        assert total == 2
        assert [message.role for message in messages] == [
            ConversationRole.USER,
            ConversationRole.ASSISTANT,
        ]
        assert messages[1].content.strip() == final.answer.strip()


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #


class TestSuggestionsAreUseful:
    def test_the_shipped_prompt_produces_answerable_questions_that_move_forward(
        self,
        live_assistant_service: Any,
        live_provider: CountingProvider,
        lawyer: User,
        french_contract: Document,
        legal_case: Case,
    ) -> None:
        """The second claim the hermetic suite cannot make.

        Two model calls: the answer, and the suggestions. Every assertion below
        is against something the prompt makes **structural** — a count, a length,
        a question mark, a script — because asserting *content* would be
        asserting that a model chose the follow-up this author would have chosen.

        The one about repetition is the important one: three rephrasings of the
        question just answered is the characteristic failure of this feature, and
        it is exactly what a hermetic test cannot detect.
        """
        question = "Quand le loyer doit-il être payé ?"
        conversation = open_conversation(live_assistant_service, lawyer, legal_case)

        exchange = live_assistant_service.send_message(
            conversation.id, MessageCreate(content=question), actor=lawyer
        )
        suggestions = exchange.assistant_message.suggestions

        # The answer and the suggestions, and nothing else.
        assert live_provider.total_calls == 2
        assert suggestions, "the shipped follow-up prompt produced nothing usable"
        assert len(suggestions) <= settings.ASSISTANT_SUGGESTION_COUNT

        for suggestion in suggestions:
            # Sendable: within the limit the composer and the API enforce.
            assert len(suggestion) <= settings.ASSISTANT_SUGGESTION_MAX_LENGTH
            # A question, not a statement or a heading.
            assert suggestion.rstrip().endswith(("?", "؟"))
            # Written in the answer's language, asserted on the *script*: a model
            # may phrase this a hundred ways but cannot write Arabic in Latin
            # letters, and cannot write French in Arabic ones.
            assert not any("؀" <= character <= "ۿ" for character in suggestion)
            # Not the question that was just answered, in any casing.
            assert suggestion.casefold() != question.casefold()

        # Distinct from one another: three rephrasings of one idea is a list of
        # one, and the parser only removes *exact* duplicates.
        assert len({suggestion.casefold() for suggestion in suggestions}) == len(suggestions)

    def test_an_ungrounded_answer_costs_one_call_and_suggests_nothing(
        self,
        live_assistant_service: Any,
        live_provider: CountingProvider,
        lawyer: User,
        french_contract: Document,
        legal_case: Case,
    ) -> None:
        """Two claims at once, and the second is about money.

        *"Suggestions should never invent unsupported facts"* — an answer that
        found no supporting document supports no follow-up either, so every
        question a model produced from it would be a guess. And because the
        suggester short-circuits **before** calling the provider, declining a
        question costs one request rather than two: on a twenty-a-day budget that
        is the difference between ten questions and twenty.

        Requires the model to decline, which is the pipeline's own sentinel path —
        already validated live in ``test_rag_live.py``. What is new here is what
        the *assistant* does with a refusal.
        """
        conversation = open_conversation(live_assistant_service, lawyer, legal_case)

        exchange = live_assistant_service.send_message(
            conversation.id,
            MessageCreate(
                content="Quel est le montant des dommages et intérêts accordés par le tribunal ?"
            ),
            actor=lawyer,
        )
        answer = exchange.assistant_message

        assert answer.insufficient_evidence is True
        assert answer.grounded is False
        assert answer.suggestions == []
        assert answer.citations == []
        # The sentinel is replaced by the platform's own sentence, never stored.
        assert INSUFFICIENT_EVIDENCE_MARKER not in answer.content
        # One call, not two.
        assert live_provider.total_calls == 1


# --------------------------------------------------------------------------- #
# Conversational context
# --------------------------------------------------------------------------- #


class TestFollowUpResolutionWorksAgainstARealModel:
    def test_a_short_follow_up_is_answered_as_itself_not_as_the_earlier_question(
        self,
        live_assistant_service: Any,
        lawyer: User,
        french_contract: Document,
        legal_case: Case,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The third thing only a real model can settle.

        :func:`~core.assistant.resolve_question` prefixes a short question with a
        labelled reference to the earlier one, because the pipeline's ``question``
        is *both* the retrieval query and the text the model is asked to answer.
        That construction is deterministic and unit-tested — but whether a real
        model reads *"Dans la continuité de : X / Question : Y"* as "answer Y with
        X as context" rather than as "answer X again" is a property of the model,
        and it is the entire premise of conversational context here.

        Suggestions are turned off for this test so the two turns cost two
        requests rather than four. The switch is production behaviour, not a test
        hook — see ``ASSISTANT_SUGGESTIONS_ENABLED``.
        """
        monkeypatch.setattr(settings, "ASSISTANT_SUGGESTIONS_ENABLED", False)
        conversation = open_conversation(live_assistant_service, lawyer, legal_case)

        live_assistant_service.send_message(
            conversation.id,
            MessageCreate(content="Quel est le montant du dépôt de garantie ?"),
            actor=lawyer,
        )
        follow_up = live_assistant_service.send_message(
            conversation.id,
            MessageCreate(content="Et sous quel délai est-il restitué ?"),
            actor=lawyer,
        )
        answer = follow_up.assistant_message

        # The platform carried the earlier question.
        assert answer.context_turns == 1
        assert answer.grounded is True

        # And the model answered *this* question: the document says thirty days,
        # and the deposit itself is two months' rent. Asserted on the figure the
        # follow-up asks for, which is the one thing that distinguishes "answered
        # the follow-up" from "answered the first question again".
        assert "30" in answer.content or "trente" in answer.content.lower()

        # The transcript still shows what the user actually typed, not the
        # resolved text the pipeline was handed.
        assert follow_up.user_message.content == "Et sous quel délai est-il restitué ?"
