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

M8 addition: ``security_node`` is no longer M4's canned stub in production
(see ``backend.orchestrator.nodes``'s own M8 docstring addition) -- it
delegates to a real ``backend.agents.security_agent.SecurityAgent`` by
default. Every test ABOVE this milestone's own new test class predates that
change and asserts on the exact M4 canned ``Finding`` (file path, severity,
confidence) the security stub used to return; the module-scoped
``_stub_security_agent_for_pre_existing_tests`` autouse fixture below
installs a small fake agent that reproduces that exact canned behavior
(same ``Finding``, same simulated work duration) via
``backend.orchestrator.nodes.set_security_agent_for_testing``, so those
tests keep proving what they always proved (fan-out parallelism,
crash-resume, error isolation, aggregation wiring) without requiring
``ANTHROPIC_API_KEY`` and without their assertions silently meaning
something different. ``TestRealSecurityAgentSlotsIntoTheGraph`` is the one
new test class that installs an actual ``SecurityAgent`` (with a fake LLM
client, still no API key) to prove the real M8 agent's own findings flow
through the compiled graph alongside the three unchanged stubs.

Every test uses a fresh ``tmp_path``-backed checkpoint database and a unique
``thread_id`` (a fresh UUID), so tests never share state and can run in any
order.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx2
import pytest
from anthropic import AuthenticationError

from backend.agents.base_agent import BaseAgent
from backend.agents.security_agent import SecurityAgent
from backend.economics.budget import BudgetGuard
from backend.hitl.queue import route_review
from backend.models import (
    AgentType,
    Finding,
    ReviewStatus,
    Severity,
    compute_overall_confidence,
)
from backend.orchestrator import nodes
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.nodes import NODE_WORK_SECONDS
from backend.orchestrator.state import GraphState
from backend.tools.llm_client import AnthropicLLMClient, LLMResponse

_SPECIALISTS = ("security", "quality", "tests", "docs")


def _initial_state(review_id: str, *, diff: str = "") -> GraphState:
    """Build a minimal, valid initial GraphState for one test run."""
    return GraphState(
        review_id=review_id,
        pr_number=1,
        repository_owner="acme",
        repository_name="widgets",
        head_sha="a" * 40,
        findings=[],
        node_errors={},
        diff=diff,
    )


class _StubSecurityAgentForPreExistingTests(BaseAgent):
    """Reproduces M4's exact canned security stub, for tests written before M8.

    Same ``Finding`` (``nodes._CANNED_FINDINGS[AgentType.SECURITY]``), same
    simulated work duration (``NODE_WORK_SECONDS``) -- installed via the
    autouse fixture below so every pre-M8 test in this module keeps
    exercising fan-out/crash-resume/aggregation mechanics without depending
    on a real (or even fake-but-different) LLM-backed agent's specific
    output.
    """

    agent_type = AgentType.SECURITY

    def analyze(self, diff: str, *, review_id: str | None = None) -> list[Finding]:
        time.sleep(NODE_WORK_SECONDS)
        return [nodes._CANNED_FINDINGS[AgentType.SECURITY]]


@pytest.fixture(autouse=True)
def _stub_security_agent_for_pre_existing_tests() -> Iterator[None]:
    """Install the M4-equivalent stub for every test, unless a test overrides it itself.

    ``TestRealSecurityAgentSlotsIntoTheGraph`` below installs its OWN
    override (a real ``SecurityAgent``) after this fixture runs, which
    simply replaces this one for the duration of that test -- see
    ``backend.orchestrator.nodes.set_security_agent_for_testing``'s
    docstring ("replaces the previous entry").
    """
    nodes.set_security_agent_for_testing(_StubSecurityAgentForPreExistingTests())
    try:
        yield
    finally:
        nodes.set_security_agent_for_testing(None)


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


class TestRealSecurityAgentSlotsIntoTheGraph:
    """M8: the real security_node (a real SecurityAgent, fake LLM client) in the compiled graph.

    Still no ANTHROPIC_API_KEY anywhere -- the fake LLM client returns a
    fixed, well-formed JSON response, exercising the full
    ``security_node -> SecurityAgent.analyze -> load_prompt + LLM call +
    response_parsing`` pipeline through the real, compiled LangGraph graph,
    not just the agent in isolation (this is the M5-lesson test: prove the
    COMPOSITION, not just SecurityAgent's own unit tests).
    """

    def test_real_security_agent_produces_findings_alongside_the_three_stubs(
        self, tmp_path: Path
    ) -> None:
        fake_response_text = json.dumps(
            {
                "findings": [
                    {
                        "severity": "HIGH",
                        "category": "sql_injection",
                        "file_path": "app/db.py",
                        "line_start": 10,
                        "line_end": 12,
                        "confidence": "0.900",
                        "rationale": "Raw string concatenation builds a SQL query from user input.",
                    }
                ]
            }
        )

        class _FakeLLMClient:
            def complete(
                self,
                *,
                system: str,
                user: str,
                agent: str,
                review_id: str | None = None,
            ) -> LLMResponse:
                return LLMResponse(
                    text=fake_response_text,
                    model="claude-haiku-4-5",
                    tokens_in=10,
                    tokens_out=10,
                    cost_usd=Decimal("0.000100"),
                    latency_ms=5,
                )

        real_agent = SecurityAgent(_FakeLLMClient())
        nodes.set_security_agent_for_testing(real_agent)

        thread_id = str(uuid4())
        engine = _new_engine(tmp_path)
        try:
            result = engine.run(
                thread_id, _initial_state(thread_id, diff="--- a/app/db.py\n+++ b/app/db.py\n")
            )
        finally:
            engine.close()

        findings = result["findings"]
        assert len(findings) == 4  # the real security finding + 3 unchanged stubs

        security_findings = [f for f in findings if f.agent_type == AgentType.SECURITY]
        assert len(security_findings) == 1
        security_finding = security_findings[0]
        assert security_finding.category == "sql_injection"
        assert security_finding.severity == Severity.HIGH
        assert security_finding.file_path == "app/db.py"
        assert security_finding.confidence == Decimal("0.900")

        other_agent_types = {f.agent_type for f in findings if f.agent_type != AgentType.SECURITY}
        assert other_agent_types == {AgentType.QUALITY, AgentType.TESTS, AgentType.DOCS}

        # The real aggregator still runs on top of this real finding.
        review = result["review"]
        assert review is not None
        assert any(f.category == "sql_injection" for f in review.findings)


class _NullBudgetEventRepository:
    """Duck-typed stand-in for ``EventRepository`` -- the only two methods
    ``AnthropicLLMClient`` needs (``BudgetGuard.check_and_raise`` via
    ``sum_llm_cost_for_day``, ``emit_llm_call`` via ``insert_event``) --
    without touching Postgres. Spend is always 0 (never blocks) and every
    inserted event is simply discarded; this test cares about the
    ``AuthenticationError`` path, not budget accounting or event emission.
    """

    def sum_llm_cost_for_day(self, day_start: datetime) -> Decimal:
        return Decimal("0")

    def insert_event(self, event: object) -> None:
        pass


class _AuthenticationErrorAnthropicClient:
    """Stands in for ``anthropic.Anthropic``: every ``messages.create`` call
    raises a REAL ``anthropic.AuthenticationError`` (constructed with no
    network call below) -- simulating an invalid/revoked API key exactly as
    the real SDK would raise it for a real 401 response.
    """

    class _Messages:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        def create(self, **_: object) -> object:
            raise self._exc

    def __init__(self, exc: Exception) -> None:
        self.messages = _AuthenticationErrorAnthropicClient._Messages(exc)


def _real_authentication_error() -> AuthenticationError:
    """A real ``anthropic.AuthenticationError``, built with no network call.

    Needs a real ``httpx2.Response`` (itself needing a real ``httpx2.
    Request``) to construct from -- exactly what the SDK builds internally
    from an actual HTTP 401 response. Building it by hand here is what lets
    this test inject the actual vendor exception type the M8 L2 DEBUG
    defect was about, not a stand-in that merely resembles one.
    """
    body = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(401, request=request, json=body)
    return AuthenticationError("invalid x-api-key", response=response, body=body)


class TestRealSecurityAgentAuthenticationFailureForcesHITL:
    """M8 L2 DEBUG regression (post-L4-VERIFY).

    See ``backend.tools.llm_client``'s and ``backend.orchestrator.nodes``'s
    own module docstrings for the full defect: an invalid/revoked
    ``ANTHROPIC_API_KEY`` raises ``anthropic.AuthenticationError`` from deep
    inside ``AnthropicLLMClient.complete``. Before the fix, that raw vendor
    exception was never wrapped into this project's own
    ``LLMCallFailedError`` -- it propagated straight through
    ``SecurityAgent.analyze`` and crashed ``security_node``/the whole graph
    run, since ``security_node`` only catches
    ``BudgetExceededError``/``LLMConfigurationError``/``LLMCallFailedError``.
    A *missing* key (``LLMConfigurationError``) was already handled
    correctly and forced human review -- this was found precisely because a
    real credential turned out to be rejected (401) rather than merely
    absent, and the two cases were NOT treated the same.

    This test wires a real ``SecurityAgent`` around a real
    ``AnthropicLLMClient`` (only its underlying ``anthropic_client`` is
    faked, to inject the ``AuthenticationError`` with no network call) into
    the actual compiled graph, and proves the fix end-to-end:
    1. The orchestrator run COMPLETES (``engine.run`` returns normally --
       if the old, unwrapped exception still escaped, this call itself
       would raise and the test would error out here, not merely fail an
       assertion below).
    2. The synthetic CRITICAL/confidence-0.000 finding is present.
    3. ``node_errors`` records the security specialist's failure.
    4. ``route_review`` -- both as reflected in the real ``Review`` the
       aggregator node produced, and recomputed directly here exactly like
       ``tests/unit/test_security_node_infrastructure_failures.py``'s own
       ``test_the_forced_finding_actually_routes_the_review_to_hitl`` does
       -- returns ``QUEUED_FOR_HITL``.
    """

    def test_an_invalid_api_key_forces_hitl_instead_of_crashing_the_run(
        self, tmp_path: Path
    ) -> None:
        auth_error = _real_authentication_error()
        fake_repository = _NullBudgetEventRepository()
        llm_client = AnthropicLLMClient(
            anthropic_client=_AuthenticationErrorAnthropicClient(auth_error),
            budget_guard=BudgetGuard(fake_repository, daily_cap_usd=Decimal("20")),  # type: ignore[arg-type]
            event_repository=fake_repository,  # type: ignore[arg-type]
        )
        nodes.set_security_agent_for_testing(SecurityAgent(llm_client))

        thread_id = str(uuid4())
        engine = _new_engine(tmp_path)
        try:
            # (1) The orchestrator run completes -- no exception escapes.
            result = engine.run(
                thread_id, _initial_state(thread_id, diff="--- a/app/db.py\n+++ b/app/db.py\n")
            )
        finally:
            engine.close()

        findings = result["findings"]
        security_findings = [f for f in findings if f.agent_type == AgentType.SECURITY]

        # (2) The synthetic forced-HITL CRITICAL finding is present.
        assert len(security_findings) == 1
        assert security_findings[0].severity == Severity.CRITICAL
        assert security_findings[0].confidence == Decimal("0.000")
        assert security_findings[0].category == "security_specialist_unavailable"

        # (3) node_errors records the failure -- it is not silently dropped.
        assert "security" in result["node_errors"]
        assert result["node_errors"]["security"], "the error message must not be empty/swallowed"

        # (4a) The real aggregator's own Review reflects forced HITL.
        review = result["review"]
        assert review is not None
        assert review.status == ReviewStatus.QUEUED_FOR_HITL

        # (4b) Recomputed directly against route_review, the same way
        # tests/unit/test_security_node_infrastructure_failures.py's
        # BudgetExceededError regression proves it -- forced HITL even
        # alongside another, confident, unrelated specialist's finding; it
        # must never be able to average out into an auto-post.
        other_finding = Finding(
            agent_type=AgentType.QUALITY,
            severity=Severity.LOW,
            category="stub_finding",
            file_path="stub/quality.py",
            line_start=1,
            line_end=1,
            confidence=Decimal("0.950"),
            rationale="a confident, unrelated finding from a different specialist",
        )
        all_findings = security_findings + [other_finding]
        overall_confidence = compute_overall_confidence(all_findings)
        status, reason = route_review(overall_confidence, all_findings, threshold=Decimal("0.75"))
        assert status == ReviewStatus.QUEUED_FOR_HITL
        assert "CRITICAL" in reason
