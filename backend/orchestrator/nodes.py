"""Stub specialist nodes (M4) plus the real aggregator join node (M5).

Owns: the four parallel "specialist" nodes (security, quality, tests, docs)
that the graph in ``backend.orchestrator.graph`` fans out to, plus the join
node they converge on. Per M4's scope, every specialist is a STUB: it
returns a canned, deterministic ``Finding`` and makes no LLM call and reads
no API key. Wiring in real reasoning is M8's job, not this one's.

``aggregate_node`` (the join point) is no longer a stub as of M5: it dedupes
the merged findings (``backend.agents.contracts.dedupe_findings``), computes
``overall_confidence`` (``backend.models.review.compute_overall_confidence``),
runs the HITL gate (``backend.hitl.queue.route_review``), and writes the
resulting ``Review`` plus the routing reason into ``GraphState`` (see
``backend.orchestrator.state`` for why those two fields are ``NotRequired``).
No LLM call happens here either — M5's whole aggregation/routing pipeline is
pure Python over already-produced ``Finding`` objects, which is exactly what
makes it testable without one (``tests/unit/test_aggregator.py`` and
``tests/unit/test_hitl_gate.py`` exercise this logic directly, with
fixtures, faster and more precisely than driving it through the graph).

This module also owns two things needed only to *prove* M4's behavior in
tests, not for any production purpose:

1. Instrumentation (``call_count`` / ``execution_windows``) — a thread-safe,
   per-``(thread_id, node_name)`` record of how many times a node's body
   actually ran and the wall-clock window each successful run occupied. This
   is what lets a test assert, with numbers rather than a manual demo, that
   (a) the four nodes' execution windows overlap (real parallelism) and (b)
   a node that already completed before a simulated crash is not re-executed
   on resume (its call count does not increase).

2. Two independent failure-injection hooks, because M4 must prove two
   different things that are easy to conflate:
   - ``arm_crash`` simulates the *worker process dying* mid-node (a real
     ``SimulatedNodeCrashError`` propagates out of the node, out of the graph, and
     out of ``WorkflowEngine.run`` uncaught — exactly what a killed process
     looks like from the checkpointer's point of view). This is what the
     checkpoint-resume test exercises.
   - ``arm_agent_error`` simulates a specialist's *own* logic failing (e.g.
     a malformed diff, a future LLM call erroring) — an ``AgentExecutionError``
     that the node itself catches and isolates, so the other three
     specialists' findings still reach the final state instead of the whole
     run being lost to one agent's bug.

M7 addition: every specialist node's stub work now runs inside
``backend.observability.traced_span``, which emits a ``span.start`` event
before ``_run_stub`` begins and a ``span.end`` event (with measured
``latency_ms`` and an "ok"/"error" outcome) after it finishes or raises —
this is the events spine's live call site for per-specialist tracing.
``aggregate_node`` additionally emits one ``decision`` event recording the
final ``status``/``overall_confidence`` — the events spine's live call site
for the aggregator's routing decision. Both freeze-boundary exceptions
(this file is not in M7's literal freeze-boundary list) are disclosed in
this milestone's build report, following M5/M6's own precedent for
explicitly-instructed out-of-boundary changes.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from langchain_core.runnables import RunnableConfig

from backend.agents.contracts import dedupe_findings
from backend.core.settings import get_settings
from backend.hitl.queue import route_review
from backend.models import AgentType, Finding, Review, Severity, compute_overall_confidence
from backend.observability import emit_decision, get_event_repository, traced_span
from backend.orchestrator.state import GraphState

# Simulated per-node work duration. Large enough that overlapping windows are
# unambiguous under normal scheduling jitter, small enough that the fan-out
# test suite stays fast. Four nodes run sequentially would take 4x this; run
# in parallel, wall time should stay close to this single value.
NODE_WORK_SECONDS = 0.2


class SimulatedNodeCrashError(RuntimeError):
    """Raised by a stub node when a test has armed a simulated worker crash.

    Deliberately a distinct type from ``AgentExecutionError``: node wrapper
    functions let this one propagate uncaught (it represents the whole
    worker process dying, which must fail the graph run so a checkpoint/
    resume cycle can be exercised), whereas ``AgentExecutionError`` is caught
    and isolated per node.
    """


class AgentExecutionError(RuntimeError):
    """Raised (internally) when a specialist's own stub work fails.

    Represents a normal, isolated agent-level failure — the kind a real LLM-
    backed agent could hit in M8 (a malformed diff, a provider error, a
    response that fails schema validation). Node wrapper functions catch
    this and record it in ``GraphState.node_errors`` instead of letting it
    fail the whole run: one bad specialist must not cost the other three
    their findings.
    """


# ---------------------------------------------------------------------------
# Instrumentation: thread-safe, keyed by (thread_id, node_name). LangGraph
# runs a super-step's parallel branches concurrently (a thread pool for sync
# graphs), so every one of these structures must be safe for concurrent
# read/write from multiple specialist nodes at once.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_call_counts: dict[tuple[str, str], int] = defaultdict(int)
_execution_windows: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
_armed_crashes: dict[tuple[str, str], int] = defaultdict(int)
_armed_errors: dict[tuple[str, str], int] = defaultdict(int)


def reset_instrumentation() -> None:
    """Test-only: clear all recorded call counts, timing windows, and arming.

    Call this between tests that reuse a ``thread_id`` (or between test
    modules) so one test's recorded calls can't be mistaken for another's.
    """
    with _lock:
        _call_counts.clear()
        _execution_windows.clear()
        _armed_crashes.clear()
        _armed_errors.clear()


def arm_crash(thread_id: str, node_name: str, *, times: int = 1) -> None:
    """Test-only: make ``node_name`` raise ``SimulatedNodeCrashError`` next time(s).

    Each armed crash is consumed exactly once per invocation, so a node can
    be made to fail on its first call and then succeed on a later resume
    without any further test setup.
    """
    with _lock:
        _armed_crashes[(thread_id, node_name)] += times


def arm_agent_error(thread_id: str, node_name: str, *, times: int = 1) -> None:
    """Test-only: make ``node_name`` raise (and internally isolate) an error."""
    with _lock:
        _armed_errors[(thread_id, node_name)] += times


def call_count(thread_id: str, node_name: str) -> int:
    """Number of times ``node_name`` has actually executed its body for this run."""
    with _lock:
        return _call_counts[(thread_id, node_name)]


def execution_windows(thread_id: str, node_name: str) -> list[tuple[float, float]]:
    """(start, end) ``time.monotonic()`` windows for each successful run."""
    with _lock:
        return list(_execution_windows[(thread_id, node_name)])


def _thread_id_from_config(config: RunnableConfig) -> str:
    configurable = config.get("configurable") or {}
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("stub node invoked without a configurable.thread_id")
    return thread_id


def _run_stub(node_name: str, agent_type: AgentType, config: RunnableConfig) -> Finding:
    """Shared body for all four specialist stub nodes.

    Records the call, honors any armed crash/error for this
    ``(thread_id, node_name)``, sleeps to simulate work, records the
    execution window, and returns this agent's canned ``Finding``.
    """
    thread_id = _thread_id_from_config(config)

    with _lock:
        _call_counts[(thread_id, node_name)] += 1
        should_crash = _armed_crashes[(thread_id, node_name)] > 0
        if should_crash:
            _armed_crashes[(thread_id, node_name)] -= 1
        should_error = _armed_errors[(thread_id, node_name)] > 0
        if should_error:
            _armed_errors[(thread_id, node_name)] -= 1

    if should_crash:
        raise SimulatedNodeCrashError(
            f"simulated worker crash in node {node_name!r} (thread {thread_id!r})"
        )
    if should_error:
        raise AgentExecutionError(
            f"simulated agent failure in node {node_name!r} (thread {thread_id!r})"
        )

    start = time.monotonic()
    time.sleep(NODE_WORK_SECONDS)
    end = time.monotonic()
    with _lock:
        _execution_windows[(thread_id, node_name)].append((start, end))

    return _CANNED_FINDINGS[agent_type]


# Canned, deterministic findings — one per specialist. Real reasoning (an
# actual LLM call producing a real Finding from a real diff) is M8's scope;
# M4 only has to prove that four distinct agent types can each contribute a
# schema-valid Finding through the graph.
_CANNED_FINDINGS: dict[AgentType, Finding] = {
    AgentType.SECURITY: Finding(
        agent_type=AgentType.SECURITY,
        severity=Severity.HIGH,
        category="stub_finding",
        file_path="stub/security.py",
        line_start=1,
        line_end=1,
        confidence=Decimal("0.900"),
        rationale="M4 stub: canned security finding, no real analysis performed.",
    ),
    AgentType.QUALITY: Finding(
        agent_type=AgentType.QUALITY,
        severity=Severity.MEDIUM,
        category="stub_finding",
        file_path="stub/quality.py",
        line_start=1,
        line_end=1,
        confidence=Decimal("0.800"),
        rationale="M4 stub: canned quality finding, no real analysis performed.",
    ),
    AgentType.TESTS: Finding(
        agent_type=AgentType.TESTS,
        severity=Severity.LOW,
        category="stub_finding",
        file_path="stub/tests.py",
        line_start=1,
        line_end=1,
        confidence=Decimal("0.700"),
        rationale="M4 stub: canned tests finding, no real analysis performed.",
    ),
    AgentType.DOCS: Finding(
        agent_type=AgentType.DOCS,
        severity=Severity.INFO,
        category="stub_finding",
        file_path="stub/docs.py",
        line_start=1,
        line_end=1,
        confidence=Decimal("0.600"),
        rationale="M4 stub: canned docs finding, no real analysis performed.",
    ),
}


def security_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """Specialist stub: security review. See module docstring for M4 scope + M7 tracing."""
    with traced_span(get_event_repository(), state["review_id"], "security"):
        try:
            finding = _run_stub("security", AgentType.SECURITY, config)
        except SimulatedNodeCrashError:
            raise
        except AgentExecutionError as exc:
            return {"findings": [], "node_errors": {"security": str(exc)}}
    return {"findings": [finding], "node_errors": {}}


def quality_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """Specialist stub: code quality review. See module docstring for M4 scope + M7 tracing."""
    with traced_span(get_event_repository(), state["review_id"], "quality"):
        try:
            finding = _run_stub("quality", AgentType.QUALITY, config)
        except SimulatedNodeCrashError:
            raise
        except AgentExecutionError as exc:
            return {"findings": [], "node_errors": {"quality": str(exc)}}
    return {"findings": [finding], "node_errors": {}}


def tests_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """Specialist stub: test-coverage review. See module docstring for M4 scope + M7 tracing."""
    with traced_span(get_event_repository(), state["review_id"], "tests"):
        try:
            finding = _run_stub("tests", AgentType.TESTS, config)
        except SimulatedNodeCrashError:
            raise
        except AgentExecutionError as exc:
            return {"findings": [], "node_errors": {"tests": str(exc)}}
    return {"findings": [finding], "node_errors": {}}


def docs_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """Specialist stub: documentation review. See module docstring for M4 scope + M7 tracing."""
    with traced_span(get_event_repository(), state["review_id"], "docs"):
        try:
            finding = _run_stub("docs", AgentType.DOCS, config)
        except SimulatedNodeCrashError:
            raise
        except AgentExecutionError as exc:
            return {"findings": [], "node_errors": {"docs": str(exc)}}
    return {"findings": [finding], "node_errors": {}}


def aggregate_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """Join point the four specialists converge on: the real M5 aggregator.

    Fed by ``GraphState.findings`` — every specialist's contribution already
    merged by the ``operator.add`` reducer (see ``state.py``), in whatever
    order the parallel branches happened to finish. This node:

    1. Dedupes same-``(file_path, line_start)`` findings, keeping the one
       that matters most: higher severity wins first, confidence only
       breaks a tie *within* the same severity
       (``backend.agents.contracts.dedupe_findings`` — see that module for
       the full deterministic tie-break rule and why severity must come
       first).
    2. Computes ``overall_confidence`` from the *surviving* (deduped)
       findings via the one formula ``Review`` itself enforces
       (``backend.models.review.compute_overall_confidence``) — using
       anything else here would immediately fail ``Review``'s own
       consistency check below.
    3. Runs the HITL gate (``backend.hitl.queue.route_review``) against the
       configured threshold to decide POSTED vs. QUEUED_FOR_HITL, and builds
       the reason string explaining why.
    4. Constructs the ``Review`` and returns it (plus the reason) as a
       partial state update — LangGraph merges this into ``GraphState`` on
       ``review``/``routing_reason`` since neither has a reducer and this is
       the only node that ever writes them (see ``state.py``).

    No LLM call, no GitHub post, no queue write: those are M8/M10/M11's
    scope. This node's whole job ends at "here is the Review and why it was
    routed that way".
    """
    deduped = dedupe_findings(state["findings"])
    overall_confidence = compute_overall_confidence(deduped)
    threshold = get_settings().hitl_confidence_threshold
    status, reason = route_review(overall_confidence, deduped, threshold=threshold)

    review = Review(
        review_id=state["review_id"],
        pr_number=state["pr_number"],
        repository_owner=state["repository_owner"],
        repository_name=state["repository_name"],
        head_sha=state["head_sha"],
        findings=deduped,
        overall_confidence=overall_confidence,
        status=status,
        created_at=datetime.now(UTC),
        # error_message defaults to None on Review, but mypy --strict's
        # dataclass_transform-based reading of `Field(None, ...)` (as
        # opposed to a plain `= None` class-body default) does not detect
        # that as optional, so it must be passed explicitly here — the one
        # call site in backend/ that actually constructs a Review.
        error_message=None,
    )
    # M7: the events spine's live call site for the aggregator's routing
    # decision -- see module docstring. A failure to write this event (the
    # events Postgres unreachable) is caught and logged inside
    # emit_decision itself; it never raises here and never prevents the
    # Review above from being returned.
    emit_decision(
        get_event_repository(),
        state["review_id"],
        agent="aggregator",
        outcome=status.value,
        confidence=overall_confidence,
    )
    return {"review": review, "routing_reason": reason}
