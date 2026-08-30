"""PLAN.md's named M8 unit-test file: schema validation of the security agent's LLM output.

No test here requires ANTHROPIC_API_KEY -- every ``SecurityAgent`` is
constructed with a fake ``LLMClientProtocol`` implementation returning a
fixed response text, never a real ``AnthropicLLMClient``.

Covers, per this milestone's own instructions:
- A well-formed response parses into real ``Finding`` objects.
- Every named drift case (key drift, markdown-fenced JSON, prose-then-JSON,
  a bare list) still parses correctly, at both the parser level
  (``backend.agents.response_parsing``) and through the full
  ``SecurityAgent.analyze`` path.
- Total garbage produces a forced-HITL CRITICAL fallback ``Finding``, never
  a crash and never a silently empty/dropped result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from backend.agents.response_parsing import ResponseParseError, parse_findings_from_llm_response
from backend.agents.security_agent import SecurityAgent
from backend.models import AgentType, Severity
from backend.tools.llm_client import LLMResponse

_WELL_FORMED_FINDING = {
    "severity": "HIGH",
    "category": "sql_injection",
    "file_path": "app/db.py",
    "line_start": 42,
    "line_end": 44,
    "confidence": "0.850",
    "rationale": "User input is interpolated directly into a SQL query without parameterization.",
}


@dataclass
class _FakeLLMClient:
    """Minimal ``LLMClientProtocol`` implementation: always returns the same fixed text."""

    response_text: str
    calls: list[dict[str, object]] | None = None

    def complete(
        self,
        *,
        system: str,
        user: str,
        agent: str,
        review_id: str | None = None,
    ) -> LLMResponse:
        if self.calls is not None:
            self.calls.append(
                {"system": system, "user": user, "agent": agent, "review_id": review_id}
            )
        return LLMResponse(
            text=self.response_text,
            model="claude-haiku-4-5",
            tokens_in=100,
            tokens_out=50,
            cost_usd=Decimal("0.000350"),
            latency_ms=10,
        )


# ---------------------------------------------------------------------------
# Parser-level drift tests (backend.agents.response_parsing), with explicit
# fixture strings for each named drift case.
# ---------------------------------------------------------------------------


class TestParserWellFormedResponse:
    def test_well_formed_findings_key_parses(self) -> None:
        raw = json.dumps({"findings": [_WELL_FORMED_FINDING]})
        findings = parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.agent_type == AgentType.SECURITY
        assert finding.severity == Severity.HIGH
        assert finding.category == "sql_injection"
        assert finding.file_path == "app/db.py"
        assert finding.line_start == 42
        assert finding.line_end == 44
        assert finding.confidence == Decimal("0.850")


class TestParserDriftCases:
    """Each of the drift cases this milestone's instructions name explicitly."""

    def test_key_drift_issues_instead_of_findings(self) -> None:
        raw = json.dumps({"issues": [_WELL_FORMED_FINDING]})
        findings = parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        assert len(findings) == 1
        assert findings[0].category == "sql_injection"

    def test_markdown_fenced_json_with_language_tag(self) -> None:
        raw = "```json\n" + json.dumps({"findings": [_WELL_FORMED_FINDING]}) + "\n```"
        findings = parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        assert len(findings) == 1
        assert findings[0].category == "sql_injection"

    def test_markdown_fenced_json_bare_fence(self) -> None:
        raw = "```\n" + json.dumps({"findings": [_WELL_FORMED_FINDING]}) + "\n```"
        findings = parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        assert len(findings) == 1

    def test_prose_then_json(self) -> None:
        raw = (
            "Here is my security review of the diff you provided:\n\n"
            + json.dumps({"findings": [_WELL_FORMED_FINDING]})
        )
        findings = parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        assert len(findings) == 1
        assert findings[0].category == "sql_injection"

    def test_bare_list_with_no_wrapping_object(self) -> None:
        raw = json.dumps([_WELL_FORMED_FINDING])
        findings = parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        assert len(findings) == 1
        assert findings[0].category == "sql_injection"

    def test_prose_before_a_markdown_fenced_bare_list(self) -> None:
        """Two drift cases stacked: prose commentary AND a fence AND a bare list, all at once."""
        raw = (
            "Sure, here you go:\n```json\n" + json.dumps([_WELL_FORMED_FINDING]) + "\n```\nHope that helps!"
        )
        findings = parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        assert len(findings) == 1

    def test_model_does_not_need_to_supply_agent_type(self) -> None:
        """The prompt tells the model not to include agent_type -- the parser injects it."""
        item = dict(_WELL_FORMED_FINDING)
        raw = json.dumps({"findings": [item]})
        findings = parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        assert findings[0].agent_type == AgentType.SECURITY

    def test_a_model_supplied_agent_type_is_respected_if_present(self) -> None:
        item = {**_WELL_FORMED_FINDING, "agent_type": "SECURITY"}
        raw = json.dumps({"findings": [item]})
        findings = parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        assert findings[0].agent_type == AgentType.SECURITY

    def test_one_malformed_item_is_dropped_but_valid_siblings_survive(self) -> None:
        malformed = {**_WELL_FORMED_FINDING, "severity": "NOT_A_REAL_SEVERITY"}
        raw = json.dumps({"findings": [malformed, _WELL_FORMED_FINDING]})
        findings = parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        assert len(findings) == 1
        assert findings[0].category == "sql_injection"

    def test_empty_findings_list_is_valid_not_an_error(self) -> None:
        """A clean diff -- the model legitimately reports zero findings."""
        raw = json.dumps({"findings": []})
        findings = parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        assert findings == []


