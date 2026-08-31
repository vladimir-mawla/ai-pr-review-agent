"""PLAN.md's named M13 demo suite: the regression gate.

Two halves, deliberately separated by ``@pytest.mark.live``:

1. ``Test*Free`` classes -- FREE, no network. Prove the gate's own
   pass/fail arithmetic works, using ``StaticScoreJudge`` (a canned
   "fixture judge score", per PLAN.md's own M13 success-criteria wording)
   so this half of the suite costs nothing and runs in CI on every push.
   This is where "the regression gate fails CI when run against a
   deliberately-degraded fixture judge score, and passes against the
   baseline" is actually proven.
2. ``TestLiveJudge*`` -- LIVE, billable (real claude-sonnet-5 calls).
   Proves the *real* judge, not just the gate's arithmetic, actually
   distinguishes a good review from a deliberately bad one on a real
   golden case, and measures the judge's own run-to-run scoring variance
   (see that class's docstring) -- run once, deliberately, not in a loop
   (see this milestone's build report for the recorded cost/variance).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.core.settings import get_settings
from backend.evaluation.golden_dataset import GoldenCase, load_golden_dataset
from backend.evaluation.judge import AnthropicJudge, JudgeVerdict, StaticScoreJudge, judge_settings
from backend.evaluation.regression_gate import (
    REGRESSION_GATE_THRESHOLD,
    MissingReviewForCaseError,
    run_regression_gate,
)
from backend.models import Finding, Review, ReviewStatus, compute_overall_confidence
from backend.tools.llm_client import AnthropicLLMClient


def _dummy_review(review_id: str, findings: list[Finding] | None = None) -> Review:
    findings = findings or []
    confidence = compute_overall_confidence(findings)
    return Review(
        review_id=review_id,
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


def _sql_injection_finding() -> Finding:
    return Finding(
        agent_type="SECURITY",
        severity="CRITICAL",
        category="sql_injection",
        file_path="app/db.py",
        line_start=14,
        line_end=14,
        confidence=Decimal("0.980"),
        rationale=(
            "User-supplied `username` is concatenated directly into the SQL "
            "query string instead of being passed as a bound parameter, "
            "allowing SQL injection."
        ),
    )


class TestRegressionGatePassesOnBaselineFree:
    """FREE: a StaticScoreJudge returning strong scores must pass the gate."""

    def test_gate_passes_when_every_case_scores_above_threshold(self):
        cases = load_golden_dataset()
        judge = StaticScoreJudge(
            {case.case_id: JudgeVerdict(score=Decimal("0.900"), rationale="baseline") for case in cases}
        )
        reviews = {case.case_id: _dummy_review(f"r-{case.case_id}") for case in cases}
        result = run_regression_gate(cases, reviews, judge)
        assert result.passed is True
        assert result.mean_score == Decimal("0.900")
        assert result.threshold == REGRESSION_GATE_THRESHOLD


class TestRegressionGateFailsOnDegradedFixtureScoreFree:
    """FREE: proves the gate CAN fail -- the whole point of a regression gate.

    This is the exact PLAN.md M13 success-criteria proof: "the regression
    gate fails CI when run against a deliberately-degraded fixture judge
    score". A gate that has never been observed to reject anything is not
    a tested gate -- this gives it a real, deterministic failing case.
    """

    def test_gate_fails_when_every_case_scores_far_below_threshold(self):
        cases = load_golden_dataset()
        judge = StaticScoreJudge(
            {
                case.case_id: JudgeVerdict(score=Decimal("0.200"), rationale="degraded: missed everything")
                for case in cases
            }
        )
        reviews = {case.case_id: _dummy_review(f"r-{case.case_id}") for case in cases}
        result = run_regression_gate(cases, reviews, judge)
        assert result.passed is False
        assert result.mean_score == Decimal("0.200")

    def test_gate_fails_when_one_must_detect_case_is_missed_and_others_are_only_average(self):
        """A single badly-missed must-detect case, diluted across the rest, still drags the mean under threshold."""
        cases = load_golden_dataset()
        scores = {}
        for case in cases:
            if case.case_id == "sqli-basic":
                scores[case.case_id] = JudgeVerdict(score=Decimal("0.100"), rationale="missed the sqli entirely")
            else:
                scores[case.case_id] = JudgeVerdict(score=Decimal("0.750"), rationale="fine")
        judge = StaticScoreJudge(scores)
        reviews = {case.case_id: _dummy_review(f"r-{case.case_id}") for case in cases}
        result = run_regression_gate(cases, reviews, judge)
        assert result.passed is False, (
            f"expected the gate to fail (mean {result.mean_score} should be below "
            f"threshold {result.threshold}) when the must-detect sqli case scores 0.100"
        )

    def test_gate_passes_at_exactly_the_threshold_boundary(self):
        """Mirrors backend.hitl.queue.route_review's own 'at or above passes' boundary convention."""
        cases = load_golden_dataset()[:1]
        judge = StaticScoreJudge(
            {cases[0].case_id: JudgeVerdict(score=REGRESSION_GATE_THRESHOLD, rationale="exactly at threshold")}
        )
        reviews = {cases[0].case_id: _dummy_review("r-boundary")}
        result = run_regression_gate(cases, reviews, judge)
        assert result.passed is True


