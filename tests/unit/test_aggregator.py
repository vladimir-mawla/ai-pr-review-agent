"""Unit tests for M5's aggregation logic: dedupe + overall-confidence formula.

This is one of the two files PLAN.md's M5 demo command names
(``pytest tests/unit/test_aggregator.py tests/unit/test_hitl_gate.py -v``).
Everything here is pure Python over fixture ``Finding`` objects -- no LLM,
no graph, no I/O -- per M5's outcome text ("all testable without any LLM").

Covers:
- dedupe keeps the highest-confidence duplicate, drops the rest
- deterministic tie-breaking on an exact confidence tie
- same file/different lines, and different files/same line, are NOT deduped
- an empty findings list is handled sanely (dedupe -> [], confidence -> 0.000)
- overall_confidence matches its documented formula (mean, ROUND_HALF_UP)
- a Review whose overall_confidence contradicts its findings is impossible
  to construct (the M1-deferred gap this milestone closes)
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.agents.base_agent import AGENT_PRECEDENCE
from backend.agents.contracts import dedupe_findings
from backend.models import (
    AgentType,
    Finding,
    Review,
    ReviewStatus,
    Severity,
    compute_overall_confidence,
)


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
