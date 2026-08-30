"""Unit tests for M5's HITL confidence gate (backend.hitl.queue).

This is the second of the two files PLAN.md's M5 demo command names
(``pytest tests/unit/test_aggregator.py tests/unit/test_hitl_gate.py -v``).

Covers:
- confidence at/above threshold with no CRITICAL finding -> auto-post
- confidence below threshold -> human review
- any CRITICAL finding -> human review even at confidence 1.0
- the threshold boundary: exactly at, just below, just above
- the reason string always names the actual configured threshold (the
  drift test from DONE.html's gate: "aggregator threshold in code matches
  the threshold in its user-facing message")
- empty findings list handled sanely
- InMemoryHitlQueue actually holds what gets routed to it
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from backend.hitl.queue import InMemoryHitlQueue, has_critical_finding, route_review
from backend.models import AgentType, Finding, Review, ReviewStatus, Severity

_DEFAULT_THRESHOLD = Decimal("0.750")


def _finding(
    *,
    severity: Severity = Severity.MEDIUM,
    confidence: str = "0.800",
    agent_type: AgentType = AgentType.SECURITY,
) -> Finding:
    return Finding(
        agent_type=agent_type,
        severity=severity,
        category="stub",
        file_path="src/app.py",
        line_start=1,
        line_end=1,
        confidence=Decimal(confidence),
        rationale="test finding",
    )


class TestRouteReviewBasicDecision:
    def test_high_confidence_no_critical_auto_posts(self) -> None:
        findings = [_finding(severity=Severity.LOW, confidence="0.900")]

        status, reason = route_review(Decimal("0.900"), findings, threshold=_DEFAULT_THRESHOLD)

        assert status == ReviewStatus.POSTED
        assert "auto-post" in reason.lower()

    def test_low_confidence_routes_to_human_review(self) -> None:
        findings = [_finding(severity=Severity.LOW, confidence="0.300")]

        status, reason = route_review(Decimal("0.300"), findings, threshold=_DEFAULT_THRESHOLD)

        assert status == ReviewStatus.QUEUED_FOR_HITL
        assert "human review" in reason.lower()

    def test_empty_findings_list_is_handled_sanely(self) -> None:
        """No findings, confidence 0.000 (per compute_overall_confidence) -> below
        threshold -> queued for human review, not a crash and not an auto-post."""
        status, reason = route_review(Decimal("0.000"), [], threshold=_DEFAULT_THRESHOLD)

        assert status == ReviewStatus.QUEUED_FOR_HITL
        assert reason  # non-empty, explains why


class TestCriticalAlwaysEscalates:
    def test_has_critical_finding_detects_critical_severity(self) -> None:
        assert has_critical_finding([_finding(severity=Severity.CRITICAL)]) is True
        assert has_critical_finding([_finding(severity=Severity.HIGH)]) is False
        assert has_critical_finding([]) is False

    def test_critical_finding_forces_human_review_even_at_confidence_one(self) -> None:
        """Confidence 1.0 would normally auto-post -- a CRITICAL finding overrides that."""
        findings = [_finding(severity=Severity.CRITICAL, confidence="1.000")]

        status, reason = route_review(Decimal("1.000"), findings, threshold=_DEFAULT_THRESHOLD)

        assert status == ReviewStatus.QUEUED_FOR_HITL
        assert "critical" in reason.lower()

    def test_critical_finding_forces_human_review_even_with_low_threshold(self) -> None:
        """Even a threshold of 0.000 (everything else would auto-post) doesn't save it."""
        findings = [_finding(severity=Severity.CRITICAL, confidence="1.000")]

        status, _reason = route_review(Decimal("1.000"), findings, threshold=Decimal("0.000"))

        assert status == ReviewStatus.QUEUED_FOR_HITL

    def test_non_critical_high_severity_does_not_force_escalation(self) -> None:
        """Only CRITICAL escalates unconditionally -- HIGH still goes through the threshold."""
        findings = [_finding(severity=Severity.HIGH, confidence="0.900")]

        status, _reason = route_review(Decimal("0.900"), findings, threshold=_DEFAULT_THRESHOLD)

        assert status == ReviewStatus.POSTED


