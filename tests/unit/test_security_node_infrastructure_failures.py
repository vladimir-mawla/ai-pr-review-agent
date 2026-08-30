"""Unit tests for backend.orchestrator.nodes.security_node's infrastructure-
failure handling (M8 L2 DEBUG, post-L4-VERIFY finding).

Owns proving the fix for a real defect: ``security_node`` used to catch
``BudgetExceededError`` (and any other genuinely-unhandled failure from
``SecurityAgent.analyze``) with a bare ``except Exception`` and turn it into
``{"findings": [], "node_errors": {"security": str(exc)}}`` -- an EMPTY
findings list, indistinguishable from "the model ran and found nothing to
flag". That is masked today only because the three remaining stub
specialists keep ``overall_confidence`` below the HITL threshold regardless
of what security reports; once M10 makes them real, a budget block would
silently read as a clean security review.

The fix narrows the catch to
``backend.orchestrator.nodes._SECURITY_INFRASTRUCTURE_FAILURE_EXCEPTIONS``
(``BudgetExceededError``, ``LLMConfigurationError``, ``LLMCallFailedError``)
and, on any of those, returns one synthetic CRITICAL/confidence-0.000
``Finding`` (``backend.agents.security_agent.
infrastructure_failure_fallback_finding``) -- reusing the exact mechanism
``SecurityAgent``'s own total-parse-failure fallback already uses
(``backend.hitl.queue.has_critical_finding``'s unconditional CRITICAL-
forces-HITL routing) -- instead of inventing a second one.

Three things are proven here, matching this milestone's instructions
exactly:
1. ``TestBudgetExceededForcesHITL`` -- a ``BudgetExceededError`` during the
   security node forces HITL (not a clean review), and is recorded in
   ``node_errors``.
2. ``TestGenuineEmptyFindingsStillRoutesNormally`` -- a real, non-error
   empty findings list from the security specialist is NOT reinterpreted as
   an infrastructure failure, and does not by itself force HITL for an
   otherwise confident review.
3. ``TestProgrammingBugPropagates`` -- an exception outside the narrow
   infrastructure-failure tuple (e.g. a ``TypeError``, standing in for a
   bug in our own code) is NOT swallowed; it propagates out of the node
   uncaught, consistent with how M7 narrowed the events failure policy to
   stop swallowing ``IntegrityError`` alongside real outages
   (``tests/unit/test_events_failure_policy.py``).

No real Postgres or LLM credential is needed: ``security_node`` wraps its
work in ``traced_span``, which itself swallows a Postgres-unavailable
failure (see ``backend.observability.events``'s own failure policy) -- so
these tests exercise the real node function directly, with a fake
``BaseAgent`` installed via the pre-existing ``set_security_agent_for_testing``
test hook, exactly the same mechanism
``tests/integration/test_orchestrator_fanout.py`` already uses.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from backend.agents.base_agent import BaseAgent
from backend.economics.budget import BudgetExceededError
from backend.hitl.queue import route_review
from backend.models import AgentType, Finding, ReviewStatus, Severity, compute_overall_confidence
from backend.orchestrator import nodes
from backend.orchestrator.state import GraphState
from backend.tools.llm_client import LLMCallFailedError, LLMConfigurationError

_THRESHOLD = Decimal("0.75")


def _state(review_id: str) -> GraphState:
    return GraphState(
        review_id=review_id,
        pr_number=1,
        repository_owner="acme",
        repository_name="widgets",
        head_sha="a" * 40,
        findings=[],
        node_errors={},
        diff="diff --git a/x b/x",
    )


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _confident_quality_finding() -> Finding:
    """A stand-in for a real, confident finding from an UNRELATED specialist --
    used to prove the routing decision genuinely depends on what security
    contributed, not merely on every other specialist already being weak.
    """
    return Finding(
        agent_type=AgentType.QUALITY,
        severity=Severity.LOW,
        category="stub_finding",
        file_path="stub/quality.py",
        line_start=1,
        line_end=1,
        confidence=Decimal("0.950"),
        rationale="a confident, unrelated finding from a different specialist",
    )


class _RaisingAgent(BaseAgent):
    """A fake SecurityAgent whose analyze() always raises a fixed exception."""

    agent_type = AgentType.SECURITY

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def analyze(self, diff: str, *, review_id: str | None = None) -> list[Finding]:
        raise self._exc


class _EmptyFindingsAgent(BaseAgent):
    """A fake SecurityAgent that completes normally and genuinely finds nothing."""

    agent_type = AgentType.SECURITY

    def analyze(self, diff: str, *, review_id: str | None = None) -> list[Finding]:
        return []


@pytest.fixture(autouse=True)
def _clear_security_agent_override() -> Iterator[None]:
    """Every test installs its own override; always clear it afterward."""
    yield
    nodes.set_security_agent_for_testing(None)


class TestBudgetExceededForcesHITL:
    """A budget block must surface as a forced-HITL signal, never a clean review."""

    def test_security_node_returns_one_forced_hitl_critical_finding(self) -> None:
        nodes.set_security_agent_for_testing(
            _RaisingAgent(BudgetExceededError(Decimal("20"), Decimal("20")))
        )
        result = nodes.security_node(_state("review-budget-1"), _config("thread-budget-1"))

        findings = result["findings"]
        assert len(findings) == 1
        assert findings[0].severity == Severity.CRITICAL
        assert findings[0].confidence == Decimal("0.000")
        assert findings[0].agent_type == AgentType.SECURITY

    def test_the_failure_is_recorded_in_node_errors(self) -> None:
        nodes.set_security_agent_for_testing(
            _RaisingAgent(BudgetExceededError(Decimal("20"), Decimal("20")))
        )
        result = nodes.security_node(_state("review-budget-2"), _config("thread-budget-2"))

        assert "security" in result["node_errors"]
        assert result["node_errors"]["security"], "the error message must not be empty/swallowed"
        assert "budget" in result["node_errors"]["security"].lower()

    def test_the_forced_finding_actually_routes_the_review_to_hitl(self) -> None:
        """The end-to-end proof: feed this Finding into the real routing
        decision alongside a confident, unrelated finding from another
        specialist. A budget block must force QUEUED_FOR_HITL even when
        every other specialist is confident -- it must never be able to
        avg out into an auto-post.
        """
        nodes.set_security_agent_for_testing(
            _RaisingAgent(BudgetExceededError(Decimal("20"), Decimal("20")))
        )
        result = nodes.security_node(_state("review-budget-3"), _config("thread-budget-3"))

        all_findings = result["findings"] + [_confident_quality_finding()]
        overall_confidence = compute_overall_confidence(all_findings)
        status, reason = route_review(overall_confidence, all_findings, threshold=_THRESHOLD)

        assert status == ReviewStatus.QUEUED_FOR_HITL
        assert "CRITICAL" in reason

    @pytest.mark.parametrize(
        "exc",
        [
            LLMConfigurationError("ANTHROPIC_API_KEY is not configured"),
            LLMCallFailedError("LLM call failed (retries exhausted or circuit breaker open)"),
        ],
    )
    def test_other_infrastructure_failures_also_force_hitl(self, exc: Exception) -> None:
        """Not just BudgetExceededError -- every infrastructure/availability
        failure SecurityAgent.analyze can raise gets the same treatment.
        """
        nodes.set_security_agent_for_testing(_RaisingAgent(exc))
        result = nodes.security_node(_state("review-infra"), _config("thread-infra"))

        findings = result["findings"]
        assert len(findings) == 1
        assert findings[0].severity == Severity.CRITICAL
        assert "security" in result["node_errors"]


class TestGenuineEmptyFindingsStillRoutesNormally:
    """A real, non-error 'nothing to flag' result must not be mistaken for a failure."""

    def test_security_node_with_no_error_returns_a_real_empty_list(self) -> None:
        nodes.set_security_agent_for_testing(_EmptyFindingsAgent())
        result = nodes.security_node(_state("review-empty-1"), _config("thread-empty-1"))

        assert result["findings"] == []
        assert result["node_errors"] == {}

    def test_empty_security_findings_do_not_force_hitl_by_themselves(self) -> None:
        """Confirms the fix does not overreach: a genuinely clean security
        result is NOT reinterpreted as an infrastructure failure and does
        not inject a synthetic CRITICAL finding -- unlike the
        BudgetExceededError case above, an otherwise-confident review
        auto-posts normally.
        """
        nodes.set_security_agent_for_testing(_EmptyFindingsAgent())
        result = nodes.security_node(_state("review-empty-2"), _config("thread-empty-2"))
        assert result["findings"] == []

        all_findings = result["findings"] + [_confident_quality_finding()]
        overall_confidence = compute_overall_confidence(all_findings)
        status, _ = route_review(overall_confidence, all_findings, threshold=_THRESHOLD)

        assert status == ReviewStatus.POSTED


class TestProgrammingBugPropagates:
    """A genuine bug in our own code must surface, not be reinterpreted as
    'the security specialist is unavailable' or silently swallowed --
    consistent with M7's narrowed events failure policy
    (tests/unit/test_events_failure_policy.py).
    """

    def test_a_type_error_from_the_agent_is_not_swallowed(self) -> None:
        nodes.set_security_agent_for_testing(_RaisingAgent(TypeError("real bug: bad argument")))

        with pytest.raises(TypeError, match="real bug"):
            nodes.security_node(_state("review-bug-1"), _config("thread-bug-1"))

    def test_a_key_error_from_the_agent_is_not_swallowed(self) -> None:
        nodes.set_security_agent_for_testing(_RaisingAgent(KeyError("unexpected-key")))

        with pytest.raises(KeyError):
            nodes.security_node(_state("review-bug-2"), _config("thread-bug-2"))
