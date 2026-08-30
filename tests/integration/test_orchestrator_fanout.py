"""Integration tests for the M4 orchestrator fan-out/fan-in graph.

This is the file PLAN.md's M4 demo command names
(``pytest tests/integration/test_orchestrator_fanout.py -v -k "fanout or
checkpoint_resume"``), and it is where this milestone's central claim gets
proven with assertions, not a manual demo:

- fan-out to the four specialists is genuinely parallel, not a linear chain
  wearing a fan-out's clothes (``test_fanout_runs_nodes_in_parallel``)
- all four specialists' findings land in the final state
  (``test_fanout_all_four_nodes_contribute_findings``)
- parallel branches' writes merge via GraphState's reducers instead of the
  last branch to finish silently clobbering the other three
  (``test_fanout_state_merges_across_parallel_branches``)
- a node's committed output survives a simulated worker crash, and is not
  re-executed when a *new* engine instance resumes the same thread_id from
  the checkpoint on disk (``test_checkpoint_resume_skips_completed_nodes``)
- one specialist's own failure is isolated and does not cost the other
  three their findings (``test_fanout_isolates_single_node_failure``)

M5 addition: ``test_aggregate_node_wires_into_the_fan_in_and_produces_a_review``
proves the real aggregator (``backend.orchestrator.nodes.aggregate_node``,
no longer M4's no-op stub) is actually wired into this same compiled graph as
the fan-in join node -- not just unit-testable in isolation
(``tests/unit/test_aggregator.py`` / ``tests/unit/test_hitl_gate.py`` cover
the aggregation/routing logic itself with fixtures; this test is the one
place that proves the graph's own fan-in edge reaches it end-to-end).

Every test uses a fresh ``tmp_path``-backed checkpoint database and a unique
``thread_id`` (a fresh UUID), so tests never share state and can run in any
order.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest

from backend.models import AgentType, ReviewStatus
from backend.orchestrator import nodes
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.nodes import NODE_WORK_SECONDS
from backend.orchestrator.state import GraphState

_SPECIALISTS = ("security", "quality", "tests", "docs")


def _initial_state(review_id: str) -> GraphState:
    """Build a minimal, valid initial GraphState for one test run."""
    return GraphState(
        review_id=review_id,
        pr_number=1,
        repository_owner="acme",
        repository_name="widgets",
        head_sha="a" * 40,
        findings=[],
        node_errors={},
    )


def _new_engine(tmp_path: Path, name: str = "checkpoints.sqlite3") -> LangGraphWorkflowEngine:
    return LangGraphWorkflowEngine(tmp_path / name)


def test_fanout_runs_nodes_in_parallel(tmp_path: Path) -> None:
    """Real fan-out: wall time is close to one node's work, not the sum of four.

    A linear chain visiting four nodes one after another would take roughly
    ``4 * NODE_WORK_SECONDS``. Real parallel fan-out should take roughly
    ``NODE_WORK_SECONDS`` regardless of how many specialists run — this
    asserts the wall time is far under the halfway point between those two,
    and, as a second, independent check, that every pair of nodes' recorded
    execution windows actually overlap in time.
    """
    thread_id = str(uuid4())
    engine = _new_engine(tmp_path)
    try:
        start = monotonic()
        engine.run(thread_id, _initial_state(thread_id))
        wall_time = monotonic() - start
    finally:
        engine.close()

    sum_of_sleeps = NODE_WORK_SECONDS * len(_SPECIALISTS)
    assert wall_time < sum_of_sleeps / 2, (
        f"wall_time={wall_time:.3f}s is not much less than the sum of all "
        f"four nodes' sleeps ({sum_of_sleeps:.3f}s) — this looks like a "
        "linear chain, not real fan-out parallelism"
    )

    windows = {name: nodes.execution_windows(thread_id, name)[0] for name in _SPECIALISTS}
    names = list(windows)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a_start, a_end = windows[names[i]]
            b_start, b_end = windows[names[j]]
            assert a_start < b_end and b_start < a_end, (
                f"{names[i]}'s window {windows[names[i]]} and {names[j]}'s "
                f"window {windows[names[j]]} do not overlap — nodes ran "
                "sequentially, not in parallel"
            )


def test_fanout_all_four_nodes_contribute_findings(tmp_path: Path) -> None:
    """Every specialist's canned Finding is present in the final state."""
    thread_id = str(uuid4())
    engine = _new_engine(tmp_path)
    try:
        result = engine.run(thread_id, _initial_state(thread_id))
    finally:
        engine.close()

    assert len(result["findings"]) == 4
    agent_types = {finding.agent_type for finding in result["findings"]}
    assert agent_types == {
        AgentType.SECURITY,
        AgentType.QUALITY,
        AgentType.TESTS,
        AgentType.DOCS,
    }


def test_fanout_state_merges_across_parallel_branches(tmp_path: Path) -> None:
    """Parallel branches' writes merge via GraphState's reducers, not overwrite.

    Without ``Annotated[list[Finding], operator.add]`` on ``findings``,
    whichever of the four specialists finished last in the super-step would
    overwrite the other three's contributions, leaving exactly one finding
    instead of four. This asserts the reducer actually combined them: four
    distinct findings, one per agent type, each still a schema-valid
    ``Finding`` (proving no corruption happened in the merge).
    """
    thread_id = str(uuid4())
    engine = _new_engine(tmp_path)
    try:
        result = engine.run(thread_id, _initial_state(thread_id))
    finally:
        engine.close()

    findings = result["findings"]
    assert len(findings) == 4, (
        f"expected all 4 parallel branches' findings to merge, got "
        f"{len(findings)} — a reducer-less overwrite would collapse this to 1"
    )
    # Each finding is independently a valid, fully-formed Finding (not a
    # partially-merged fragment), and node_errors merged to an empty map
    # since nothing failed in this run.
    for finding in findings:
        assert finding.rationale
        assert 0 <= finding.confidence <= 1
    assert result["node_errors"] == {}