class TestThresholdBoundary:
    """Off-by-one at the boundary is the classic bug this class exists to catch."""

    def test_exactly_at_threshold_auto_posts(self) -> None:
        """PLAN.md's success criteria says confidence 'below' the threshold escalates,
        which places the boundary value itself on the auto-post side."""
        findings = [_finding(severity=Severity.LOW, confidence="0.750")]

        status, reason = route_review(Decimal("0.750"), findings, threshold=Decimal("0.750"))

        assert status == ReviewStatus.POSTED
        assert "0.750" in reason

    def test_just_below_threshold_routes_to_human(self) -> None:
        findings = [_finding(severity=Severity.LOW, confidence="0.749")]

        status, _reason = route_review(Decimal("0.749"), findings, threshold=Decimal("0.750"))

        assert status == ReviewStatus.QUEUED_FOR_HITL

    def test_just_above_threshold_auto_posts(self) -> None:
        findings = [_finding(severity=Severity.LOW, confidence="0.751")]

        status, _reason = route_review(Decimal("0.751"), findings, threshold=Decimal("0.750"))

        assert status == ReviewStatus.POSTED


class TestReasonMessageAgreesWithConfiguredThreshold:
    """The DONE.html gate this class exists to satisfy: "aggregator threshold in
    code matches the threshold in its user-facing message." The reference
    implementation's real bug was code checking `>= 2` while the message said
    "3+ agents required" -- a literal drift between the comparison and the
    string. route_review interpolates the SAME `threshold` argument the
    comparison itself uses, so there is no second, independently-hardcoded
    number that could ever drift from it.
    """

    def test_reason_message_uses_the_configured_threshold_value(self) -> None:
        """Changing the threshold changes the message -- because the message IS the
        threshold, not a copy of it. If someone reintroduced a hardcoded number in
        the message (the reference bug), this test would catch it: the hardcoded
        text would not match str(threshold) for at least one of these two calls."""
        findings = [_finding(severity=Severity.LOW, confidence="0.700")]

        low_threshold = Decimal("0.620")
        status_low, reason_low = route_review(
            Decimal("0.700"), findings, threshold=low_threshold
        )
        assert status_low == ReviewStatus.POSTED
        assert str(low_threshold) in reason_low

        high_threshold = Decimal("0.910")
        status_high, reason_high = route_review(
            Decimal("0.700"), findings, threshold=high_threshold
        )
        assert status_high == ReviewStatus.QUEUED_FOR_HITL
        assert str(high_threshold) in reason_high

        # The two messages must differ -- proof the threshold actually flows
        # into the text rather than a static template being reused verbatim.
        assert reason_low != reason_high

    def test_critical_escalation_message_also_names_the_configured_threshold(self) -> None:
        """Even the CRITICAL-override reason string is threshold-aware, not a
        separate hardcoded template that could drift independently."""
        findings = [_finding(severity=Severity.CRITICAL, confidence="1.000")]
        threshold = Decimal("0.333")

        _status, reason = route_review(Decimal("1.000"), findings, threshold=threshold)

        assert str(threshold) in reason


class TestInMemoryHitlQueue:
    def test_enqueue_and_list_pending(self) -> None:
        queue = InMemoryHitlQueue()
        assert queue.size() == 0
        assert queue.list_pending() == []

        findings = [_finding(severity=Severity.CRITICAL, confidence="0.800")]

        review = Review(
            review_id="r1",
            pr_number=1,
            repository_owner="acme",
            repository_name="widgets",
            head_sha="a" * 40,
            findings=findings,
            overall_confidence=Decimal("0.800"),
            status=ReviewStatus.QUEUED_FOR_HITL,
            created_at=datetime.now(),
        )

        queue.enqueue(review)

        assert queue.size() == 1
        assert queue.list_pending() == [review]
