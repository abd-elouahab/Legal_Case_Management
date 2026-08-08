"""Unit tests for the Report Generation Agent's workflow graph.

The graph owns the *order* and nothing else, so these tests drive it with a
recorder that only writes down what ran — no pipeline, no model, no database.
That split is the whole reason the graph is a separate module, and it is what
lets these assert the two things the spec actually requires of it: that the
agent's six responsibilities happen in the order it lists them, and that
generation is **incremental** rather than one call per report.

The nodes' own behaviour is tested in ``tests/unit/test_report_service.py``,
against the real pipeline.
"""

from __future__ import annotations

import uuid

import pytest

from core.reports import template_for
from models.report import ReportType
from schemas.report import ReportCreate
from services.report_graph import (
    ASSEMBLE,
    FINALIZE,
    PLAN,
    VALIDATE,
    WORKFLOW_NODES,
    WRITE_SECTION,
    ReportState,
    build_report_graph,
    route_after_section,
)


class RecordingNodes:
    """A :class:`~services.report_graph.ReportNodes` that only records its order.

    Every node returns the smallest state change the graph needs to keep moving,
    so a traversal is driven by the graph's edges rather than by anything the
    recorder decides. ``write_section`` is the exception and has to be real
    enough to advance the cursor, because that is the value the loop's edge reads.
    """

    def __init__(self, *, sections: int = 3) -> None:
        self.calls: list[str] = []
        self._sections = sections

    def plan_report(self, state: ReportState) -> ReportState:
        self.calls.append(PLAN)
        plan = list(template_for(state["request"].report_type).sections)[: self._sections]
        return ReportState(plan=plan, cursor=0, drafts=[])

    def write_section(self, state: ReportState) -> ReportState:
        self.calls.append(WRITE_SECTION)
        return ReportState(cursor=state.get("cursor", 0) + 1)

    def assemble_report(self, state: ReportState) -> ReportState:
        self.calls.append(ASSEMBLE)
        return ReportState(sections=[], citations=[])

    def validate_report(self, state: ReportState) -> ReportState:
        self.calls.append(VALIDATE)
        return ReportState(grounded_sections=0, character_count=0)

    def finalize_report(self, state: ReportState) -> ReportState:
        self.calls.append(FINALIZE)
        return ReportState()


def initial_state(report_type: ReportType = ReportType.CASE_SUMMARY) -> ReportState:
    return ReportState(
        request=ReportCreate(case_id=uuid.uuid4(), report_type=report_type),
        report_id=uuid.uuid4(),
        case_id=uuid.uuid4(),
        language="fr",
        started=0.0,
        deadline=1e12,
        cursor=0,
        drafts=[],
    )


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


class TestShape:
    def test_the_workflow_is_the_agents_declared_responsibilities(self) -> None:
        """``14-ai-report-agent.md`` lists six responsibilities; the graph is
        five nodes because "generate citations" happens where the sections are
        assembled — the two cannot be separated without numbering a section's
        markers twice."""
        assert WORKFLOW_NODES == (PLAN, WRITE_SECTION, ASSEMBLE, VALIDATE, FINALIZE)

    def test_the_compiled_graph_contains_exactly_those_nodes(self) -> None:
        """How "a report still plans before it writes, and validates before it
        finalises" stays true after somebody adds a sixth node."""
        graph = build_report_graph(RecordingNodes())

        nodes = set(graph.get_graph().nodes) - {"__start__", "__end__"}
        assert nodes == set(WORKFLOW_NODES)


# --------------------------------------------------------------------------- #
# Order
# --------------------------------------------------------------------------- #


class TestOrder:
    def test_the_nodes_run_in_the_order_the_spec_gives(self) -> None:
        nodes = RecordingNodes(sections=1)

        build_report_graph(nodes).invoke(initial_state())

        assert nodes.calls == [PLAN, WRITE_SECTION, ASSEMBLE, VALIDATE, FINALIZE]

    def test_planning_happens_before_anything_is_written(self) -> None:
        nodes = RecordingNodes(sections=2)

        build_report_graph(nodes).invoke(initial_state())

        assert nodes.calls.index(PLAN) < nodes.calls.index(WRITE_SECTION)

    def test_validation_happens_before_finalisation(self) -> None:
        nodes = RecordingNodes(sections=1)

        build_report_graph(nodes).invoke(initial_state())

        assert nodes.calls.index(VALIDATE) < nodes.calls.index(FINALIZE)


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


class TestSectionLoop:
    @pytest.mark.parametrize("sections", [1, 2, 5, 7])
    def test_one_iteration_per_section(self, sections: int) -> None:
        """The spec's "Large Cases" requirement made structural: a case larger
        than any context window costs more *iterations* rather than a bigger
        prompt."""
        nodes = RecordingNodes(sections=sections)

        build_report_graph(nodes).invoke(
            initial_state(),
            {"recursion_limit": sections * 2 + 10},
        )

        assert nodes.calls.count(WRITE_SECTION) == sections

    def test_a_plan_with_no_sections_skips_the_loop_entirely(self) -> None:
        """Cannot happen with the shipped templates; it can happen the first time
        somebody writes a planner that filters them, and a loop with nothing to
        do must not be entered."""
        nodes = RecordingNodes(sections=0)

        build_report_graph(nodes).invoke(initial_state())

        assert nodes.calls == [PLAN, ASSEMBLE, VALIDATE, FINALIZE]

    def test_the_route_is_a_pure_function_of_the_state(self) -> None:
        """Which is what would make a future concurrent or resumable
        implementation a change to one node rather than to how the loop is
        driven."""
        section = template_for(ReportType.CASE_SUMMARY).sections[0]

        assert route_after_section(ReportState(cursor=0, plan=[section])) == WRITE_SECTION
        assert route_after_section(ReportState(cursor=1, plan=[section])) == ASSEMBLE

    def test_the_route_ends_the_loop_when_the_plan_is_empty(self) -> None:
        assert route_after_section(ReportState(cursor=0, plan=[])) == ASSEMBLE
