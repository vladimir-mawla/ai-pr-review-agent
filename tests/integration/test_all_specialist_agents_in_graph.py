"""M10: composition tests -- all four REAL specialist agents through the real compiled graph.

``tests/integration/test_orchestrator_fanout.py``'s
``TestRealSecurityAgentSlotsIntoTheGraph`` proved this for SECURITY alone
at M8. This file proves it for all four at once, and specifically targets
the M5 lesson this project's own build instructions keep repeating ("the
interaction between correctly-tested halves is where the last blocking
bugs lived"): each agent's own unit tests
(``tests/unit/test_specialist_agents.py``) and the aggregator's own unit
tests (``tests/unit/test_aggregator.py``) both pass in isolation, but
neither one proves that four REAL agent objects' outputs, produced by
running the actual compiled LangGraph graph, dedupe and route correctly
together. That composition is what this file drives, with no
``ANTHROPIC_API_KEY`` anywhere (a per-agent fake LLM client is installed
for each of the four).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from backend.agents.docs_agent import DocsAgent
from backend.agents.quality_agent import QualityAgent
from backend.agents.security_agent import SecurityAgent
from backend.agents.test_agent import TestsAgent
from backend.models import AgentType, ReviewStatus, Severity
from backend.orchestrator import nodes
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.state import GraphState
from backend.tools.llm_client import LLMResponse

_COLLIDING_FILE_PATH = "app/x.py"
_COLLIDING_LINE = 5


def _response_for(severity: str, category: str) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "severity": severity,
                    "category": category,
                    "file_path": _COLLIDING_FILE_PATH,
                    "line_start": _COLLIDING_LINE,
                    "line_end": _COLLIDING_LINE,
                    "confidence": "0.900",
                    "rationale": f"a real {category} finding, deliberately colliding on the same location",
                }
            ]
        }
    )


class _FixedResponseLLMClient:
    def __init__(self, response_text: str) -> None:
        self._response_text = response_text

    def complete(
        self,
        *,
        system: str,
        user: str,
        agent: str,
        review_id: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            text=self._response_text,
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


class TestAggregationDedupesAcrossFourRealAgentsOutputs:
    """Four REAL agents, all reporting on the exact same (file_path, line_start).

    Severity precedence (CRITICAL beats MEDIUM beats LOW beats INFO -- see
    ``backend.agents.contracts.dedupe_findings``) must survive going
    through four real, independently-constructed agent objects and the
    real parallel fan-out, not just a hand-built list of Finding fixtures.
    """

    def test_only_the_highest_severity_real_finding_survives_dedupe(
        self, tmp_path: Path
    ) -> None:
        nodes.set_security_agent_for_testing(
            SecurityAgent(_FixedResponseLLMClient(_response_for("CRITICAL", "sql_injection")))
        )
        nodes.set_quality_agent_for_testing(
            QualityAgent(_FixedResponseLLMClient(_response_for("MEDIUM", "excessive_complexity")))
        )
        nodes.set_tests_agent_for_testing(
            TestsAgent(_FixedResponseLLMClient(_response_for("LOW", "missing_test_coverage")))
        )
        nodes.set_docs_agent_for_testing(
            DocsAgent(_FixedResponseLLMClient(_response_for("INFO", "stale_docstring")))
        )

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

        # Pre-dedupe: all four real agents' raw contributions are present
        # in GraphState (the operator.add reducer merged them).
        assert len(result["findings"]) == 4
        assert {f.agent_type for f in result["findings"]} == {
            AgentType.SECURITY,
            AgentType.QUALITY,
            AgentType.TESTS,
            AgentType.DOCS,
        }

        # Post-dedupe (the real aggregator's actual output): exactly one
        # finding survives, and it is SECURITY's CRITICAL one -- not
        # whichever branch happened to finish last in the parallel fan-out.
        review = result["review"]
        assert review is not None
        assert len(review.findings) == 1
        assert review.findings[0].agent_type == AgentType.SECURITY
        assert review.findings[0].severity == Severity.CRITICAL
        assert review.findings[0].category == "sql_injection"

        # And the HITL gate routes correctly given that surviving CRITICAL
        # finding: forced to human review, unconditionally.
        assert review.status == ReviewStatus.QUEUED_FOR_HITL


class TestHitlGateRoutesCorrectlyWithFourRealAgentsContributing:
    """No collisions this time -- four distinct, confident, non-CRITICAL findings should auto-post."""

    def test_four_confident_non_critical_findings_auto_post(self, tmp_path: Path) -> None:
        def response_at(line: int, severity: str, category: str) -> str:
            return json.dumps(
                {
                    "findings": [
                        {
                            "severity": severity,
                            "category": category,
                            "file_path": f"app/{category}.py",
                            "line_start": line,
                            "line_end": line,
                            "confidence": "0.950",
                            "rationale": f"a confident {category} finding",
                        }
                    ]
                }
            )

        nodes.set_security_agent_for_testing(
            SecurityAgent(_FixedResponseLLMClient(response_at(1, "LOW", "minor_security_nit")))
        )
        nodes.set_quality_agent_for_testing(
            QualityAgent(_FixedResponseLLMClient(response_at(2, "LOW", "minor_quality_nit")))
        )
        nodes.set_tests_agent_for_testing(
            TestsAgent(_FixedResponseLLMClient(response_at(3, "LOW", "minor_tests_nit")))
        )
        nodes.set_docs_agent_for_testing(
            DocsAgent(_FixedResponseLLMClient(response_at(4, "LOW", "minor_docs_nit")))
        )

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

        review = result["review"]
        assert review is not None
        assert len(review.findings) == 4  # four distinct file paths -- no dedup collision
        assert review.overall_confidence == Decimal("0.950")
        assert review.status == ReviewStatus.POSTED
