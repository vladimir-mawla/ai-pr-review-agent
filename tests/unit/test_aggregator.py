"""Unit tests for M5's aggregation logic: dedupe + overall-confidence formula.

This is one of the two files PLAN.md's M5 demo command names
(``pytest tests/unit/test_aggregator.py tests/unit/test_hitl_gate.py -v``).
Everything here is pure Python over fixture ``Finding`` objects -- no LLM,
no graph, no I/O -- per M5's outcome text ("all testable without any LLM").

Covers:
- dedupe keeps the higher-severity duplicate first, and only falls back to
  highest-confidence to break a tie *within* the same severity
- deterministic tie-breaking on an exact confidence tie (same severity)
- same file/different lines, and different files/same line, are NOT deduped
- an empty findings list is handled sanely (dedupe -> [], confidence -> 0.000)
- overall_confidence matches its documented formula (mean, ROUND_HALF_UP)
- a Review whose overall_confidence contradicts its findings is impossible
  to construct (the M1-deferred gap this milestone closes)

``TestSeverityBeforeConfidenceDedupe`` and
``TestDedupeAndRoutingInteraction`` (below) are the regression coverage for
an M5 L4 VERIFY REJECT: the original dedupe rule ("highest confidence
wins", full stop) let a CRITICAL finding lose a dedup collision to a
same-key INFO finding with marginally higher confidence, which then made
it invisible to ``backend.hitl.queue.has_critical_finding`` (which only
ever inspects the *post-dedupe* list) and the review auto-posted instead
of escalating to human review. Every other test class in this file
exercises ``dedupe_findings`` in isolation with same-severity fixtures,
which is exactly why the original bug survived: dedupe and routing were
each individually well-tested but never exercised *together* across a
severity-losing-to-confidence collision.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import permutations

import pytest
from pydantic import ValidationError

from backend.agents.base_agent import AGENT_PRECEDENCE
from backend.agents.contracts import dedupe_findings
from backend.hitl.queue import has_critical_finding, route_review
from backend.models import (
    AgentType,
    Finding,
    Review,
    ReviewStatus,
    Severity,
    compute_overall_confidence,
)
from backend.models.enums import SEVERITY_RANK

_HITL_THRESHOLD = Decimal("0.750")


def _finding(
    *,
    agent_type: AgentType = AgentType.SECURITY,
    severity: Severity = Severity.MEDIUM,
    category: str = "stub",
    file_path: str = "src/app.py",
    line_start: int = 10,
    line_end: int | None = None,
    confidence: str = "0.800",
    rationale: str = "test finding",
) -> Finding:
    """Build a valid Finding with sensible defaults, overriding only what a test cares about."""
    return Finding(
        agent_type=agent_type,
        severity=severity,
        category=category,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end if line_end is not None else line_start,
        confidence=Decimal(confidence),
        rationale=rationale,
    )


class TestDedupeFindings:
    """backend.agents.contracts.dedupe_findings."""

    def test_keeps_highest_confidence_and_drops_the_rest(self) -> None:
        """Three agents flag the same (file, line); only the most confident survives."""
        low = _finding(agent_type=AgentType.DOCS, confidence="0.400", rationale="low")
        mid = _finding(agent_type=AgentType.TESTS, confidence="0.700", rationale="mid")
        high = _finding(agent_type=AgentType.SECURITY, confidence="0.950", rationale="high")

        result = dedupe_findings([low, high, mid])

        assert result == [high]

    def test_tie_break_is_deterministic_by_agent_precedence(self) -> None:
        """Equal confidence: the earlier AGENT_PRECEDENCE agent wins, regardless of input order."""
        # SECURITY precedes TESTS in AGENT_PRECEDENCE.
        assert AGENT_PRECEDENCE.index(AgentType.SECURITY) < AGENT_PRECEDENCE.index(
            AgentType.TESTS
        )
        security_finding = _finding(
            agent_type=AgentType.SECURITY, confidence="0.800", rationale="from security"
        )
        tests_finding = _finding(
            agent_type=AgentType.TESTS, confidence="0.800", rationale="from tests"
        )

        result_a = dedupe_findings([tests_finding, security_finding])
        result_b = dedupe_findings([security_finding, tests_finding])

        assert result_a == [security_finding]
        assert result_b == [security_finding]
        assert result_a == result_b, "dedupe must not depend on input order"

    def test_tie_break_falls_through_to_category_when_agent_type_also_ties(self) -> None:
        """Same agent, same confidence: lexicographically smaller category wins, either order."""
        finding_z = _finding(category="zzz_category", confidence="0.800", rationale="z")
        finding_a = _finding(category="aaa_category", confidence="0.800", rationale="a")

        assert dedupe_findings([finding_z, finding_a]) == [finding_a]
        assert dedupe_findings([finding_a, finding_z]) == [finding_a]

    def test_same_file_different_lines_are_not_deduped(self) -> None:
        """Two findings in the same file at different lines are independent issues."""
        line_10 = _finding(file_path="src/app.py", line_start=10)
        line_20 = _finding(file_path="src/app.py", line_start=20)

        result = dedupe_findings([line_10, line_20])

        assert len(result) == 2
        assert line_10 in result
        assert line_20 in result

    def test_different_files_same_line_are_not_deduped(self) -> None:
        """The same line number in two different files must not collide."""
        file_a = _finding(file_path="src/a.py", line_start=5)
        file_b = _finding(file_path="src/b.py", line_start=5)

        result = dedupe_findings([file_a, file_b])

        assert len(result) == 2
        assert file_a in result
        assert file_b in result

    def test_empty_input_returns_empty_list(self) -> None:
        """No findings in, no findings out -- no crash, no special-casing surprise."""
        assert dedupe_findings([]) == []

    def test_mixed_fixture_only_true_duplicates_collapse(self) -> None:
        """A realistic mixed fixture: some findings collide, some don't."""
        dup_low = _finding(
            file_path="src/db.py", line_start=42, confidence="0.600", agent_type=AgentType.QUALITY
        )
        dup_high = _finding(
            file_path="src/db.py",
            line_start=42,
            confidence="0.950",
            agent_type=AgentType.SECURITY,
        )
        unique_other_line = _finding(file_path="src/db.py", line_start=99, confidence="0.500")
        unique_other_file = _finding(file_path="src/auth.py", line_start=42, confidence="0.500")

        result = dedupe_findings([dup_low, unique_other_line, dup_high, unique_other_file])

        assert len(result) == 3
        assert dup_high in result
        assert dup_low not in result
        assert unique_other_line in result
        assert unique_other_file in result


