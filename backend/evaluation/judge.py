"""LLM-as-judge: scores a produced ``Review`` against one ``GoldenCase``'s expectations.

Owns: ``JudgeVerdict`` (one scored result), ``JudgeProtocol`` (the shape
``backend.evaluation.regression_gate`` depends on -- a ``Protocol``, not an
ABC, mirroring ``backend.tools.llm_client.LLMClientProtocol``'s own
structural-typing precedent so tests never need to construct a real
Anthropic client), and two implementations:

- ``AnthropicJudge``: the real judge. Calls **claude-sonnet-5**, never the
  driver model the four specialists use (``claude-haiku-4-5``) -- a judge
  scoring its own model family's output would be a weaker check than an
  independent, stronger model grading it. Built on top of
  ``backend.tools.llm_client.AnthropicLLMClient`` (the exact same
  retry/circuit-breaker/timeout/cost-accounting composition the specialist
  agents already use -- see that module's docstring), with only the model
  id overridden via ``Settings.model_copy``. This is the one live, billable
  call in this package.
- ``StaticScoreJudge``: a canned, in-memory judge for tests -- returns
  whatever ``JudgeVerdict`` it was constructed with, per ``case_id``, no
  network at all. This is the "fixture judge score" PLAN.md's M13 success
  criteria names ("the regression gate fails CI when run against a
  deliberately-degraded fixture judge score").

SCORING SCALE: ``JudgeVerdict.score`` is a ``Decimal`` in ``[0.000, 1.000]``,
matching ``Finding.confidence``'s own precision convention (3 decimal
places) so every probability-flavored number in this codebase reads the
same way.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Protocol

from backend.core.settings import Settings, get_settings
from backend.evaluation.golden_dataset import GoldenCase
from backend.models import Review
from backend.tools.llm_client import LLMClientProtocol

# claude-sonnet-5: this project's designated checker/judge model (per
# PLAN.md's M13 text) -- deliberately never the driver model the four
# specialist agents call (claude-haiku-4-5, see backend.core.settings'
# _DEFAULT_ANTHROPIC_MODEL). A model judging its own family's output is a
# strictly weaker check than an independent, higher-tier model grading it.
JUDGE_MODEL = "claude-sonnet-5"

_SCORE_QUANTUM = Decimal("0.001")

_JUDGE_SYSTEM_PROMPT = """\
You are an impartial code-review quality judge for an automated PR-review \
system. You will be given ONE golden test case (a diff, a description, and \
a list of expected findings a correct review should surface) and the \
findings a candidate review actually produced for that same diff. Score \
how well the candidate review matches the expectations.

Scoring rubric (produce a single float in [0.0, 1.0]):
- Every expected finding marked "must_detect: true" that the candidate \
substantively caught (same class of issue, correct file, severity at \
least as high as expected) contributes strongly to the score. Missing \
even one must-detect finding is a serious defect -- score no higher than \
0.4 if any must-detect finding was missed entirely.
- Expected findings marked "must_detect: false" are nice-to-catch: reward \
catching them, but do not penalize heavily for missing them.
- If "expected_clean" is true, the diff has no real issues. A candidate \
with no CRITICAL/HIGH findings scores near 1.0; a candidate that \
fabricates a CRITICAL/HIGH finding not grounded in the diff scores low \
(hallucination is exactly what this case exists to catch).
- A candidate that reports a finding on the wrong file, or with a wildly \
inflated severity, should score lower than one that reports it correctly.
- Extra, well-grounded findings beyond what was expected are neutral to \
mildly positive -- do not penalize thoroughness.

Respond with STRICTLY ONE JSON object and nothing else, on a single line \
or pretty-printed, in exactly this shape:
{"score": <float 0.0-1.0>, "rationale": "<one or two sentences explaining the score>"}
"""

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class JudgeParseError(Exception):
    """Raised when the judge's raw response text cannot be parsed into a ``JudgeVerdict``."""


@dataclass(frozen=True)
class JudgeVerdict:
    """One judge's scored opinion of one review, against one golden case.

    Attributes:
        score: In ``[0.000, 1.000]`` -- 3 decimal places, matching
            ``Finding.confidence``'s precision convention.
        rationale: The judge's own explanation, always non-empty (a judge
            that cannot explain itself is not trustworthy -- see this
            module's docstring's honesty notes in
            ``backend.evaluation.regression_gate``).
    """

    score: Decimal
    rationale: str


class JudgeProtocol(Protocol):
    """The shape ``backend.evaluation.regression_gate.run_regression_gate`` depends on."""

    def score_review(self, case: GoldenCase, review: Review) -> JudgeVerdict: ...


def _clamp_score(value: float) -> Decimal:
    bounded = max(0.0, min(1.0, value))
    return Decimal(str(bounded)).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP)


def _strip_code_fence(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text.strip()).strip()


