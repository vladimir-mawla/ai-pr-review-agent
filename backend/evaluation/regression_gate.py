"""The regression gate: fails when judged review quality drops below a threshold.

Owns: ``run_regression_gate``, which scores a set of produced ``Review``
objects (one per ``GoldenCase``) with a ``JudgeProtocol`` and decides
pass/fail against ``REGRESSION_GATE_THRESHOLD``.

THRESHOLD CHOICE -- 0.700, deliberately, not 0.5 and not 0.9:
    The golden dataset (``tests/fixtures/golden_dataset.json``) mixes
    hard-must-catch cases (a missed SQL injection scores that case <= 0.4,
    per the judge's own rubric -- see ``backend.evaluation.judge``'s system
    prompt) with softer, nice-to-catch cases. A mean score of 0.700 across
    4 cases requires the review pipeline to be catching essentially every
    must-detect finding (missing even one drags a case below 0.4, which by
    itself would require every other case to average above ~0.87 just to
    clear 0.700) while tolerating normal judge scoring noise on the softer
    cases. Set much higher (e.g. 0.9), routine judge variance (see the
    module docstring's honesty note below) would make the gate flip on
    reruns with no real regression; set much lower (e.g. 0.5), a pipeline
    that silently stopped catching the SQL injection case entirely could
    still pass by acing the softer cases. 0.700 is the deliberate middle:
    tight enough to catch a real quality regression on a must-detect case,
    loose enough to survive one soft case scoring lower than expected on a
    given run.

CRITICAL METHODOLOGICAL HONESTY -- what this gate can and cannot detect:
    An LLM judge is itself an unreliable instrument: the same review can
    receive different scores on different judge calls (see
    ``tests/eval/test_regression_gate.py``'s live variance-measurement
    test, which runs the real judge twice on the identical input and
    reports the delta -- read that test's recorded result before trusting
    this gate's precision). This gate can catch a LARGE, unambiguous
    regression (a specialist that stops firing entirely, a prompt change
    that makes it miss an obvious SQL injection) with reasonable
    confidence. It CANNOT be trusted to catch a small, subtle quality
    regression (e.g. a 5% drop in finding precision) -- that signal would
    be smaller than the judge's own run-to-run noise, and a gate this
    coarse would either miss it entirely or flip on unrelated reruns. Do
    not present this gate as unimpeachable "quality assurance"; it is a
    coarse, one-sided tripwire for obvious degradation, backed by an
    LLM whose own consistency has not been proven beyond the two-run
    measurement in ``test_regression_gate.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from backend.evaluation.golden_dataset import GoldenCase
from backend.evaluation.judge import JudgeProtocol, JudgeVerdict
from backend.models import Review

# See module docstring's "THRESHOLD CHOICE" section for the full reasoning.
REGRESSION_GATE_THRESHOLD = Decimal("0.700")

_MEAN_QUANTUM = Decimal("0.001")


@dataclass(frozen=True)
class CaseVerdict:
    """One golden case's judged result."""

    case: GoldenCase
    verdict: JudgeVerdict


@dataclass(frozen=True)
class RegressionGateResult:
    """The gate's overall verdict across every golden case it was run against.

    Attributes:
        case_verdicts: Per-case judge results, same order as the input.
        mean_score: The arithmetic mean of every case's score, rounded to
            3 decimal places with ROUND_HALF_UP (mirrors
            ``backend.models.review.compute_overall_confidence``'s own
            fixed rounding rule, for the same "deterministic regardless of
            platform" reason).
        threshold: The threshold this result was evaluated against.
        passed: ``True`` iff ``mean_score >= threshold``. At-threshold
            passes -- mirrors ``backend.hitl.queue.route_review``'s own
            "at or above" boundary convention.
    """

    case_verdicts: list[CaseVerdict]
    mean_score: Decimal
    threshold: Decimal
    passed: bool

    def summary(self) -> str:
        """A short, human-readable line for CI logs/pytest output."""
        verdict_word = "PASS" if self.passed else "FAIL"
        lines = [
            f"regression gate: {verdict_word} -- mean_score={self.mean_score} "
            f"threshold={self.threshold} ({len(self.case_verdicts)} case(s))"
        ]
        for cv in self.case_verdicts:
            lines.append(f"  {cv.case.case_id}: score={cv.verdict.score} -- {cv.verdict.rationale}")
        return "\n".join(lines)


class MissingReviewForCaseError(Exception):
    """Raised when ``run_regression_gate`` is given a case with no corresponding produced review."""


def run_regression_gate(
    cases: list[GoldenCase],
    reviews_by_case_id: dict[str, Review],
    judge: JudgeProtocol,
    *,
    threshold: Decimal = REGRESSION_GATE_THRESHOLD,
) -> RegressionGateResult:
    """Score every case with ``judge`` and decide pass/fail against ``threshold``.

    Args:
        cases: The golden cases to evaluate (typically
            ``backend.evaluation.golden_dataset.load_golden_dataset()``'s
            full result, or a filtered subset).
        reviews_by_case_id: The candidate review produced for each case's
            diff, keyed by ``GoldenCase.case_id``. Every ``case`` must have
            an entry here -- how those reviews were produced (a real
            pipeline run, or a hand-built fixture ``Review`` for a fast
            test) is deliberately this function's caller's concern, not
            this function's, so the gate itself never depends on a live
            LLM to run its own pass/fail logic (only the injected
            ``judge`` might).
        judge: Anything satisfying ``JudgeProtocol`` -- a real
            ``AnthropicJudge`` in production/live tests, a
            ``StaticScoreJudge`` in fast/deterministic tests.
        threshold: Overridable for tests that want to probe a specific
            boundary; production callers should use the default.

    Raises:
        ``MissingReviewForCaseError``: a case has no corresponding entry in
            ``reviews_by_case_id``.
    """
    case_verdicts: list[CaseVerdict] = []
    for case in cases:
        review = reviews_by_case_id.get(case.case_id)
        if review is None:
            raise MissingReviewForCaseError(
                f"no produced review supplied for golden case {case.case_id!r}"
            )
        verdict = judge.score_review(case, review)
        case_verdicts.append(CaseVerdict(case=case, verdict=verdict))

    total = sum((cv.verdict.score for cv in case_verdicts), start=Decimal("0"))
    mean_score = (total / len(case_verdicts)).quantize(_MEAN_QUANTUM, rounding=ROUND_HALF_UP)
    passed = mean_score >= threshold

    return RegressionGateResult(
        case_verdicts=case_verdicts,
        mean_score=mean_score,
        threshold=threshold,
        passed=passed,
    )
