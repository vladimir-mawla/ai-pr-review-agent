"""M10: infrastructure-failure handling, generalized to all four specialist nodes.

``tests/unit/test_security_node_infrastructure_failures.py`` (M8 L2 DEBUG)
proved this fix for SECURITY specifically, and its own docstring named the
exact defect that would reappear once M10 made the other three specialists
real: "today it's masked only because the three remaining stub specialists
keep overall_confidence below the HITL threshold regardless... once M10
makes them real, a budget block would silently read as a clean [specialist]
review."

This file is that follow-through: it proves ``quality_node``/``tests_node``/
``docs_node`` apply the EXACT SAME narrow-catch-and-forced-HITL-fallback
treatment ``security_node`` already used, via
``backend.orchestrator.nodes._specialist_node`` (the shared body all four
node functions now delegate to -- see that module's docstring). Parametrized
over all four agent types so there is one test body proving the policy is
identical everywhere, not four hand-copied near-duplicates that could
silently drift apart from each other.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
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

_NODE_FUNCTIONS: dict[AgentType, Callable[[GraphState, dict[str, Any]], dict[str, Any]]] = {
    AgentType.SECURITY: nodes.security_node,
    AgentType.QUALITY: nodes.quality_node,
    AgentType.TESTS: nodes.tests_node,
    AgentType.DOCS: nodes.docs_node,
}

_SETTERS: dict[AgentType, Callable[[BaseAgent | None], None]] = {
    AgentType.SECURITY: nodes.set_security_agent_for_testing,
    AgentType.QUALITY: nodes.set_quality_agent_for_testing,
    AgentType.TESTS: nodes.set_tests_agent_for_testing,
    AgentType.DOCS: nodes.set_docs_agent_for_testing,
}

_ALL_AGENT_TYPES = (AgentType.SECURITY, AgentType.QUALITY, AgentType.TESTS, AgentType.DOCS)


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


class _RaisingAgent(BaseAgent):
    """A fake specialist agent whose analyze() always raises a fixed exception."""

    def __init__(self, agent_type: AgentType, exc: BaseException) -> None:
        self.agent_type = agent_type
        self._exc = exc

    def analyze(self, diff: str, *, review_id: str | None = None) -> list[Finding]:
        raise self._exc


class _EmptyFindingsAgent(BaseAgent):
    """A fake specialist agent that completes normally and genuinely finds nothing."""

    def __init__(self, agent_type: AgentType) -> None:
        self.agent_type = agent_type

    def analyze(self, diff: str, *, review_id: str | None = None) -> list[Finding]:
        return []


def _unrelated_confident_finding(exclude: AgentType) -> Finding:
    """A stand-in confident finding from an agent OTHER than ``exclude``."""
    other = next(agent_type for agent_type in _ALL_AGENT_TYPES if agent_type != exclude)
    return Finding(
        agent_type=other,
        severity=Severity.LOW,
        category="stub_finding",
        file_path=f"stub/{other.value.lower()}.py",
        line_start=1,
        line_end=1,
        confidence=Decimal("0.950"),
        rationale="a confident, unrelated finding from a different specialist",
    )


@pytest.fixture(autouse=True)
def _clear_all_overrides() -> Iterator[None]:
    yield
    for setter in _SETTERS.values():
        setter(None)


@pytest.mark.parametrize("agent_type", _ALL_AGENT_TYPES)
class TestInfrastructureFailureForcesHITLForEveryAgent:
    """A budget block (or any infrastructure failure) forces HITL, for EVERY specialist."""

    def test_returns_one_forced_hitl_critical_finding(self, agent_type: AgentType) -> None:
        _SETTERS[agent_type](
            _RaisingAgent(agent_type, BudgetExceededError(Decimal("20"), Decimal("20")))
        )
        node_func = _NODE_FUNCTIONS[agent_type]
        result = node_func(_state(f"review-{agent_type.value}-1"), _config(f"thread-{agent_type.value}-1"))

        findings = result["findings"]
        assert len(findings) == 1
        assert findings[0].severity == Severity.CRITICAL
        assert findings[0].confidence == Decimal("0.000")
        assert findings[0].agent_type == agent_type

    def test_the_failure_is_recorded_in_node_errors(self, agent_type: AgentType) -> None:
        _SETTERS[agent_type](
            _RaisingAgent(agent_type, BudgetExceededError(Decimal("20"), Decimal("20")))
        )
        node_func = _NODE_FUNCTIONS[agent_type]
        node_name = agent_type.value.lower()
        result = node_func(_state(f"review-{agent_type.value}-2"), _config(f"thread-{agent_type.value}-2"))

        assert node_name in result["node_errors"]
        assert result["node_errors"][node_name]
        assert "budget" in result["node_errors"][node_name].lower()

    def test_the_forced_finding_routes_the_review_to_hitl_even_with_a_confident_peer(
        self, agent_type: AgentType
    ) -> None:
        """The end-to-end proof, for every agent: a budget block must never
        be able to average out into an auto-post, no matter which
        specialist hit it.
        """
        _SETTERS[agent_type](
            _RaisingAgent(agent_type, BudgetExceededError(Decimal("20"), Decimal("20")))
        )
        node_func = _NODE_FUNCTIONS[agent_type]
        result = node_func(_state(f"review-{agent_type.value}-3"), _config(f"thread-{agent_type.value}-3"))

        all_findings = result["findings"] + [_unrelated_confident_finding(agent_type)]
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
    def test_other_infrastructure_failures_also_force_hitl(
        self, agent_type: AgentType, exc: Exception
    ) -> None:
        _SETTERS[agent_type](_RaisingAgent(agent_type, exc))
        node_func = _NODE_FUNCTIONS[agent_type]
        result = node_func(_state(f"review-{agent_type.value}-infra"), _config(f"thread-{agent_type.value}-infra"))

        findings = result["findings"]
        assert len(findings) == 1
        assert findings[0].severity == Severity.CRITICAL


@pytest.mark.parametrize("agent_type", _ALL_AGENT_TYPES)
class TestGenuineEmptyFindingsStillRoutesNormallyForEveryAgent:
    def test_returns_a_real_empty_list_not_a_fallback(self, agent_type: AgentType) -> None:
        _SETTERS[agent_type](_EmptyFindingsAgent(agent_type))
        node_func = _NODE_FUNCTIONS[agent_type]
        result = node_func(_state(f"review-empty-{agent_type.value}"), _config(f"thread-empty-{agent_type.value}"))

        assert result["findings"] == []
        assert result["node_errors"] == {}


@pytest.mark.parametrize("agent_type", _ALL_AGENT_TYPES)
class TestProgrammingBugPropagatesForEveryAgent:
    """A genuine bug in our own code must surface, not be swallowed, for every specialist."""

    def test_a_type_error_is_not_swallowed(self, agent_type: AgentType) -> None:
        _SETTERS[agent_type](_RaisingAgent(agent_type, TypeError("real bug: bad argument")))
        node_func = _NODE_FUNCTIONS[agent_type]

        with pytest.raises(TypeError, match="real bug"):
            node_func(_state(f"review-bug-{agent_type.value}"), _config(f"thread-bug-{agent_type.value}"))
