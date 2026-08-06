"""Unit tests for :mod:`services.rag_graph`, the LangGraph workflow.

The graph owns the *order*, and nothing else — so these tests drive it with a
recorder that implements :class:`~services.rag_graph.RagNodes` and does nothing
but write down which node ran. That is the whole point of the split: the order
can be asserted without a search service, a prompt, a provider, or a database,
and the nodes can be asserted without a graph.

Three claims are worth pinning here, because a later feature adding a node is
exactly when one of them would quietly stop being true:

* the spec's six steps run, in the spec's order;
* retrieval that finds nothing **skips the model entirely** — the spec's "do not
  fabricate answers", and its "avoid duplicate LLM calls" taken to its limit;
* both branches converge on one formatting node, so how an answer is shaped is
  written once.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.rag_graph import (
    ASSEMBLE,
    FORMAT,
    GENERATE,
    NO_EVIDENCE,
    RETRIEVE,
    VALIDATE,
    VERIFY,
    WORKFLOW_NODES,
    RagNodes,
    RagState,
    build_rag_graph,
    route_after_retrieval,
)


class RecordingNodes:
    """A :class:`RagNodes` that records the order it was called in."""

    def __init__(self, *, passages: list[Any] | None = None) -> None:
        self.visited: list[str] = []
        self._passages = passages if passages is not None else ["a passage"]

    def validate_request(self, state: RagState) -> RagState:
        self.visited.append(VALIDATE)
        return RagState(question="Quand le loyer est-il du ?", language="fr", top_k=8)

    def retrieve_context(self, state: RagState) -> RagState:
        self.visited.append(RETRIEVE)
        return RagState(passages=list(self._passages), retrieval_ms=12)  # type: ignore[typeddict-item]

    def assemble_prompt(self, state: RagState) -> RagState:
        self.visited.append(ASSEMBLE)
        return RagState(context_characters=100, context_truncated=False)

    def invoke_model(self, state: RagState) -> RagState:
        self.visited.append(GENERATE)
        return RagState(generation_ms=340)

    def verify_response(self, state: RagState) -> RagState:
        self.visited.append(VERIFY)
        return RagState(answer="Le loyer est payable le 5 [1].", grounded=True, insufficient=False)

    def format_output(self, state: RagState) -> RagState:
        self.visited.append(FORMAT)
        return RagState(answer=state.get("answer", "").strip())

    def report_no_evidence(self, state: RagState) -> RagState:
        self.visited.append(NO_EVIDENCE)
        return RagState(answer="Rien trouvé.", grounded=False, insufficient=True)


class TestRouting:
    def test_passages_route_to_prompt_assembly(self) -> None:
        assert route_after_retrieval(RagState(passages=["p"])) == ASSEMBLE  # type: ignore[typeddict-item]

    def test_no_passages_route_around_the_model(self) -> None:
        assert route_after_retrieval(RagState(passages=[])) == NO_EVIDENCE

    def test_an_absent_key_routes_around_the_model(self) -> None:
        """Defence in depth: a retrieval node that failed to set the key must not
        fall through into generation with an empty context."""
        assert route_after_retrieval(RagState()) == NO_EVIDENCE


class TestWorkflow:
    def test_the_graph_runs_the_specs_six_steps_in_order(self) -> None:
        nodes = RecordingNodes()

        build_rag_graph(nodes).invoke(RagState())

        assert nodes.visited == [VALIDATE, RETRIEVE, ASSEMBLE, GENERATE, VERIFY, FORMAT]

    def test_retrieval_always_precedes_generation(self) -> None:
        """The whole of 'retrieval-augmented': nothing is generated unretrieved."""
        nodes = RecordingNodes()

        build_rag_graph(nodes).invoke(RagState())

        assert nodes.visited.index(RETRIEVE) < nodes.visited.index(GENERATE)

    def test_an_empty_retrieval_skips_the_model_entirely(self) -> None:
        nodes = RecordingNodes(passages=[])

        build_rag_graph(nodes).invoke(RagState())

        assert nodes.visited == [VALIDATE, RETRIEVE, NO_EVIDENCE, FORMAT]
        assert GENERATE not in nodes.visited
        assert ASSEMBLE not in nodes.visited

    def test_both_branches_converge_on_the_formatting_node(self) -> None:
        for passages in ([], ["a passage"]):
            nodes = RecordingNodes(passages=passages)
            build_rag_graph(nodes).invoke(RagState())
            assert nodes.visited[-1] == FORMAT

    def test_the_final_state_carries_what_the_nodes_produced(self) -> None:
        final = build_rag_graph(RecordingNodes()).invoke(RagState())

        assert final["answer"] == "Le loyer est payable le 5 [1]."
        assert final["grounded"] is True
        assert final["retrieval_ms"] == 12
        assert final["generation_ms"] == 340

    def test_a_node_failure_propagates_rather_than_being_swallowed(self) -> None:
        class Failing(RecordingNodes):
            def invoke_model(self, state: RagState) -> RagState:
                raise RuntimeError("provider down")

        with pytest.raises(RuntimeError, match="provider down"):
            build_rag_graph(Failing()).invoke(RagState())


class TestShape:
    def test_the_workflow_lists_every_node(self) -> None:
        assert set(WORKFLOW_NODES) == {
            VALIDATE,
            RETRIEVE,
            ASSEMBLE,
            GENERATE,
            VERIFY,
            FORMAT,
            NO_EVIDENCE,
        }

    def test_the_compiled_graph_contains_exactly_those_nodes(self) -> None:
        """So 'the pipeline still retrieves before generating' stays true after
        somebody adds an eighth node."""
        compiled = build_rag_graph(RecordingNodes())
        drawn = set(compiled.get_graph().nodes) - {"__start__", "__end__"}

        assert drawn == set(WORKFLOW_NODES)

    def test_the_service_satisfies_the_node_protocol(self, rag_service: Any) -> None:
        checked: RagNodes = rag_service
        assert callable(checked.retrieve_context)

    def test_the_state_has_no_conversation(self) -> None:
        """`ai-architecture.md`: the RAG pipeline must never manage conversations,
        and this is the file where that would first have been broken."""
        keys = set(RagState.__annotations__)

        assert not {"conversation", "conversation_id", "messages", "history", "session"} & keys
