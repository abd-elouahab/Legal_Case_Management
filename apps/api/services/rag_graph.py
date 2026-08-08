"""The RAG workflow, as a LangGraph state graph.

``12-rag-pipeline.md`` names LangGraph as the orchestrator and specifies the
minimum workflow:

.. code-block:: text

    Validate input → Retrieve context → Assemble prompt → Invoke LLM
                   → Validate response → Format output

and adds one requirement that shapes the whole module: *"The workflow should
support future branching without redesign."*

**This module owns the order, and nothing else.** Every node is a call onto a
:class:`RagNodes` implementation — :class:`~services.rag.RagService` — so the
graph is a readable statement of *what happens when*, with no retrieval, no
prompt, no provider, and no citation logic inside it. That split is what makes
the two independently testable: the graph can be driven with a stub that records
the order it was called in, and the service's nodes can be called directly with
no graph at all.

**The branch is real, not decorative.** After retrieval the graph forks: passages
found go on to prompt assembly and generation, and *no passages at all* goes
straight to the no-evidence node, skipping the model entirely. That is
simultaneously the spec's "No Evidence Handling" ("do not fabricate answers"),
its "Performance" requirement ("avoid duplicate LLM calls", and the cheapest call
is the one not made), and the demonstration that the branching the spec asks to
be possible actually is — a graph whose only shape is a straight line proves
nothing about supporting one that is not.

**Where the future nodes go**, so the next feature does not have to guess:

* *conversation memory* — a node before ``retrieve``, rewriting a follow-up
  question into a standalone one, which is why the state carries the question as
  a value rather than reading it from the request each time;
* *tool calling* / *planner agents* — a branch out of ``generate`` back into
  ``retrieve``, which the conditional edge already establishes as a legal shape;
* *multiple retrieval strategies* — a branch at ``retrieve`` selecting between
  nodes, with the state's ``passages`` key as the common output;
* *report generation* — its own graph reusing these nodes, not a branch here.

**Nothing here manages a conversation**, and the state deliberately has no key
for one: ``ai-architecture.md`` states that the RAG pipeline must never manage
conversations, and this is the file where that would first have been broken.
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

import structlog

from models.user import User
from schemas.rag import RagRequest
from schemas.search import SearchResultRead
from services.llm import LLMCompletion
from services.prompts import RenderedPrompt

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------- #
# Node names
#
# Constants rather than string literals at each edge: a typo in an edge name is a
# graph that compiles and then routes to a node that does not exist, which
# LangGraph reports at build time but only for the *target* — a mistyped source
# is silently an orphan.
# --------------------------------------------------------------------------- #

VALIDATE = "validate"
RETRIEVE = "retrieve"
ASSEMBLE = "assemble"
GENERATE = "generate"
VERIFY = "verify"
FORMAT = "format"
NO_EVIDENCE = "no_evidence"


class RagState(TypedDict, total=False):
    """Everything one run of the pipeline carries between its nodes.

    ``total=False`` because the state is built up: ``question`` exists from the
    start, ``passages`` only after retrieval, ``completion`` only after
    generation — and the no-evidence branch never produces the last two at all.

    Keys are written by exactly one node each, which is why the graph needs no
    reducers: there is no key two nodes both append to, and therefore no merge
    rule anyone has to remember.
    """

    # --- set by the caller ---
    #: The validated request, kept whole so a future node can read a filter or a
    #: limit without the state having to grow a key per field.
    request: RagRequest
    #: Who is asking. Present because retrieval is scoped to them — it is the
    #: single most important value in this dictionary, and the reason the graph
    #: cannot be run "for the platform" with no actor.
    actor: User
    #: :func:`time.monotonic` reading when the run began, and the instant it must
    #: finish by. Both live in the state rather than on the service, because a
    #: service instance is shared by every request that resolves the same
    #: dependency and a per-run value stored on it would be a race between two
    #: questions. Monotonic rather than wall-clock, so a clock adjustment
    #: mid-request cannot produce a negative latency or a deadline in the past.
    started: float
    deadline: float

    #: Ceiling on the model's output for this run, in provider tokens, or absent
    #: for the deployment's default.
    #:
    #: On the state rather than on the request schema, deliberately: a token
    #: ceiling is a *provider* concept, and ``12-rag-pipeline.md`` keeps the
    #: platform's budgets in characters precisely so the wire contract does not
    #: acquire one. What it is for is a caller that knows its output is longer
    #: than a chat reply — the Report Generation Agent, whose sections are prose
    #: rather than an answer, and which discovered on a live run that a reasoning
    #: model spends most of a small budget thinking before emitting a word.
    max_output_tokens: int

    # --- validate ---
    question: str
    language: str
    top_k: int

    # --- retrieve ---
    passages: list[SearchResultRead]
    retrieval_ms: int

    # --- assemble ---
    prompt: RenderedPrompt
    #: The passages that actually fitted the context budget, with their text
    #: clipped to what was placed in the prompt. Distinct from ``passages``,
    #: because a citation must quote what the model *read*, not what was
    #: retrieved and then dropped.
    context: list[SearchResultRead]
    context_characters: int
    context_truncated: bool

    # --- generate ---
    completion: LLMCompletion
    generation_ms: int

    # --- verify ---
    answer: str
    grounded: bool
    insufficient: bool
    cited: set[int]


class RagNodes(Protocol):
    """What the graph requires of the service beneath it.

    Seven members, one per node, and each takes and returns the state. The
    protocol exists so this module depends on a *shape* rather than on
    :class:`~services.rag.RagService` — which is what lets the graph be tested
    with a recorder, and what will let a report-generation graph reuse these same
    nodes without importing the question-answering service.
    """

    def validate_request(self, state: RagState) -> RagState:
        """Normalise the question, settle the answer language, and refuse a non-question."""
        ...

    def retrieve_context(self, state: RagState) -> RagState:
        """Retrieve the passages an answer may be built from, scoped to the actor."""
        ...

    def assemble_prompt(self, state: RagState) -> RagState:
        """Fit the passages to the context budget and render the prompt."""
        ...

    def invoke_model(self, state: RagState) -> RagState:
        """Call the configured provider once."""
        ...

    def verify_response(self, state: RagState) -> RagState:
        """Reject an unusable answer, and decide whether it is grounded."""
        ...

    def format_output(self, state: RagState) -> RagState:
        """Shape the final answer text. The one node both branches end at."""
        ...

    def report_no_evidence(self, state: RagState) -> RagState:
        """Answer without calling a model, because nothing was retrieved."""
        ...


def route_after_retrieval(state: RagState) -> str:
    """Choose the branch to take once retrieval has run.

    The whole of the decision is *did anything come back*. It is a function
    rather than a lambda so it can be unit-tested on its own and so the reason
    lives next to it: with no passages there is nothing to ground an answer in,
    and asking a language model to answer anyway is asking it to answer from its
    training data — which for a legal platform is the one outcome that must never
    happen.
    """
    return ASSEMBLE if state.get("passages") else NO_EVIDENCE


def build_rag_graph(nodes: RagNodes) -> Any:
    """Compile the workflow.

    Returns LangGraph's compiled graph, typed loosely on purpose: the library's
    generic parameters move between versions, the pipeline calls exactly one
    method on the result (``invoke``), and pinning a generic here would make a
    library upgrade a change to this feature's type signatures.

    Compiled once per service instance rather than per request — compilation
    validates every edge and builds the executor, and neither depends on the
    question being asked.
    """
    from langgraph.graph import END, START, StateGraph

    graph: Any = StateGraph(RagState)

    graph.add_node(VALIDATE, nodes.validate_request)
    graph.add_node(RETRIEVE, nodes.retrieve_context)
    graph.add_node(ASSEMBLE, nodes.assemble_prompt)
    graph.add_node(GENERATE, nodes.invoke_model)
    graph.add_node(VERIFY, nodes.verify_response)
    graph.add_node(FORMAT, nodes.format_output)
    graph.add_node(NO_EVIDENCE, nodes.report_no_evidence)

    graph.add_edge(START, VALIDATE)
    graph.add_edge(VALIDATE, RETRIEVE)
    graph.add_conditional_edges(
        RETRIEVE,
        route_after_retrieval,
        {ASSEMBLE: ASSEMBLE, NO_EVIDENCE: NO_EVIDENCE},
    )
    graph.add_edge(ASSEMBLE, GENERATE)
    graph.add_edge(GENERATE, VERIFY)
    graph.add_edge(VERIFY, FORMAT)
    # Both branches converge here, so "how an answer is shaped" is written once
    # and a future branch gets it for free by pointing at this node.
    graph.add_edge(NO_EVIDENCE, FORMAT)
    graph.add_edge(FORMAT, END)

    return graph.compile()


#: Every node in the workflow, in the order the spec lists them.
#:
#: Exported so a test can assert that the compiled graph contains exactly these —
#: which is how "the pipeline still does retrieval before generation" stays true
#: after somebody adds an eighth node.
WORKFLOW_NODES: tuple[str, ...] = (
    VALIDATE,
    RETRIEVE,
    ASSEMBLE,
    GENERATE,
    VERIFY,
    FORMAT,
    NO_EVIDENCE,
)


__all__ = [
    "ASSEMBLE",
    "FORMAT",
    "GENERATE",
    "NO_EVIDENCE",
    "RETRIEVE",
    "VALIDATE",
    "VERIFY",
    "WORKFLOW_NODES",
    "RagNodes",
    "RagState",
    "build_rag_graph",
    "route_after_retrieval",
]
