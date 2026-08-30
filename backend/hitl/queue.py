"""The HITL confidence gate: routes a completed Review to auto-post or human review.

Owns two things:

1. ``route_review`` -- the pure decision function. Given a Review's
   deduplicated findings and its ``overall_confidence``, decide whether it
   auto-posts or is queued for human review, and produce the human-readable
   reason string explaining the decision. No I/O, no side effects, fully
   deterministic -- this is the "L7 gate" M5's outcome text refers to
   ("routes to 'post' or 'human_approval_queue'").

2. ``InMemoryHitlQueue`` -- the concrete "human_approval_queue" a
   ``QUEUED_FOR_HITL`` review lands in. Mirrors the existing
   ``backend.job_queue.InMemoryJobQueue`` pattern (M2/M3): a process-local
   stand-in, not durable, not shared across processes. A real persistent
   queue (Postgres-backed, surfaced through a dashboard) is out of scope
   here -- M5 only has to prove the routing *decision* is correct and
   testable without an LLM; a durable queue implementation is future work,
   same as M2's ``InMemoryJobQueue`` was a stand-in until M3's Redis queue.

Decision rule (the DoD gate this module exists to satisfy):
    auto-post   iff  overall_confidence >= threshold  AND  no CRITICAL finding
    human review otherwise (overall_confidence < threshold, OR any CRITICAL
    finding present, regardless of how high overall_confidence is)

Boundary rule, made explicit and tested (``tests/unit/test_hitl_gate.py``):
    overall_confidence exactly equal to the threshold auto-posts. The
    routing question is "is confidence *below* threshold", not "at or
    below" -- PLAN.md's M5 success criteria says a review "below" the
    threshold routes to HITL, which places the boundary value itself on the
    auto-post side.

Critical correctness requirement (this module's reason for existing as more
than a one-line ``if``): DONE.html's M5 gate specifically forbids a reason
string whose wording can drift from the number the code actually compares
against (the reference implementation's real bug: code checks
``critical_block_count >= 2`` while the message claims "3+ agents
required"). Every reason string built here interpolates the *same*
``threshold`` argument the comparison itself uses -- there is no
independent literal number anywhere in this module's strings. See
``tests/unit/test_hitl_gate.py::test_reason_message_uses_the_configured_threshold_value``
for a test that would fail if a future edit reintroduced a hardcoded number.
"""

from __future__ import annotations

from decimal import Decimal

from backend.models import Finding, Review, ReviewStatus, Severity


def has_critical_finding(findings: list[Finding]) -> bool:
    """True if any finding is CRITICAL severity."""
    return any(finding.severity == Severity.CRITICAL for finding in findings)


def route_review(
    overall_confidence: Decimal,
    findings: list[Finding],
    *,
    threshold: Decimal,
) -> tuple[ReviewStatus, str]:
    """Decide POSTED vs QUEUED_FOR_HITL for one review, with a matching reason.

    Args:
        overall_confidence: The review's ``compute_overall_confidence``
            result (see ``backend.models.review``).
        findings: The deduplicated findings the review is based on --
            only their severities matter here (for the CRITICAL check);
            confidence itself is already summarized in ``overall_confidence``.
        threshold: The configured ``HITL_CONFIDENCE_THRESHOLD``
            (``backend.core.settings.Settings.hitl_confidence_threshold``).
            Passed explicitly rather than read from settings internally so
            this function stays a pure, trivially-testable decision --
            callers (the aggregator node, tests) own reading configuration.

    Returns:
        A ``(status, reason)`` pair. ``reason`` always names the actual
        ``threshold`` value used, never a separately-hardcoded number.
    """
    if has_critical_finding(findings):
        reason = (
            "queued for human review: at least one CRITICAL finding is present "
            "(a CRITICAL finding always requires human review, regardless of "
            f"overall_confidence {overall_confidence} or the configured "
            f"HITL_CONFIDENCE_THRESHOLD of {threshold})"
        )
        return ReviewStatus.QUEUED_FOR_HITL, reason

    if overall_confidence < threshold:
        reason = (
            f"queued for human review: overall_confidence {overall_confidence} is "
            f"below the configured HITL_CONFIDENCE_THRESHOLD of {threshold}"
        )
        return ReviewStatus.QUEUED_FOR_HITL, reason

    reason = (
        f"auto-posted: overall_confidence {overall_confidence} meets or exceeds "
        f"the configured HITL_CONFIDENCE_THRESHOLD of {threshold}, and no "
        "CRITICAL finding is present"
    )
    return ReviewStatus.POSTED, reason


class InMemoryHitlQueue:
    """Process-local human approval queue holding reviews routed to HITL.

    Not thread-safe under real concurrent writers and not durable across
    process restarts -- the same accepted limitation ``InMemoryJobQueue``
    (M2) has, for the same reason: it exists to give the routing decision
    somewhere real to land, not to be M5's final production queue.
    """

    def __init__(self) -> None:
        self._pending: list[Review] = []

    def enqueue(self, review: Review) -> None:
        """Add ``review`` to the human approval queue.

        Callers are expected to only enqueue a review whose ``status`` is
        already ``QUEUED_FOR_HITL`` (i.e. after calling ``route_review``);
        this method does not re-derive or check that itself, since deciding
        *whether* to enqueue is ``route_review``'s job, not this queue's.
        """
        self._pending.append(review)

    def list_pending(self) -> list[Review]:
        """All reviews currently waiting for human approval, oldest first."""
        return list(self._pending)

    def size(self) -> int:
        """Number of reviews currently waiting for human approval."""
        return len(self._pending)
