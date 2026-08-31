"""Tests for the judge's parsing/prompt-building logic and ``AnthropicJudge`` wired to a fake client.

FREE -- every test here injects a fake ``LLMClientProtocol``-shaped object
(mirroring ``tests/unit/test_security_agent_schema.py``'s own precedent for
testing an LLM-backed class without a real Anthropic call), never a real
``AnthropicLLMClient``. The real, billable judge is exercised only in
``tests/eval/test_regression_gate.py``'s ``@pytest.mark.live`` class.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from backend.evaluation.golden_dataset import ExpectedFinding, GoldenCase
from backend.evaluation.judge import (
    AnthropicJudge,
    JudgeParseError,
    build_judge_prompt,
    judge_settings,
    parse_judge_response,
)
from backend.models import Finding, Review, ReviewStatus, compute_overall_confidence
from backend.tools.llm_client import LLMResponse


def _case(case_id: str = "case-1", expected_clean: bool = False) -> GoldenCase:
    return GoldenCase(
        case_id=case_id,
        description="a test case",
        diff_path="tests/fixtures/clean_diff.patch",
        expected_clean=expected_clean,
        expected_findings=[]
        if expected_clean
        else [
            ExpectedFinding(
                category="sql_injection",
                severity="CRITICAL",
                file_path="app/db.py",
                must_detect=True,
            )
        ],
        diff_text="+ some diff text",
    )


def _review(findings: list[Finding] | None = None) -> Review:
    findings = findings or []
    from datetime import UTC, datetime

    confidence = compute_overall_confidence(findings)
    return Review(
        review_id="r-1",
        pr_number=1,
        repository_owner="o",
        repository_name="r",
        head_sha="0" * 40,
        findings=findings,
        overall_confidence=confidence,
        status=ReviewStatus.QUEUED_FOR_HITL,
        created_at=datetime.now(UTC),
        error_message=None,
    )


def _finding(category: str = "sql_injection", severity: str = "CRITICAL") -> Finding:
    return Finding(
        agent_type="SECURITY",
        severity=severity,
        category=category,
        file_path="app/db.py",
        line_start=1,
        line_end=1,
        confidence=Decimal("0.900"),
        rationale="test finding",
    )


class TestParseJudgeResponse:
    def test_parses_plain_json(self):
        verdict = parse_judge_response('{"score": 0.85, "rationale": "caught the sqli"}')
        assert verdict.score == Decimal("0.850")
        assert verdict.rationale == "caught the sqli"

    def test_parses_json_wrapped_in_code_fence(self):
        raw = '```json\n{"score": 0.4, "rationale": "missed it"}\n```'
        verdict = parse_judge_response(raw)
        assert verdict.score == Decimal("0.400")

    def test_clamps_out_of_range_score(self):
        verdict = parse_judge_response('{"score": 1.5, "rationale": "x"}')
        assert verdict.score == Decimal("1.000")
        verdict2 = parse_judge_response('{"score": -0.2, "rationale": "x"}')
        assert verdict2.score == Decimal("0.000")

    def test_raises_on_non_json(self):
        with pytest.raises(JudgeParseError):
            parse_judge_response("not json at all")

    def test_raises_on_missing_score(self):
        with pytest.raises(JudgeParseError):
            parse_judge_response('{"rationale": "no score field"}')

    def test_raises_on_empty_rationale(self):
        with pytest.raises(JudgeParseError):
            parse_judge_response('{"score": 0.5, "rationale": ""}')

    def test_raises_on_non_numeric_score(self):
        with pytest.raises(JudgeParseError):
            parse_judge_response('{"score": "high", "rationale": "x"}')


class TestBuildJudgePrompt:
    def test_prompt_includes_case_id_and_diff_and_findings(self):
        case = _case()
        review = _review([_finding()])
        prompt = build_judge_prompt(case, review)
        assert "case-1" in prompt
        assert "some diff text" in prompt
        assert "sql_injection" in prompt
        assert "CRITICAL" in prompt


@dataclass
class _FakeLLMClient:
    """Satisfies ``LLMClientProtocol`` without touching the real Anthropic SDK."""

    response_text: str
    captured_system: str | None = None
    captured_user: str | None = None

    def complete(self, *, system, user, agent, review_id=None):
        self.captured_system = system
        self.captured_user = user
        return LLMResponse(
            text=self.response_text,
            model="claude-sonnet-5",
            tokens_in=10,
            tokens_out=5,
            cost_usd=Decimal("0.000100"),
            latency_ms=42,
        )


class TestAnthropicJudge:
    def test_score_review_parses_the_fake_clients_response(self):
        fake = _FakeLLMClient(response_text='{"score": 0.9, "rationale": "good catch"}')
        judge = AnthropicJudge(llm_client=fake)
        verdict = judge.score_review(_case(), _review([_finding()]))
        assert verdict.score == Decimal("0.900")
        assert verdict.rationale == "good catch"

    def test_score_review_raises_judge_parse_error_on_garbage_output(self):
        fake = _FakeLLMClient(response_text="I cannot comply with this request.")
        judge = AnthropicJudge(llm_client=fake)
        with pytest.raises(JudgeParseError):
            judge.score_review(_case(), _review([_finding()]))

    def test_passes_review_id_none_so_no_event_is_attributed(self):
        # AnthropicJudge always calls with review_id=None -- see that
        # class's docstring for why a judge call isn't attributed to any
        # review's own agent_events trace. Verified indirectly: the fake
        # client's `complete` signature accepts review_id and the call
        # succeeds without one being threaded through explicitly by the
        # caller (score_review never receives review_id at all).
        fake = _FakeLLMClient(response_text='{"score": 0.5, "rationale": "ok"}')
        judge = AnthropicJudge(llm_client=fake)
        judge.score_review(_case(), _review())
        assert fake.captured_system is not None
        assert fake.captured_user is not None


class TestJudgeSettings:
    def test_overrides_model_to_claude_sonnet_5(self):
        from backend.core.settings import Settings

        base = Settings(github_webhook_secret="x", anthropic_model="claude-haiku-4-5")
        overridden = judge_settings(base)
        assert overridden.anthropic_model == "claude-sonnet-5"
        # Everything else is inherited unchanged, including the shared
        # BudgetGuard cap -- see judge_settings' docstring for why that
        # sharing is deliberate.
        assert overridden.budget_daily_cap_usd == base.budget_daily_cap_usd
        assert overridden.github_webhook_secret == base.github_webhook_secret