class TestParserTotalGarbage:
    def test_unparseable_text_raises_response_parse_error(self) -> None:
        raw = "I refuse to answer in JSON today, sorry about that."
        try:
            parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        except ResponseParseError:
            pass
        else:
            raise AssertionError("expected ResponseParseError for unparseable garbage")

    def test_json_with_no_recognizable_findings_key_raises(self) -> None:
        raw = json.dumps({"some_other_shape": {"nested": True}})
        try:
            parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        except ResponseParseError:
            pass
        else:
            raise AssertionError("expected ResponseParseError when no findings-like list exists")

    def test_all_items_invalid_raises(self) -> None:
        raw = json.dumps({"findings": [{"severity": "NOPE"}, {"totally": "wrong shape"}]})
        try:
            parse_findings_from_llm_response(raw, default_agent_type=AgentType.SECURITY)
        except ResponseParseError:
            pass
        else:
            raise AssertionError("expected ResponseParseError when every item fails validation")


# ---------------------------------------------------------------------------
# Agent-level tests: the same drift cases, exercised through SecurityAgent's
# real analyze() -> load_prompt + fake LLM client + parser pipeline.
# ---------------------------------------------------------------------------


class TestSecurityAgentAnalyze:
    def test_well_formed_response_produces_a_schema_valid_finding(self) -> None:
        raw = json.dumps({"findings": [_WELL_FORMED_FINDING]})
        agent = SecurityAgent(_FakeLLMClient(raw))
        findings = agent.analyze("--- a/app/db.py\n+++ b/app/db.py\n")
        assert len(findings) == 1
        assert findings[0].severity == Severity.HIGH
        assert findings[0].agent_type == AgentType.SECURITY

    def test_key_drift_through_the_full_agent_path(self) -> None:
        raw = json.dumps({"issues": [_WELL_FORMED_FINDING]})
        agent = SecurityAgent(_FakeLLMClient(raw))
        findings = agent.analyze("some diff")
        assert len(findings) == 1
        assert findings[0].category == "sql_injection"

    def test_markdown_fenced_through_the_full_agent_path(self) -> None:
        raw = "```json\n" + json.dumps({"findings": [_WELL_FORMED_FINDING]}) + "\n```"
        agent = SecurityAgent(_FakeLLMClient(raw))
        findings = agent.analyze("some diff")
        assert len(findings) == 1

    def test_prose_then_json_through_the_full_agent_path(self) -> None:
        raw = "Sure! " + json.dumps({"findings": [_WELL_FORMED_FINDING]})
        agent = SecurityAgent(_FakeLLMClient(raw))
        findings = agent.analyze("some diff")
        assert len(findings) == 1

    def test_bare_list_through_the_full_agent_path(self) -> None:
        raw = json.dumps([_WELL_FORMED_FINDING])
        agent = SecurityAgent(_FakeLLMClient(raw))
        findings = agent.analyze("some diff")
        assert len(findings) == 1

    def test_total_garbage_falls_back_to_a_forced_hitl_critical_finding(self) -> None:
        agent = SecurityAgent(_FakeLLMClient("not json at all, just prose forever"))
        findings = agent.analyze("some diff")

        assert len(findings) == 1
        fallback = findings[0]
        # CRITICAL is the load-bearing property: has_critical_finding()
        # forces human review unconditionally, regardless of any other
        # specialist's confidence -- see security_agent's module docstring
        # for why this, not merely "very low confidence", is the fix.
        assert fallback.severity == Severity.CRITICAL
        assert fallback.confidence == Decimal("0.000")
        assert fallback.agent_type == AgentType.SECURITY
        assert fallback.rationale  # non-empty, explains what happened

    def test_total_garbage_does_not_raise(self) -> None:
        """The whole point of the fallback: analyze() must not crash the run."""
        agent = SecurityAgent(_FakeLLMClient("{{{not json"))
        findings = agent.analyze("some diff")  # must not raise
        assert len(findings) == 1

    def test_empty_findings_list_is_a_real_empty_list_not_a_fallback(self) -> None:
        """A clean diff must not be confused with a parse failure."""
        raw = json.dumps({"findings": []})
        agent = SecurityAgent(_FakeLLMClient(raw))
        findings = agent.analyze("a clean diff")
        assert findings == []

    def test_analyze_passes_review_id_through_to_the_llm_client(self) -> None:
        calls: list[dict[str, object]] = []
        raw = json.dumps({"findings": [_WELL_FORMED_FINDING]})
        agent = SecurityAgent(_FakeLLMClient(raw, calls=calls))
        agent.analyze("some diff", review_id="review-abc")
        assert len(calls) == 1
        assert calls[0]["review_id"] == "review-abc"
        assert calls[0]["agent"] == "security"
