"""The four real specialist nodes (M8 SECURITY, M10 QUALITY/TESTS/DOCS) and the real aggregator (M5).

Owns: the four parallel "specialist" nodes (security, quality, tests, docs)
that the graph in ``backend.orchestrator.graph`` fans out to, plus the join
node they converge on. Per M4's original scope, every specialist was a
STUB returning a canned, deterministic ``Finding``. M8 made SECURITY real
(a real ``backend.agents.security_agent.SecurityAgent``, a real LLM call).
M10 makes the remaining three real too (``backend.agents.{quality_agent,
test_agent,docs_agent}``) -- all four specialist nodes now share one
generic body (``_run_specialist``) that delegates to a real,
``BaseAgent``-shaped agent for that node's ``AgentType``, with the exact
same instrumentation (call-count, execution-window timing, crash/error
arming) M4 originally built and M8 already proved works unchanged for a
real agent.

``aggregate_node`` (the join point) is no longer a stub as of M5: it dedupes
the merged findings (``backend.agents.contracts.dedupe_findings``), computes
``overall_confidence`` (``backend.models.review.compute_overall_confidence``),
runs the HITL gate (``backend.hitl.queue.route_review``), and writes the
resulting ``Review`` plus the routing reason into ``GraphState`` (see
``backend.orchestrator.state`` for why those two fields are ``NotRequired``).
No LLM call happens here either -- M5's whole aggregation/routing pipeline is
pure Python over already-produced ``Finding`` objects.

This module also owns two things needed only to *prove* M4's behavior in
tests, not for any production purpose:

1. Instrumentation (``call_count`` / ``execution_windows``) -- a thread-safe,
   per-``(thread_id, node_name)`` record of how many times a node's body
   actually ran and the wall-clock window each successful run occupied.
2. Two independent failure-injection hooks (``arm_crash`` /
   ``arm_agent_error``), unchanged since M4.

M7 addition: every specialist node's work now runs inside
``backend.observability.traced_span``, which emits a ``span.start`` event
before the node's work begins and a ``span.end`` event (with measured
``latency_ms`` and an "ok"/"error" outcome) after it finishes or raises.
``aggregate_node`` additionally emits one ``decision`` event recording the
final ``status``/``overall_confidence``.

M8 INFRASTRUCTURE-FAILURE FIX, GENERALIZED AT M10: M8's L2 DEBUG fix
narrowed ``security_node``'s exception handling so a ``BudgetGuard`` block
(or any other infrastructure/availability failure --
``LLMConfigurationError``, ``LLMCallFailedError``) returns ONE synthetic
CRITICAL/confidence-0.000 forced-HITL Finding instead of an empty findings
list -- because an empty list is indistinguishable from "the model ran and
genuinely found nothing to flag", and that gap was masked only because the
three remaining stub specialists kept ``overall_confidence`` below the HITL
threshold regardless. M8's own L4 VERIFY flagged, by name, that the masking
would disappear once M10 made the other three real -- this module's own
``quality_node``/``tests_node``/``docs_node`` now apply the EXACT SAME
narrow-catch-and-fallback treatment ``security_node`` already used, via the
same generic mechanism (``backend.agents.base_agent.
infrastructure_failure_fallback_finding``, generalized from SECURITY-only
to any ``AgentType`` -- see that module's docstring). All four specialist
nodes share one implementation of this (``_run_specialist`` /
``_INFRASTRUCTURE_FAILURE_EXCEPTIONS``), so there is exactly one place this
policy is defined, not four copies that could silently drift apart.

M10 RETRIEVAL WIRING: each real agent (constructed by this module's
``_get_<agent>_agent`` lazy singletons) is given a real
``backend.memory.context_retriever.HybridRetriever`` (``_get_retriever``),
so every specialist's prompt is grounded with retrieved repository context,
not just the raw diff -- see ``backend.agents.base_agent``'s module
docstring for the query strategy and per-agent context budget. Test
overrides (``set_<agent>_agent_for_testing``) install a full ``BaseAgent``
replacement, which bypasses this retriever entirely -- exactly how M8's
security override already worked with the real ``AnthropicLLMClient``, so
no unit test in this project needs a real pgvector connection.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from langchain_core.runnables import RunnableConfig

from backend.agents.base_agent import (
    AGENT_NAME_BY_TYPE,
    BaseAgent,
    RetrieverProtocol,
    infrastructure_failure_fallback_finding,
)
from backend.agents.contracts import dedupe_findings
from backend.agents.docs_agent import DocsAgent
from backend.agents.quality_agent import QualityAgent
from backend.agents.security_agent import SecurityAgent
from backend.agents.test_agent import TestsAgent
from backend.core.settings import get_settings
from backend.database.review_store import get_review_repository, persist_review
from backend.economics.budget import BudgetExceededError
from backend.hitl.queue import route_review
from backend.memory.context_retriever import HybridRetriever
from backend.memory.embedder import get_embedder
from backend.models import AgentType, Finding, Review, Severity, compute_overall_confidence
from backend.observability import emit_decision, get_event_repository, traced_span
from backend.orchestrator.state import GraphState
from backend.tools.llm_client import LLMCallFailedError, LLMConfigurationError

# Simulated per-node work duration -- still used by test doubles that stand
# in for a specialist (e.g. tests/integration/test_orchestrator_fanout.py's
# stub-equivalent fakes) to reproduce M4's original timing characteristics,
# even though no *production* node code sleeps for a fixed duration anymore
# (a real LLM call's latency stands in for it).
NODE_WORK_SECONDS = 0.2


class SimulatedNodeCrashError(RuntimeError):
    """Raised by a node when a test has armed a simulated worker crash.

    Deliberately a distinct type from ``AgentExecutionError``: node wrapper
    functions let this one propagate uncaught (it represents the whole
    worker process dying, which must fail the graph run so a checkpoint/
    resume cycle can be exercised), whereas ``AgentExecutionError`` is caught
    and isolated per node.
    """


class AgentExecutionError(RuntimeError):
    """Raised (internally) when a specialist's own work fails.

    Represents a normal, isolated agent-level failure. Node wrapper
    functions catch this and record it in ``GraphState.node_errors``
    instead of letting it fail the whole run: one bad specialist must not
    cost the other three their findings.
    """


# ---------------------------------------------------------------------------
# Instrumentation: thread-safe, keyed by (thread_id, node_name). LangGraph
# runs a super-step's parallel branches concurrently (a thread pool for sync
# graphs), so every one of these structures must be safe for concurrent
# read/write from multiple specialist nodes at once.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_call_counts: dict[tuple[str, str], int] = {}
_execution_windows: dict[tuple[str, str], list[tuple[float, float]]] = {}
_armed_crashes: dict[tuple[str, str], int] = {}
_armed_errors: dict[tuple[str, str], int] = {}


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
        _armed_crashes[(thread_id, node_name)] = _armed_crashes.get((thread_id, node_name), 0) + (
            times
        )


def arm_agent_error(thread_id: str, node_name: str, *, times: int = 1) -> None:
    """Test-only: make ``node_name`` raise (and internally isolate) an error."""
    with _lock:
        _armed_errors[(thread_id, node_name)] = _armed_errors.get((thread_id, node_name), 0) + times


def call_count(thread_id: str, node_name: str) -> int:
    """Number of times ``node_name`` has actually executed its body for this run."""
    with _lock:
        return _call_counts.get((thread_id, node_name), 0)


def execution_windows(thread_id: str, node_name: str) -> list[tuple[float, float]]:
    """(start, end) ``time.monotonic()`` windows for each successful run."""
    with _lock:
        return list(_execution_windows.get((thread_id, node_name), []))


# ---------------------------------------------------------------------------
# M8/M10: which BaseAgent each specialist node delegates to, generalized
# from M8's SECURITY-only override mechanism to all four AgentTypes. One
# dict keyed by AgentType instead of four near-identical globals, so a new
# specialist (were one ever added) is a one-line addition, not a fourth
# copy-pasted pair of functions.
# ---------------------------------------------------------------------------

_agent_overrides: dict[AgentType, BaseAgent | None] = {}
_real_agents: dict[AgentType, BaseAgent] = {}
_real_retriever: RetrieverProtocol | None = None

# Concrete BaseAgent subclass to lazily construct for each AgentType, when
# no test override is installed. Each constructor takes the same
# (llm_client=None, *, prompt_version=..., retriever=...) shape.
_AGENT_CLASSES: dict[AgentType, Callable[..., BaseAgent]] = {
    AgentType.SECURITY: SecurityAgent,
    AgentType.QUALITY: QualityAgent,
    AgentType.TESTS: TestsAgent,
    AgentType.DOCS: DocsAgent,
}


def set_security_agent_for_testing(agent: BaseAgent | None) -> None:
    """Test-only: override which ``BaseAgent`` ``security_node`` delegates to.

    Production code never calls this. Pass ``None`` to clear a previously
    installed override and fall back to the real, lazily-constructed agent.
    """
    _set_agent_override_for_testing(AgentType.SECURITY, agent)


def set_quality_agent_for_testing(agent: BaseAgent | None) -> None:
    """Test-only: override which ``BaseAgent`` ``quality_node`` delegates to. See ``set_security_agent_for_testing``."""
    _set_agent_override_for_testing(AgentType.QUALITY, agent)


def set_tests_agent_for_testing(agent: BaseAgent | None) -> None:
    """Test-only: override which ``BaseAgent`` ``tests_node`` delegates to. See ``set_security_agent_for_testing``."""
    _set_agent_override_for_testing(AgentType.TESTS, agent)


def set_docs_agent_for_testing(agent: BaseAgent | None) -> None:
    """Test-only: override which ``BaseAgent`` ``docs_node`` delegates to. See ``set_security_agent_for_testing``."""
    _set_agent_override_for_testing(AgentType.DOCS, agent)


def _set_agent_override_for_testing(agent_type: AgentType, agent: BaseAgent | None) -> None:
    if agent is None:
        _agent_overrides.pop(agent_type, None)
    else:
        _agent_overrides[agent_type] = agent


def _get_retriever() -> RetrieverProtocol:
    """Lazily construct and cache ONE real ``HybridRetriever`` for this process.

    Mirrors ``_get_agent``'s laziness for the LLM client: constructing a
    ``HybridRetriever`` performs no I/O of its own (each of its methods
    opens its own short-lived connection only when actually called -- see
    that class's docstring), so this is safe to reach with no pgvector
    container running at all; only an actual ``.hybrid_search()`` call
    would need one reachable, and
    ``backend.agents.base_agent.build_user_message`` already treats a
    retrieval failure as non-fatal (falls back to the diff alone -- see
    that function's docstring for why).

    No test-override hook exists for this specifically because every test
    in this project that cares about a specialist node installs a full
    ``BaseAgent`` override (``set_<agent>_agent_for_testing``), which
    bypasses this function entirely -- the same reason
    ``_get_security_agent``'s real ``AnthropicLLMClient()`` construction
    was never exercised directly by M8's own test suite either.
    """
    global _real_retriever
    if _real_retriever is None:
        settings = get_settings()
        # M12: settings.effective_pgvector_url routes at the real Tiger
        # Cloud DiskANN-backed code_chunks when MEMORY_BACKEND=tiger,
        # unchanged (local pgvector) behavior otherwise -- see that
        # property's docstring.
        _real_retriever = HybridRetriever(settings.effective_pgvector_url, get_embedder(settings))
    return _real_retriever


def _get_agent(agent_type: AgentType) -> BaseAgent:
    """Return the ``BaseAgent`` the node for ``agent_type`` should delegate to this call.

    Returns the test-installed override if one is set, otherwise lazily
    constructs and caches ONE real agent instance (with a real retriever
    for grounding -- see ``_get_retriever``) for this process. Lazy
    construction is what lets importing this module -- and running every
    test that always installs an override before invoking the graph --
    work with no ``ANTHROPIC_API_KEY``/pgvector at all.
    """
    override = _agent_overrides.get(agent_type)
    if override is not None:
        return override
    if agent_type not in _real_agents:
        agent_class = _AGENT_CLASSES[agent_type]
        _real_agents[agent_type] = agent_class(retriever=_get_retriever())
    return _real_agents[agent_type]


def _thread_id_from_config(config: RunnableConfig) -> str:
    configurable = config.get("configurable") or {}
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("stub node invoked without a configurable.thread_id")
    return thread_id


# M8 L2 DEBUG (post-L4-VERIFY), generalized at M10: the specific "the
# specialist could not even attempt or complete its analysis" exceptions
# every specialist node must turn into a forced-HITL fallback Finding,
# never an empty findings list -- see this module's own docstring for the
# full defect this fixes and why it now applies identically to all four
# specialists, not just SECURITY. Deliberately narrow (mirroring how M7's
# own events-failure-policy swallow was narrowed from bare
# `(psycopg.Error, OSError)` down to exactly the availability exceptions
# that mean "the dependency, not our code, failed"): a genuine programming
# bug in our own code (a TypeError, a KeyError from a real defect) is NOT
# in this tuple, so it propagates out of the node uncaught, exactly like
# SimulatedNodeCrashError already does, instead of being silently
# reinterpreted as "the specialist is unavailable".
_INFRASTRUCTURE_FAILURE_EXCEPTIONS: tuple[type[Exception], ...] = (
    BudgetExceededError,
    LLMConfigurationError,
    LLMCallFailedError,
)


def _run_specialist(node_name: str, agent_type: AgentType, config: RunnableConfig, diff: str) -> list[Finding]:
    """Shared body for all four specialist nodes: instrumentation + delegate to the real agent.

    Records the call, honors any armed crash/error for this
    ``(thread_id, node_name)``, times the real (or fake) agent's work, and
    returns its findings. Generalizes M8's ``_run_security`` to all four
    agent types -- see this module's docstring.
    """
    thread_id = _thread_id_from_config(config)

    with _lock:
        _call_counts[(thread_id, node_name)] = _call_counts.get((thread_id, node_name), 0) + 1
        should_crash = _armed_crashes.get((thread_id, node_name), 0) > 0
        if should_crash:
            _armed_crashes[(thread_id, node_name)] -= 1
        should_error = _armed_errors.get((thread_id, node_name), 0) > 0
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
    findings = _get_agent(agent_type).analyze(diff, review_id=thread_id)
    end = time.monotonic()
    with _lock:
        _execution_windows.setdefault((thread_id, node_name), []).append((start, end))

    return findings


def _specialist_node(agent_type: AgentType, state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """Shared node body: run the specialist, isolate its failures, force HITL on infrastructure failure.

    Every one of the four exported node functions below
    (``security_node``/``quality_node``/``tests_node``/``docs_node``) is a
    one-line wrapper around this, parameterized only by ``AgentType`` --
    see this module's docstring for why the M8 infrastructure-failure
    treatment (previously SECURITY-only) is now identical for all four.
    """
    node_name = AGENT_NAME_BY_TYPE[agent_type]
    with traced_span(get_event_repository(), state["review_id"], node_name):
        try:
            findings = _run_specialist(node_name, agent_type, config, state.get("diff", ""))
        except SimulatedNodeCrashError:
            raise
        except AgentExecutionError as exc:
            return {"findings": [], "node_errors": {node_name: str(exc)}}
        except _INFRASTRUCTURE_FAILURE_EXCEPTIONS as exc:
            fallback = infrastructure_failure_fallback_finding(agent_type, exc)
            return {"findings": [fallback], "node_errors": {node_name: str(exc)}}
    return {"findings": findings, "node_errors": {}}


def security_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """Specialist: real, LLM-backed security review (M8). See module docstring."""
    return _specialist_node(AgentType.SECURITY, state, config)


def quality_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """Specialist: real, LLM-backed code-quality review (M10). See module docstring."""
    return _specialist_node(AgentType.QUALITY, state, config)


def tests_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """Specialist: real, LLM-backed test-coverage review (M10). See module docstring."""
    return _specialist_node(AgentType.TESTS, state, config)


def docs_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """Specialist: real, LLM-backed documentation review (M10). See module docstring."""
    return _specialist_node(AgentType.DOCS, state, config)


# Canned, deterministic findings -- one per specialist. No longer used by
# any *production* node (all four now delegate to a real agent -- see
# module docstring), but kept as a shared fixture for tests that need a
# stub-equivalent BaseAgent double reproducing M4's exact original
# behavior (e.g. tests/integration/test_orchestrator_fanout.py's autouse
# fixture) without depending on a real LLM call.
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


def aggregate_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """Join point the four specialists converge on: the real M5 aggregator.

    Fed by ``GraphState.findings`` -- every specialist's contribution
    already merged by the ``operator.add`` reducer (see ``state.py``), in
    whatever order the parallel branches happened to finish. This node:

    1. Dedupes same-``(file_path, line_start)`` findings, keeping the one
       that matters most: higher severity wins first, confidence only
       breaks a tie *within* the same severity
       (``backend.agents.contracts.dedupe_findings``).
    2. Computes ``overall_confidence`` from the *surviving* (deduped)
       findings via the one formula ``Review`` itself enforces
       (``backend.models.review.compute_overall_confidence``).
    3. Runs the HITL gate (``backend.hitl.queue.route_review``) against the
       configured threshold to decide POSTED vs. QUEUED_FOR_HITL, and builds
       the reason string explaining why.
    4. Constructs the ``Review`` and returns it (plus the reason) as a
       partial state update.

    No LLM call, no GitHub post, no queue write: those live in each real
    agent's own ``analyze`` and in ``backend.integrations.github_client`` /
    ``backend.cli.review_local`` / ``backend.job_queue.arq_worker`` (M10).
    This node's whole job ends at "here is the Review and why it was
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
        # that as optional, so it must be passed explicitly here -- the one
        # call site in backend/ that actually constructs a Review.
        error_message=None,
    )
    # M7: the events spine's live call site for the aggregator's routing
    # decision. A failure to write this event (the events Postgres
    # unreachable) is caught and logged inside emit_decision itself; it
    # never raises here and never prevents the Review above from being
    # returned.
    emit_decision(
        get_event_repository(),
        state["review_id"],
        agent="aggregator",
        outcome=status.value,
        confidence=overall_confidence,
    )
    # M13: the durable HITL-queue/dashboard read model
    # (backend.database.review_store) -- see that module's docstring for
    # why agent_events alone cannot answer "what are this review's actual
    # findings/reason". Same fail-safe posture as emit_decision above: a
    # write failure here is logged and swallowed, never allowed to fail
    # the review itself (persist_review's own docstring).
    persist_review(get_review_repository(), review, reason=reason)
    return {"review": review, "routing_reason": reason}