def test_checkpoint_resume_skips_completed_nodes(tmp_path: Path) -> None:
    """The milestone's core claim: crash mid-run, resume, don't redo finished work.

    Arms a simulated crash in the ``docs`` node, runs the graph (which
    raises), then builds a *brand-new* ``LangGraphWorkflowEngine`` pointed
    at the same on-disk checkpoint database to resume — modeling a genuinely
    new worker process picking up where a dead one left off, not just a
    retry against the same in-memory objects.

    The assertion that actually proves resume-without-redo: the three nodes
    that committed a successful result before the crash keep a call count of
    exactly 1 after resume (they were never invoked a second time), while
    ``docs`` — which crashed once — shows a call count of 2 (armed crash
    consumed, then genuinely re-executed and this time succeeded).
    """
    thread_id = str(uuid4())
    db_path = tmp_path / "checkpoints.sqlite3"

    engine = LangGraphWorkflowEngine(db_path)
    try:
        nodes.arm_crash(thread_id, "docs")
        with pytest.raises(nodes.SimulatedNodeCrashError):
            engine.run(thread_id, _initial_state(thread_id))

        # Immediately after the crash: all four were invoked once. The three
        # good ones recorded a successful execution window; docs crashed
        # before it could record one.
        for name in ("security", "quality", "tests", "docs"):
            assert nodes.call_count(thread_id, name) == 1
        assert len(nodes.execution_windows(thread_id, "docs")) == 0
        for name in ("security", "quality", "tests"):
            assert len(nodes.execution_windows(thread_id, name)) == 1
    finally:
        engine.close()

    # Simulate a new worker process: fresh engine, fresh SQLite connection,
    # same on-disk checkpoint file, same thread_id.
    resumed_engine = LangGraphWorkflowEngine(db_path)
    try:
        result = resumed_engine.resume(thread_id)
    finally:
        resumed_engine.close()

    # The core assertion: nodes that had already committed before the crash
    # were NOT re-executed.
    assert nodes.call_count(thread_id, "security") == 1
    assert nodes.call_count(thread_id, "quality") == 1
    assert nodes.call_count(thread_id, "tests") == 1
    # docs crashed once, then ran again on resume (this time without an
    # armed crash) and succeeded.
    assert nodes.call_count(thread_id, "docs") == 2
    assert len(nodes.execution_windows(thread_id, "docs")) == 1

    # And the run actually completed: all four findings present.
    assert len(result["findings"]) == 4
    agent_types = {finding.agent_type for finding in result["findings"]}
    assert agent_types == {
        AgentType.SECURITY,
        AgentType.QUALITY,
        AgentType.TESTS,
        AgentType.DOCS,
    }


def test_fanout_isolates_single_node_failure(tmp_path: Path) -> None:
    """One specialist's own failure must not lose the whole run.

    Distinct from the crash-resume scenario: this simulates a specialist's
    *own* logic failing (``AgentExecutionError``, e.g. what a real LLM-backed
    agent in M8 might raise on a malformed diff), which the node itself
    catches and records in ``node_errors`` rather than letting propagate.
    The other three specialists' findings must still be present.
    """
    thread_id = str(uuid4())
    engine = _new_engine(tmp_path)
    try:
        nodes.arm_agent_error(thread_id, "quality")
        result = engine.run(thread_id, _initial_state(thread_id))
    finally:
        engine.close()

    assert "quality" in result["node_errors"]
    assert result["node_errors"]["quality"]  # non-empty message, not swallowed silently

    findings = result["findings"]
    assert len(findings) == 3
    agent_types = {finding.agent_type for finding in findings}
    assert agent_types == {AgentType.SECURITY, AgentType.TESTS, AgentType.DOCS}


def test_aggregate_node_wires_into_the_fan_in_and_produces_a_review(tmp_path: Path) -> None:
    """M5: the real aggregator actually runs as the graph's fan-in join node.

    The four canned M4 stub findings (confidences 0.900/0.800/0.700/0.600,
    one per agent, on four distinct stub file paths -- see
    ``nodes._CANNED_FINDINGS``) never collide on ``(file_path, line_start)``,
    so dedup is a no-op here and overall_confidence is exactly their mean:
    (0.900+0.800+0.700+0.600)/4 = 0.750 -- exactly the default
    HITL_CONFIDENCE_THRESHOLD, which this milestone's boundary rule defines
    as auto-post (see backend/hitl/queue.py).
    """
    thread_id = str(uuid4())
    engine = _new_engine(tmp_path)
    try:
        result = engine.run(thread_id, _initial_state(thread_id))
    finally:
        engine.close()

    review = result["review"]
    assert review is not None
    assert len(review.findings) == 4
    assert review.overall_confidence == Decimal("0.750")
    assert review.status == ReviewStatus.POSTED

    reason = result["routing_reason"]
    assert "0.750" in reason