class TestSeverityBeforeConfidenceDedupe:
    """Regression coverage for the M5 L4 VERIFY REJECT.

    The bug: dedupe ordered purely by confidence, so a CRITICAL finding
    with slightly *lower* confidence than a colliding lower-severity
    finding was discarded. The fix: severity is compared first; confidence
    only breaks a tie *within* the same severity. Every test here builds a
    pair where the higher-severity finding has the LOWER confidence, so a
    reversion to confidence-first ordering would flip these assertions.
    """

    @pytest.mark.parametrize(
        ("higher_severity", "lower_severity"),
        [
            (Severity.CRITICAL, Severity.HIGH),
            (Severity.CRITICAL, Severity.MEDIUM),
            (Severity.CRITICAL, Severity.LOW),
            (Severity.CRITICAL, Severity.INFO),
            (Severity.HIGH, Severity.MEDIUM),
            (Severity.HIGH, Severity.LOW),
            (Severity.HIGH, Severity.INFO),
            (Severity.MEDIUM, Severity.LOW),
            (Severity.MEDIUM, Severity.INFO),
            (Severity.LOW, Severity.INFO),
        ],
    )
    def test_higher_severity_wins_even_with_lower_confidence(
        self, higher_severity: Severity, lower_severity: Severity
    ) -> None:
        """For every adjacent (and non-adjacent) severity pair, severity outranks confidence."""
        assert SEVERITY_RANK[higher_severity] < SEVERITY_RANK[lower_severity], (
            "test fixture error: severities not actually ordered higher/lower"
        )
        weak_but_severe = _finding(
            severity=higher_severity,
            confidence="0.500",
            agent_type=AgentType.SECURITY,
            rationale="severe-but-lower-confidence",
        )
        confident_but_mild = _finding(
            severity=lower_severity,
            confidence="0.999",
            agent_type=AgentType.DOCS,
            rationale="mild-but-higher-confidence",
        )

        result_a = dedupe_findings([confident_but_mild, weak_but_severe])
        result_b = dedupe_findings([weak_but_severe, confident_but_mild])

        assert result_a == [weak_but_severe]
        assert result_b == [weak_but_severe]
        assert result_a == result_b, "dedupe must not depend on input order"

    def test_the_exact_reported_scenario_survives_dedupe(self) -> None:
        """The precise repro L4 VERIFY reported: SECURITY/CRITICAL/0.751 vs
        DOCS/INFO/0.752 at app.py line 42. Confidence-only ordering picks the
        INFO finding (0.752 > 0.751); the fix picks the CRITICAL one."""
        critical_finding = _finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.CRITICAL,
            file_path="app.py",
            line_start=42,
            confidence="0.751",
            rationale="SQL injection via unsanitized query parameter",
        )
        info_finding = _finding(
            agent_type=AgentType.DOCS,
            severity=Severity.INFO,
            file_path="app.py",
            line_start=42,
            confidence="0.752",
            rationale="missing docstring",
        )

        result = dedupe_findings([critical_finding, info_finding])

        assert result == [critical_finding]

    def test_same_severity_still_breaks_tie_by_confidence(self) -> None:
        """Sanity check: within the same severity, the old confidence rule still applies."""
        low_conf = _finding(severity=Severity.HIGH, confidence="0.600", rationale="low")
        high_conf = _finding(severity=Severity.HIGH, confidence="0.900", rationale="high")

        assert dedupe_findings([low_conf, high_conf]) == [high_conf]
        assert dedupe_findings([high_conf, low_conf]) == [high_conf]

    @pytest.mark.parametrize("trial", range(5))
    def test_property_a_critical_finding_is_never_dropped(self, trial: int) -> None:
        """Property-style check over permuted inputs: whenever a CRITICAL finding
        collides with any mix of other severities/confidences at the same key,
        the CRITICAL one is always the survivor, in every input ordering."""
        critical_finding = _finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.CRITICAL,
            confidence="0.001",  # deliberately the lowest possible confidence
            rationale=f"critical-trial-{trial}",
        )
        # A handful of colliding findings spanning every other severity, each
        # with near-maximum confidence -- the worst case for a confidence-first
        # rule, and exactly what should still lose to CRITICAL.
        rivals = [
            _finding(
                agent_type=AgentType.DOCS,
                severity=severity,
                confidence="0.999",
                rationale=f"rival-{severity.value}-{trial}",
            )
            for severity in (Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO)
        ]

        for ordering in permutations([critical_finding, *rivals]):
            result = dedupe_findings(list(ordering))
            assert result == [critical_finding], (
                f"CRITICAL finding was dropped for ordering {[f.severity for f in ordering]}"
            )


