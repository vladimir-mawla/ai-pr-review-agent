"""M10: BudgetGuard across four agents -- composition-level proof.

Owns proving this milestone's own instructions directly: "Four calls per
review means four guard checks. Confirm the guard is hit per call and that
a mid-review budget exhaustion is handled sanely (what happens if agents
1-2 succeed and agent 3 is blocked? The review must not silently look
complete)."

DESIGN/BEHAVIOR DECISION, recorded here (per this milestone's own
instruction to decide and document): each of the four specialist nodes
makes its OWN independent LLM call, and ``backend.tools.llm_client.
AnthropicLLMClient.complete`` calls ``BudgetGuard.check_and_raise()`` as
the first thing every single one of those four calls does (see that
class's own docstring/module docstring -- this was already true since M8,
unchanged by M10). There is no shared, review-scoped "budget already spent
this review" fast path -- each agent independently reads the SAME real
spend from ``agent_events`` and independently decides whether it is over
the cap. This means: if the cap is crossed partway through a review (e.g.
by the first two agents' own real spend), whichever of the remaining
agents' calls happen to run afterward are blocked, and -- per
``backend.orchestrator.nodes``'s shared infrastructure-failure handling
(generalized at M10 to all four specialists, not just SECURITY) -- each
blocked agent contributes ONE synthetic CRITICAL/confidence-0.000 Finding
instead of an empty list. Because ``backend.hitl.queue.
has_critical_finding`` forces ``QUEUED_FOR_HITL`` unconditionally on ANY
CRITICAL finding, a review that is "2/4 successful, 2/4 budget-blocked" can
never silently look like a complete, confident, auto-postable review --
this test proves that end to end through the real compiled graph, not just
by construction.

This test does NOT use a real Postgres-backed BudgetGuard/EventRepository
(that would need real spend to actually accumulate across four real calls,
which is unnecessarily slow and flaky to set up for what is fundamentally
an agent-isolation/routing question). Instead it uses a single, SHARED
fake LLM client instance across all four real agent objects that
deterministically raises ``BudgetExceededError`` -- the exact exception
``AnthropicLLMClient.complete`` raises when the real guard trips -- for
every call after the first ``allow_count`` succeed, and independently
counts how many calls were actually attempted (proving "the guard is hit
per call", not merely configured).
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from backend.agents.docs_agent import DocsAgent
from backend.agents.quality_agent import QualityAgent
from backend.agents.security_agent import SecurityAgent
from backend.agents.test_agent import TestsAgent
from backend.economics.budget import BudgetExceededError
from backend.models import ReviewStatus, Severity
from backend.orchestrator import nodes
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.state import GraphState
from backend.tools.llm_client import LLMResponse

_RESPONSES_BY_AGENT = {
    "security": json.dumps(
        {
            "findings": [
                {
                    "severity": "HIGH",
                    "category": "sql_injection",
                    "file_path": "app/x.py",
                    "line_start": 1,
                    "line_end": 1,
                    "confidence": "0.900",
                    "rationale": "real security finding",
                }
            ]
        }
    ),
    "quality": json.dumps(
        {
            "findings": [
                {
                    "severity": "MEDIUM",
                    "category": "excessive_complexity",
                    "file_path": "app/y.py",
                    "line_start": 1,
                    "line_end": 1,
                    "confidence": "0.900",
                    "rationale": "real quality finding",
                }
            ]
        }
    ),
    "tests": json.dumps(
        {
            "findings": [
                {
                    "severity": "MEDIUM",
                    "category": "missing_test_coverage",
                    "file_path": "app/z.py",
                    "line_start": 1,
                    "line_end": 1,
                    "confidence": "0.900",
                    "rationale": "real tests finding",
                }
            ]
        }
    ),
    "docs": json.dumps(
        {
            "findings": [
                {
                    "severity": "LOW",
                    "category": "stale_docstring",
                    "file_path": "app/w.py",
                    "line_start": 1,
                    "line_end": 1,
                    "confidence": "0.900",
                    "rationale": "real docs finding",
                }
            ]
        }
    ),
}


class _SharedBudgetTrippingLLMClient:
    """One shared fake LLM client, injected into all four real agents.

    The first ``allow_count`` calls (across ALL four agents combined, in
    whatever order LangGraph's parallel fan-out happens to invoke them)
    succeed with that agent's own canned, valid response; every call after
    that raises ``BudgetExceededError`` -- modeling a real, shared daily
    cap being crossed partway through one review's four calls. Thread-safe
    (LangGraph runs the four specialist nodes concurrently on a thread
    pool), and records exactly which agents attempted a call, in order.
    """

    def __init__(self, allow_count: int) -> None:
        self._lock = threading.Lock()
        self._calls_made = 0
        self._allow_count = allow_count
        self.call_agents: list[str] = []

    def complete(
        self,
        *,
        system: str,
        user: str,
        agent: str,
        review_id: str | None = None,
    ) -> LLMResponse:
        with self._lock:
            self._calls_made += 1
            call_index = self._calls_made
            self.call_agents.append(agent)

        if call_index > self._allow_count:
            raise BudgetExceededError(Decimal("20.00"), Decimal("20.00"))

        return LLMResponse(
            text=_RESPONSES_BY_AGENT[agent],
            model="fake-model",
            tokens_in=10,
            tokens_out=10,
            cost_usd=Decimal("0.000100"),
            latency_ms=1,
        )


def _initial_state(review_id: str) -> GraphState:
    return GraphState(
        review_id=review_id,
        pr_number=1,
        repository_owner="acme",
        repository_name="widgets",
        head_sha="a" * 40,
        findings=[],
        node_errors={},
        diff="diff --git a/x b/x\n+def f():\n+    pass\n",
    )


class TestBudgetGuardHitPerCallAcrossFourAgents:
    """The guard is checked independently by each of the four calls, not once for the review."""

    def test_all_four_agents_independently_attempt_a_call(self, tmp_path: Path) -> None:
        shared_client = _SharedBudgetTrippingLLMClient(allow_count=4)  # nothing blocked
        nodes.set_security_agent_for_testing(SecurityAgent(shared_client))
        nodes.set_quality_agent_for_testing(QualityAgent(shared_client))
        nodes.set_tests_agent_for_testing(TestsAgent(shared_client))
        nodes.set_docs_agent_for_testing(DocsAgent(shared_client))

        thread_id = str(uuid4())
        engine = LangGraphWorkflowEngine(tmp_path / "checkpoints.sqlite3")
        try:
            engine.run(thread_id, _initial_state(thread_id))
        finally:
            engine.close()
            for setter in (
                nodes.set_security_agent_for_testing,
                nodes.set_quality_agent_for_testing,
                nodes.set_tests_agent_for_testing,
                nodes.set_docs_agent_for_testing,
            ):
                setter(None)

        assert sorted(shared_client.call_agents) == ["docs", "quality", "security", "tests"], (
            "expected exactly one call attempted per agent -- the guard "
            "must be checked on every one of the four calls, not once per review"
        )


class TestMidReviewBudgetExhaustionIsHandledSanely:
    """Agents 1-2 succeed, agents 3-4 are blocked -- the review must never look complete/clean."""

    def test_partial_budget_exhaustion_forces_hitl_not_a_silent_auto_post(
        self, tmp_path: Path
    ) -> None:
        # Only the first 2 of the 4 concurrent calls succeed; the rest are
        # blocked -- deterministically models "agents 1-2 succeed, agent 3
        # (and, here, 4) is blocked" from this milestone's own instructions.
        shared_client = _SharedBudgetTrippingLLMClient(allow_count=2)
        nodes.set_security_agent_for_testing(SecurityAgent(shared_client))
        nodes.set_quality_agent_for_testing(QualityAgent(shared_client))
        nodes.set_tests_agent_for_testing(TestsAgent(shared_client))
        nodes.set_docs_agent_for_testing(DocsAgent(shared_client))

        thread_id = str(uuid4())
        engine = LangGraphWorkflowEngine(tmp_path / "checkpoints.sqlite3")
        try:
            result = engine.run(thread_id, _initial_state(thread_id))
        finally:
            engine.close()
            for setter in (
                nodes.set_security_agent_for_testing,
                nodes.set_quality_agent_for_testing,
                nodes.set_tests_agent_for_testing,
                nodes.set_docs_agent_for_testing,
            ):
                setter(None)

        # (1) The guard was hit by all four calls -- 2 allowed through, 2 blocked.
        assert len(shared_client.call_agents) == 4

        findings = result["findings"]
        real_findings = [f for f in findings if f.confidence != Decimal("0.000")]
        fallback_findings = [f for f in findings if f.confidence == Decimal("0.000")]

        # (2) Exactly 2 real findings (the allowed calls) and 2 forced-HITL
        # fallbacks (the blocked calls) -- the run is genuinely "half real,
        # half infrastructure-failure", not silently collapsed to one or
        # the other.
        assert len(real_findings) == 2
        assert len(fallback_findings) == 2
        for fallback in fallback_findings:
            assert fallback.severity == Severity.CRITICAL
            assert "budget" in fallback.rationale.lower() or "BudgetExceededError" in fallback.rationale

        # (3) node_errors records exactly the two blocked specialists.
        assert len(result["node_errors"]) == 2

        # (4) The headline assertion: the review must NEVER silently look
        # complete/auto-postable when the budget ran out partway through --
        # QUEUED_FOR_HITL is forced by the CRITICAL fallback(s), regardless
        # of how confident the two real findings were.
        review = result["review"]
        assert review is not None
        assert review.status == ReviewStatus.QUEUED_FOR_HITL
        assert len(review.findings) == 4  # dedup is a no-op here: 4 distinct file paths