def parse_judge_response(raw_text: str) -> JudgeVerdict:
    """Parse the judge model's raw text into a ``JudgeVerdict``.

    Tolerant of a markdown code fence around the JSON (models frequently
    add one despite being told not to -- the same drift-tolerance
    philosophy ``backend.agents.response_parsing`` already applies to the
    specialist agents' own JSON output), but otherwise strict: a response
    with no parseable ``score``/``rationale`` raises ``JudgeParseError``
    rather than silently defaulting to some score, since a judge whose
    output cannot even be read is not a working judge.
    """
    cleaned = _strip_code_fence(raw_text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise JudgeParseError(f"judge response is not valid JSON: {raw_text!r}") from exc

    if not isinstance(payload, dict):
        raise JudgeParseError(f"judge response JSON is not an object: {raw_text!r}")

    raw_score = payload.get("score")
    rationale = payload.get("rationale")
    if raw_score is None or not isinstance(rationale, str) or not rationale.strip():
        raise JudgeParseError(f"judge response missing 'score'/'rationale': {raw_text!r}")

    try:
        score = _clamp_score(float(raw_score))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise JudgeParseError(f"judge response 'score' is not a number: {raw_text!r}") from exc

    return JudgeVerdict(score=score, rationale=rationale.strip())


def _findings_summary(review: Review) -> str:
    """A compact JSON summary of a review's findings, for the judge prompt.

    Deliberately omits ``line_start``/``line_end`` (irrelevant to whether
    the right *class* of issue on the right *file* was caught) to keep the
    prompt small and focused on what the rubric actually scores.
    """
    return json.dumps(
        [
            {
                "agent_type": finding.agent_type.value,
                "severity": finding.severity.value,
                "category": finding.category,
                "file_path": finding.file_path,
                "confidence": str(finding.confidence),
                "rationale": finding.rationale,
            }
            for finding in review.findings
        ],
        indent=2,
    )


def build_judge_prompt(case: GoldenCase, review: Review) -> str:
    """The user-turn content sent to the judge: the case's expectations plus the candidate's findings."""
    expected = [
        {
            "category": f.category,
            "severity": f.severity.value,
            "file_path": f.file_path,
            "must_detect": f.must_detect,
            "notes": f.notes,
        }
        for f in case.expected_findings
    ]
    return (
        f"CASE ID: {case.case_id}\n"
        f"DESCRIPTION: {case.description}\n"
        f"EXPECTED_CLEAN: {case.expected_clean}\n\n"
        f"DIFF:\n{case.diff_text}\n\n"
        f"EXPECTED FINDINGS (JSON):\n{json.dumps(expected, indent=2)}\n\n"
        f"CANDIDATE REVIEW FINDINGS (JSON):\n{_findings_summary(review)}\n"
    )


@dataclass
class AnthropicJudge:
    """The real judge: claude-sonnet-5 via ``AnthropicLLMClient``.

    Attributes:
        llm_client: Anything satisfying ``LLMClientProtocol`` -- production
            code constructs this via ``build_anthropic_judge`` below (a
            real ``AnthropicLLMClient`` pinned to ``JUDGE_MODEL``); tests
            inject a fake satisfying the same protocol, the identical
            pattern ``SecurityAgent``'s own tests already use.
    """

    llm_client: LLMClientProtocol

    def score_review(self, case: GoldenCase, review: Review) -> JudgeVerdict:
        """One real (or fake-injected) LLM call, scoring ``review`` against ``case``.

        ``review_id=None`` deliberately -- a judge call is not attributed
        to any review's own ``agent_events`` trace (it happens after the
        fact, often in CI, not as part of that review's own pipeline run),
        so no ``llm.call`` event is emitted for it. See
        ``backend.tools.llm_client.AnthropicLLMClient.complete``'s
        docstring for why ``review_id=None`` skips event emission.
        """
        response = self.llm_client.complete(
            system=_JUDGE_SYSTEM_PROMPT,
            user=build_judge_prompt(case, review),
            agent="judge",
            review_id=None,
        )
        return parse_judge_response(response.text)


def judge_settings(base: Settings | None = None) -> Settings:
    """``Settings`` with ``anthropic_model`` overridden to ``JUDGE_MODEL``.

    Everything else (the API key, budget cap, reliability knobs) is
    inherited from ``base`` (or the process-wide singleton) unchanged --
    the judge shares the same daily ``BudgetGuard`` cap as the specialist
    agents, deliberately: a runaway judge should be bounded by the exact
    same real-money safety net, not a separate, easy-to-forget-to-configure
    one.
    """
    resolved = base if base is not None else get_settings()
    return resolved.model_copy(update={"anthropic_model": JUDGE_MODEL})


class StaticScoreJudge:
    """A canned, no-network judge for tests -- the "fixture judge score" PLAN.md's M13 success criteria name.

    Constructed with a fixed ``{case_id: JudgeVerdict}`` mapping; raises
    ``KeyError`` if asked to score a case it wasn't given a canned verdict
    for, rather than silently returning some default score.
    """

    def __init__(self, verdicts_by_case_id: dict[str, JudgeVerdict]) -> None:
        self._verdicts = dict(verdicts_by_case_id)

    def score_review(self, case: GoldenCase, review: Review) -> JudgeVerdict:
        del review  # unused: this judge's whole point is to ignore the input and return canned output
        return self._verdicts[case.case_id]