class TestRegressionGateInputValidationFree:
    def test_raises_when_a_case_has_no_supplied_review(self):
        cases = load_golden_dataset()[:1]
        judge = StaticScoreJudge({})
        with pytest.raises(MissingReviewForCaseError):
            run_regression_gate(cases, {}, judge)

    def test_summary_mentions_pass_or_fail_and_every_case_id(self):
        case = GoldenCase(
            case_id="s1",
            description="d",
            diff_path="tests/fixtures/clean_diff.patch",
            expected_clean=True,
            diff_text="x",
        )
        judge = StaticScoreJudge({"s1": JudgeVerdict(score=Decimal("0.900"), rationale="ok")})
        result = run_regression_gate([case], {"s1": _dummy_review("r1")}, judge)
        summary = result.summary()
        assert "PASS" in summary
        assert "s1" in summary


# ---------------------------------------------------------------------------
# LIVE: real claude-sonnet-5 calls. Deselected by default (addopts "-m not
# live"); run explicitly with `pytest -m live tests/eval/test_regression_gate.py -v`.
# Skips cleanly if ANTHROPIC_API_KEY is not configured, same belt-and-braces
# pattern every other live test in this project follows.
# ---------------------------------------------------------------------------

_HAS_ANTHROPIC_KEY = bool(os.environ.get("ANTHROPIC_API_KEY") or get_settings().anthropic_api_key)


@pytest.mark.live
@pytest.mark.skipif(not _HAS_ANTHROPIC_KEY, reason="ANTHROPIC_API_KEY not configured")
class TestLiveJudgeDistinguishesGoodFromBadReviews:
    """Proves the REAL judge (not just the gate's arithmetic) works end-to-end.

    Three real claude-sonnet-5 calls total, run once per invocation of this
    test (see this milestone's build report for the exact recorded cost):

    1. Score a genuinely correct review (the SQL injection, caught) against
       the "sqli-basic" golden case.
    2. Score a deliberately bad review (empty findings -- the SQL injection
       missed entirely) against the same case.
    3. Re-score the SAME good review a second time, to measure the judge's
       own run-to-run scoring variance -- reported, not hidden. A gate
       whose score flips between reruns of the identical input is a weaker
       instrument than one whose score is stable; this is the honest
       measurement backend.evaluation.regression_gate's module docstring
       promises rather than assumes.
    """

    @pytest.fixture(scope="class")
    def sqli_case(self) -> GoldenCase:
        cases = {case.case_id: case for case in load_golden_dataset()}
        return cases["sqli-basic"]

    @pytest.fixture(scope="class")
    def judge(self) -> AnthropicJudge:
        client = AnthropicLLMClient(settings=judge_settings())
        return AnthropicJudge(llm_client=client)

    def test_good_review_scores_higher_than_bad_review_and_variance_is_measured(self, sqli_case, judge):
        good_review = _dummy_review("live-good", [_sql_injection_finding()])
        bad_review = _dummy_review("live-bad", [])  # the SQL injection missed entirely

        good_verdict_a = judge.score_review(sqli_case, good_review)
        bad_verdict = judge.score_review(sqli_case, bad_review)
        good_verdict_b = judge.score_review(sqli_case, good_review)  # same input, re-scored

        print(f"\n[live judge] good review, run A: score={good_verdict_a.score} -- {good_verdict_a.rationale}")
        print(f"[live judge] bad review:          score={bad_verdict.score} -- {bad_verdict.rationale}")
        print(f"[live judge] good review, run B: score={good_verdict_b.score} -- {good_verdict_b.rationale}")
        variance = abs(good_verdict_a.score - good_verdict_b.score)
        print(f"[live judge] run-to-run variance on identical input: {variance}")

        # The core claim: the real judge distinguishes catching the CRITICAL
        # sql_injection from missing it entirely, by a wide margin -- not
        # just a rounding difference.
        assert good_verdict_a.score > bad_verdict.score + Decimal("0.2"), (
            f"expected the good review ({good_verdict_a.score}) to score meaningfully "
            f"higher than the bad review ({bad_verdict.score})"
        )
        # Per the judge's own rubric (backend.evaluation.judge's system
        # prompt: missing a must-detect finding caps the score at 0.4), the
        # bad review should score at or below that cap.
        assert bad_verdict.score <= Decimal("0.4"), (
            f"expected the bad review (SQL injection missed entirely) to score "
            f"<= 0.4 per the judge's own rubric, got {bad_verdict.score}"
        )

        # Run the actual gate against these SAME three real verdicts (no
        # further live calls -- StaticScoreJudge just replays what the real
        # judge already returned above), the same proof
        # TestRegressionGateFails.../Passes... give with a canned fixture
        # judge, but seeded with real claude-sonnet-5 output end-to-end.
        replay_judge = StaticScoreJudge(
            {sqli_case.case_id: good_verdict_a},
        )
        good_result = run_regression_gate([sqli_case], {sqli_case.case_id: good_review}, replay_judge)
        assert good_result.passed is True, good_result.summary()

        replay_judge_bad = StaticScoreJudge({sqli_case.case_id: bad_verdict})
        bad_result = run_regression_gate([sqli_case], {sqli_case.case_id: bad_review}, replay_judge_bad)
        assert bad_result.passed is False, bad_result.summary()