class TestDedupeAndRoutingInteraction:
    """Closes the exact test-design gap that let the M5 bug through.

    ``tests/unit/test_hitl_gate.py`` tests ``route_review`` against
    pre-fabricated finding lists that never pass through
    ``dedupe_findings``; ``tests/integration/test_orchestrator_fanout.py``
    uses four non-colliding stub findings, so dedup is a no-op there. Both
    halves were individually well-tested, but the INTERACTION -- feeding
    dedupe's actual output into route_review -- was not. These tests wire
    the two functions together the same way
    ``backend.orchestrator.nodes.aggregate_node`` does.
    """

    def test_end_to_end_critical_survives_dedupe_and_forces_hitl(self) -> None:
        """The full reported scenario, end to end: dedupe -> route_review.

        This is the test that must FAIL against the old confidence-only
        dedupe ordering (proven by temporarily reverting the fix -- see
        the regression-test proof pasted in the fix's commit/session log)
        and PASS against the fix.
        """
        critical_finding = _finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.CRITICAL,
            file_path="app.py",
            line_start=42,
            confidence="0.751",
            rationale="SQL injection via unsanitized query parameter",
        )
        info_finding = _finding(
            agent_type=AgentType.DOCS,
            severity=Severity.INFO,
            file_path="app.py",
            line_start=42,
            confidence="0.752",
            rationale="missing docstring",
        )

        deduped = dedupe_findings([critical_finding, info_finding])

        # The CRITICAL finding must have survived dedupe -- if the old
        # confidence-only rule were still in place, `deduped` would be
        # `[info_finding]` instead and the assertion below would fail.
        assert deduped == [critical_finding]
        assert has_critical_finding(deduped) is True

        overall_confidence = compute_overall_confidence(deduped)
        status, reason = route_review(overall_confidence, deduped, threshold=_HITL_THRESHOLD)

        assert status == ReviewStatus.QUEUED_FOR_HITL
        assert "critical" in reason.lower()

    def test_end_to_end_is_order_independent(self) -> None:
        """Same scenario, findings supplied in the opposite order: identical outcome."""
        critical_finding = _finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.CRITICAL,
            file_path="app.py",
            line_start=42,
            confidence="0.751",
            rationale="SQL injection via unsanitized query parameter",
        )
        info_finding = _finding(
            agent_type=AgentType.DOCS,
            severity=Severity.INFO,
            file_path="app.py",
            line_start=42,
            confidence="0.752",
            rationale="missing docstring",
        )

        deduped = dedupe_findings([info_finding, critical_finding])
        overall_confidence = compute_overall_confidence(deduped)
        status, _reason = route_review(overall_confidence, deduped, threshold=_HITL_THRESHOLD)

        assert deduped == [critical_finding]
        assert status == ReviewStatus.QUEUED_FOR_HITL

    @pytest.mark.parametrize(
        ("higher_severity", "milder_severity"),
        [
            (Severity.HIGH, Severity.MEDIUM),
            (Severity.HIGH, Severity.INFO),
            (Severity.MEDIUM, Severity.LOW),
            (Severity.LOW, Severity.INFO),
        ],
    )
    def test_non_critical_severities_do_not_force_hitl_via_dedupe(
        self, higher_severity: Severity, milder_severity: Severity
    ) -> None:
        """Contrast case: when the higher-severity survivor is NOT CRITICAL,
        routing follows the ordinary confidence-threshold rule, not an
        unconditional escalation -- confirms the fix is specific to CRITICAL,
        not an accidental blanket "severity wins therefore always HITL"."""
        assert SEVERITY_RANK[higher_severity] < SEVERITY_RANK[milder_severity], (
            "test fixture error: severities not actually ordered higher/lower"
        )
        severe_but_low_confidence = _finding(
            agent_type=AgentType.SECURITY,
            severity=higher_severity,
            file_path="app.py",
            line_start=7,
            confidence="0.900",
            rationale="higher severity, high confidence",
        )
        milder_higher_confidence = _finding(
            agent_type=AgentType.DOCS,
            severity=milder_severity,
            file_path="app.py",
            line_start=7,
            confidence="0.901",
            rationale="milder, marginally higher confidence",
        )

        deduped = dedupe_findings([milder_higher_confidence, severe_but_low_confidence])
        overall_confidence = compute_overall_confidence(deduped)
        status, _reason = route_review(overall_confidence, deduped, threshold=_HITL_THRESHOLD)

        assert deduped == [severe_but_low_confidence]
        assert has_critical_finding(deduped) is False
        # 0.900 confidence, default threshold 0.750 -> auto-posts (no CRITICAL present).
        assert status == ReviewStatus.POSTED


