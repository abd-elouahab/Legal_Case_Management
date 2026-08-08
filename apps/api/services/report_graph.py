"""The Report Generation Agent's workflow, as a LangGraph state graph.

``ai-architecture.md`` names LangGraph as the orchestrator and lists the **Report
Generation Agent** among the agents future features add, *"only when their
corresponding feature is implemented"*. This module is that agent's declaration.
``14-ai-report-agent.md`` gives it six responsibilities and this graph is exactly
those six, in order:

.. code-block:: text

    Select Report Type → Request Context + Generate Sections
                       → Assemble Report → Generate Citations
                       → Validate Output → Prepare Export

**This module owns the order, and nothing else.** Every node is a call onto a
:class:`ReportNodes` implementation — :class:`~services.report.ReportService` —
so the graph is a readable statement of *what happens when*, with no retrieval,
no prompt, no provider, and no rendering inside it. That split is what makes the
two independently testable: the graph can be driven with a stub that records the
order it was called in, and the service's nodes can be called with no graph at
all. It is the same split :mod:`services.rag_graph` makes, for the same reason.

--------------------------------------------------------------------------------
Why this is its own graph, and why it reuses the *service* rather than the nodes
--------------------------------------------------------------------------------

:mod:`services.rag_graph` reserved the shape: *"report generation — its own graph
reusing these nodes, not a branch here"*. Its own graph is exactly what this is.
What it reuses, though, is :meth:`~services.rag.RagService.answer` — the whole
pipeline — rather than the pipeline's individual nodes, and that is a deliberate
improvement on the note rather than a departure from it.

Re-wiring ``retrieve → assemble → generate → verify → format`` into this graph
would mean re-implementing, here, the branch that skips the model when nothing
was retrieved, the character budget that fits passages to the context window, the
refusal sentinel, the removal of invented markers, and the attachment of
citations. ``14-ai-report-agent.md`` forbids precisely that: *"It must not
duplicate retrieval, prompt construction, or LLM interaction logic."* Calling the
service means a report section **is** a grounded answer, produced by the same
code path, verified by the same rules, and cited by the same mechanism — and the
spec's *"the Report Generation Agent must retrieve supporting information
exclusively through the existing RAG Pipeline"* and *"it must never query Qdrant
directly"* hold structurally, because this graph has no other collaborator.

--------------------------------------------------------------------------------
The loop, and why it is one
--------------------------------------------------------------------------------

``write_section`` is a **self-looping node**: it writes one section, advances an
index, and the conditional edge sends control back to itself until the template
is exhausted. That is not decoration either — it is the spec's "Large Cases"
requirement made structural:

* *"retrieve context incrementally"* — each iteration retrieves only for the
  section it is writing;
* *"generate reports section-by-section when appropriate"* — one model call per
  section, never one for the report;
* *"avoid exceeding model limits"* — a section's context is one pipeline run's
  budget, so a case with four hundred documents costs more *iterations* rather
  than a larger prompt.

A future optimisation — sections generated concurrently, a planner that chooses
sections from the case's contents, a revision pass over the assembled draft — is
an edge out of this loop rather than a redesign, which is what the spec's
*"extensible for future optimization"* asks to remain true.

**Nothing here manages a conversation**, and the state deliberately has no key
for one: a report's optional ``conversation_id`` is provenance recorded on the
row, and the agent never reads a transcript.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, TypedDict

import structlog

from core.reports import ReportSectionSpec, ReportTemplate
from models.user import User
from schemas.rag import RagCitationRead
from schemas.report import ReportCreate

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------- #
# Node names
#
# Constants rather than string literals at each edge, for the reason
# :mod:`services.rag_graph` records: a mistyped *target* is caught at build time,
# but a mistyped *source* is silently an orphan node that never runs.
# --------------------------------------------------------------------------- #

PLAN = "plan"
WRITE_SECTION = "write_section"
ASSEMBLE = "assemble"
VALIDATE = "validate"
FINALIZE = "finalize"


class SectionDraft(TypedDict, total=False):
    """One written section, before the report's citations are numbered.

    Carries its own citations and its own local markers, because at the moment it
    is produced they are the pipeline's — ``[1]`` here means "the first source of
    *this* section". The assemble node is what turns those into the report's
    global numbering; see :func:`~core.reports.remap_markers`.
    """

    key: str
    title: str
    content: str
    grounded: bool
    #: Whether the model hit its output ceiling and the section stops
    #: mid-thought. Carried through to the reader rather than swallowed: a legal
    #: section that ends early must not be presented as a complete one.
    truncated: bool
    citations: list[RagCitationRead]
    retrieved_count: int
    context_count: int
    duration_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    provider: str | None
    model: str | None
    prompt_name: str | None
    prompt_version: int | None


class ReportState(TypedDict, total=False):
    """Everything one generation run carries between its nodes.

    ``total=False`` because the state is built up: ``template`` exists after
    planning, ``drafts`` grows one entry per loop iteration, and ``sections``
    only after assembly.

    Keys are written by exactly one node each — apart from ``drafts`` and
    ``cursor``, which the loop node owns and is the only writer of — which is why
    the graph needs no reducers: there is no key two nodes both append to, and
    therefore no merge rule anyone has to remember.
    """

    # --- set by the caller ---
    #: The validated request, kept whole so a node can read a filter or a limit
    #: without the state growing a key per field.
    request: ReportCreate
    #: Who asked. Present because every section's retrieval is scoped to them —
    #: it is the single most important value in this dictionary, and the reason
    #: the graph cannot be run "for the platform" with no actor.
    actor: User
    #: The report row being produced, and the case it is about. Identifiers only:
    #: the graph runs on a background thread and an ORM instance handed across
    #: one is a stale read waiting to happen.
    report_id: uuid.UUID
    case_id: uuid.UUID
    language: str
    #: :func:`time.monotonic` reading when the run began, and the instant it must
    #: finish by. Both live in the state rather than on the service, because a
    #: service instance is shared and a per-run value stored on it would be a
    #: race between two reports.
    started: float
    deadline: float

    # --- plan ---
    template: ReportTemplate
    #: The sections still to write, in template order. A list rather than the
    #: template's own tuple because the plan node may bound it (see
    #: ``REPORT_MAX_SECTIONS``), and because a future planner that *chooses*
    #: sections writes here rather than needing a new key.
    plan: list[ReportSectionSpec]

    # --- write_section (the loop) ---
    #: Which entry of ``plan`` is next. The loop's whole state, and the reason
    #: the conditional edge below is a pure function of the graph state rather
    #: than of anything the service remembers between calls.
    cursor: int
    drafts: list[SectionDraft]

    # --- assemble ---
    #: The sections with their markers renumbered against ``citations``.
    sections: list[dict[str, Any]]
    citations: list[RagCitationRead]

    # --- validate / finalize ---
    grounded_sections: int
    character_count: int


class ReportNodes(Protocol):
    """What the graph requires of the service beneath it.

    Five members, one per node, and each takes and returns the state. The
    protocol exists so this module depends on a *shape* rather than on
    :class:`~services.report.ReportService` — which is what lets the graph be
    tested with a recorder, and what would let a second kind of report (a
    scheduled digest, a multi-case portfolio view) reuse this order without
    importing the single-report service.
    """

    def plan_report(self, state: ReportState) -> ReportState:
        """Select the template and the sections this run will produce."""
        ...

    def write_section(self, state: ReportState) -> ReportState:
        """Produce the next section through the RAG pipeline, and advance."""
        ...

    def assemble_report(self, state: ReportState) -> ReportState:
        """Merge the drafts into one document and renumber their citations."""
        ...

    def validate_report(self, state: ReportState) -> ReportState:
        """Refuse a report nothing could be grounded in, and count what was."""
        ...

    def finalize_report(self, state: ReportState) -> ReportState:
        """Record the finished report and make it exportable."""
        ...


def route_after_section(state: ReportState) -> str:
    """Choose whether to write another section or move on to assembly.

    The whole of the decision is *are there sections left*. It is a function
    rather than a lambda so it can be unit-tested on its own and so the reason
    lives next to it: this edge is what makes generation incremental, which is
    what lets a case larger than any context window still produce a report.

    Reading ``cursor`` from the state rather than from a counter on the service
    is what keeps it a pure function — and therefore what makes a future
    concurrent or resumable implementation a change to one node instead of a
    change to how the loop is driven.
    """
    return WRITE_SECTION if state.get("cursor", 0) < len(state.get("plan", [])) else ASSEMBLE


def build_report_graph(nodes: ReportNodes) -> Any:
    """Compile the workflow.

    Returns LangGraph's compiled graph, typed loosely on purpose: the library's
    generic parameters move between versions, this feature calls exactly one
    method on the result (``invoke``), and pinning a generic here would make a
    library upgrade a change to this feature's type signatures — the same
    reasoning :func:`~services.rag_graph.build_rag_graph` records.

    ``recursion_limit`` is raised above LangGraph's default of 25 because the
    section loop legitimately re-enters one node once per section, and the
    default would refuse a template with more than about two dozen of them.
    ``REPORT_MAX_SECTIONS`` is the real bound; this one just has to be above it.

    Compiled once per service instance rather than per report — compilation
    validates every edge and builds the executor, and neither depends on which
    report is being generated.
    """
    from langgraph.graph import END, START, StateGraph

    graph: Any = StateGraph(ReportState)

    graph.add_node(PLAN, nodes.plan_report)
    graph.add_node(WRITE_SECTION, nodes.write_section)
    graph.add_node(ASSEMBLE, nodes.assemble_report)
    graph.add_node(VALIDATE, nodes.validate_report)
    graph.add_node(FINALIZE, nodes.finalize_report)

    graph.add_edge(START, PLAN)
    # Routed rather than edged straight into the loop, so a template that plans
    # *no* sections goes to assembly instead of entering a loop with nothing to
    # do. That cannot happen with the shipped templates; it can happen the first
    # time somebody writes a planner that filters them.
    graph.add_conditional_edges(
        PLAN, route_after_section, {WRITE_SECTION: WRITE_SECTION, ASSEMBLE: ASSEMBLE}
    )
    graph.add_conditional_edges(
        WRITE_SECTION, route_after_section, {WRITE_SECTION: WRITE_SECTION, ASSEMBLE: ASSEMBLE}
    )
    graph.add_edge(ASSEMBLE, VALIDATE)
    graph.add_edge(VALIDATE, FINALIZE)
    graph.add_edge(FINALIZE, END)

    return graph.compile()


#: Every node in the workflow, in the order the spec lists the agent's
#: responsibilities.
#:
#: Exported so a test can assert that the compiled graph contains exactly these —
#: which is how "a report still retrieves before it generates, and validates
#: before it finalises" stays true after somebody adds a sixth node.
WORKFLOW_NODES: tuple[str, ...] = (PLAN, WRITE_SECTION, ASSEMBLE, VALIDATE, FINALIZE)


__all__ = [
    "ASSEMBLE",
    "FINALIZE",
    "PLAN",
    "VALIDATE",
    "WORKFLOW_NODES",
    "WRITE_SECTION",
    "ReportNodes",
    "ReportState",
    "SectionDraft",
    "build_report_graph",
    "route_after_section",
]