class TestComputeOverallConfidence:
    """backend.models.review.compute_overall_confidence."""

    def test_matches_the_documented_mean_formula(self) -> None:
        """Mean of two exact confidences, no rounding needed."""
        findings = [_finding(confidence="0.950"), _finding(confidence="0.500", line_start=20)]

        assert compute_overall_confidence(findings) == Decimal("0.725")

    def test_rounds_half_up_to_three_decimal_places(self) -> None:
        """0.100 + 0.200 + 0.200 = 0.500 / 3 = 0.1666... -> rounds to 0.167."""
        findings = [
            _finding(confidence="0.100", line_start=1),
            _finding(confidence="0.200", line_start=2),
            _finding(confidence="0.200", line_start=3),
        ]

        result = compute_overall_confidence(findings)

        assert result == Decimal("0.167")

    def test_empty_findings_list_is_zero(self) -> None:
        """No findings means no evidence to average -- defined as 0.000, not a guess."""
        assert compute_overall_confidence([]) == Decimal("0.000")

    def test_uses_deduped_findings_not_raw_agent_output(self) -> None:
        """The aggregator's intended usage: compute confidence AFTER dedup, not before.

        If a caller mistakenly averaged the raw (pre-dedup) findings, this
        duplicate pair would pull the mean down toward the lower, discarded
        finding instead of reflecting only the survivor.
        """
        raw = [
            _finding(file_path="src/x.py", line_start=1, confidence="0.200", rationale="dup-low"),
            _finding(
                file_path="src/x.py", line_start=1, confidence="0.900", rationale="dup-high"
            ),
        ]
        deduped = dedupe_findings(raw)

        assert compute_overall_confidence(deduped) == Decimal("0.900")


class TestReviewOverallConfidenceConsistency:
    """The M1-deferred gap, closed here: Review enforces its own formula."""

    def test_review_rejects_overall_confidence_that_contradicts_findings(self) -> None:
        """Cannot construct a Review whose overall_confidence disagrees with its findings.

        Before M5, this exact shape (findings averaging 0.500, declared
        overall_confidence 0.000) was accepted by Review -- see
        backend/models/review.py's module docstring and
        .genesis/checkpoints/CURRENT.md's M1 Deferred section.
        """
        findings = [_finding(confidence="0.500")]

        with pytest.raises(ValidationError):
            Review(
                review_id="test",
                pr_number=1,
                repository_owner="acme",
                repository_name="widgets",
                head_sha="a" * 40,
                findings=findings,
                overall_confidence=Decimal("0.000"),  # contradicts the mean (0.500)
                status=ReviewStatus.POSTED,
                created_at=datetime.now(),
            )

    def test_review_accepts_the_correctly_computed_value(self) -> None:
        """The one value compute_overall_confidence produces is always accepted."""
        findings = [_finding(confidence="0.500")]
        correct = compute_overall_confidence(findings)

        review = Review(
            review_id="test",
            pr_number=1,
            repository_owner="acme",
            repository_name="widgets",
            head_sha="a" * 40,
            findings=findings,
            overall_confidence=correct,
            status=ReviewStatus.POSTED,
            created_at=datetime.now(),
        )

        assert review.overall_confidence == Decimal("0.500")
