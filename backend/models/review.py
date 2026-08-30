"""Review domain contract.

This module defines the Review model: aggregates all findings from all agents
for one PR review, along with overall confidence and routing status.

Why this shape: A Review is the complete result of analyzing one PR. It must
track the findings, compute overall confidence (weighted across agents), route
to posting or HITL based on that confidence and the presence of CRITICAL findings,
and record the final outcome status. This is the top-level contract that clients
(the API, dashboard, HITL queue) see.

M5 note -- closing the M1-deferred consistency gap
---------------------------------------------------
M1 shipped ``overall_confidence`` with no relationship to ``findings``: a
``Review`` could be constructed with findings averaging 0.500 and
``overall_confidence`` 0.000, and pydantic accepted it (see
``.genesis/checkpoints/CURRENT.md``'s Deferred section). Nothing consumed
the field yet, so the gap was recorded rather than fixed. M5's HITL gate
(``backend.hitl.queue.route_review``) is the first thing that actually
*trusts* ``overall_confidence`` to make a routing decision, so a value that
disagrees with the findings it claims to summarize would silently produce a
wrong human-vs-auto-post decision. ``compute_overall_confidence`` below is
the single formula, and ``Review``'s ``model_validator`` enforces that the
stored value always equals it -- construction raises ``ValidationError``
otherwise, rather than merely documenting the intended formula and hoping
callers compute it correctly. A validator (option B) was chosen over
silently recomputing the field from ``findings`` (option A) because option A
would let a caller pass an arbitrary ``overall_confidence`` and have it
silently discarded, which hides bugs instead of surfacing them; a validator
fails loudly at construction time, which fits this project's
fail-toward-doing-less philosophy better than a silent auto-correction would.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field, model_validator

from backend.models.enums import ReviewStatus
from backend.models.findings import Finding

# overall_confidence's declared field precision (decimal_places=3 below) --
# reused here so the rounding quantum used by compute_overall_confidence()
# can never drift out of sync with the field constraint it must satisfy.
_CONFIDENCE_QUANTUM = Decimal("0.001")


def compute_overall_confidence(findings: list[Finding]) -> Decimal:
    """Compute the one formula ``Review.overall_confidence`` must match.

    Formula: the arithmetic mean of every surviving finding's ``confidence``,
    rounded to 3 decimal places with ROUND_HALF_UP (a fixed, deterministic
    rounding rule -- not the ``decimal`` module's context-dependent default --
    so the same findings always produce the same overall_confidence
    regardless of caller or platform).

    An empty findings list has no evidence to average, so it is defined as
    ``Decimal("0.000")`` rather than left undefined or treated as "fully
    confident": "no findings" must never look like "high confidence" to the
    HITL gate. 0.000 is *below* the gate's default threshold (0.75), so an
    empty-findings Review still routes to human review by default -- the
    conservative, fail-toward-doing-less outcome the project's DoD calls
    for, not a guess dressed up as certainty.

    This function is the single source of truth for the formula: the
    aggregator (``backend.agents.contracts.dedupe_findings`` callers) and
    ``Review``'s own ``model_validator`` both call this, rather than each
    reimplementing "average the confidences" and risking the two drifting
    apart.
    """
    if not findings:
        return Decimal("0.000")
    total = sum((finding.confidence for finding in findings), start=Decimal("0"))
    mean = total / len(findings)
    return mean.quantize(_CONFIDENCE_QUANTUM, rounding=ROUND_HALF_UP)


class Review(BaseModel):
    """Aggregated review result for one pull request.

    Attributes:
        review_id: Unique identifier for this review (e.g., UUID or PR number + run_id).
        pr_number: GitHub PR number.
        repository_owner: Repository owner (username or org name).
        repository_name: Repository name.
        head_sha: The commit SHA at the head of the PR branch.
        findings: List of all findings from all agents, deduplicated and sorted.
        overall_confidence: The arithmetic mean of every finding's confidence,
                          rounded to 3 decimal places with ROUND_HALF_UP (0.000
                          when findings is empty) -- see
                          ``compute_overall_confidence`` above for the exact
                          formula. Enforced, not just documented: a
                          ``model_validator`` rejects any ``Review`` whose
                          ``overall_confidence`` does not equal
                          ``compute_overall_confidence(findings)``.
        status: Outcome of this review (POSTED, QUEUED_FOR_HITL, REJECTED, ERROR).
        created_at: ISO 8601 timestamp of when the review was created.
        posted_at: ISO 8601 timestamp when posted to GitHub (if status == POSTED).
        error_message: If status == ERROR, the exception message.
    """

    review_id: str = Field(min_length=1, max_length=100)
    pr_number: int = Field(gt=0)
    repository_owner: str = Field(min_length=1, max_length=255)
    repository_name: str = Field(min_length=1, max_length=255)
    head_sha: str = Field(
        min_length=40,
        max_length=40,
        description="Full commit SHA (40 hex chars)",
    )
    findings: list[Finding] = Field(default_factory=list, description="All findings, sorted")
    overall_confidence: Decimal = Field(
        ge=Decimal("0.000"),
        le=Decimal("1.000"),
        decimal_places=3,
        description=(
            "Mean confidence across all findings, rounded with ROUND_HALF_UP "
            "(0.000 when findings is empty); must equal "
            "compute_overall_confidence(findings) or construction fails"
        ),
    )
    status: ReviewStatus
    created_at: datetime
    posted_at: datetime | None = None
    error_message: str | None = Field(None, max_length=10000)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "review_id": "ghpr-12345-run-001",
                    "pr_number": 12345,
                    "repository_owner": "myorg",
                    "repository_name": "myapp",
                    "head_sha": "0000000000000000000000000000000000000000",
                    "findings": [],
                    "overall_confidence": "0.000",
                    "status": "QUEUED_FOR_HITL",
                    "created_at": "2025-01-15T10:30:00Z",
                    "posted_at": None,
                    "error_message": None,
                }
            ]
        }
    }

    @model_validator(mode="after")
    def _check_overall_confidence_matches_findings(self) -> "Review":
        # See the module docstring's "M5 note" for why this is a hard
        # rejection rather than a silent recompute: this is the fix for the
        # M1-deferred gap where overall_confidence had no relationship to
        # findings at all.
        expected = compute_overall_confidence(self.findings)
        if self.overall_confidence != expected:
            raise ValueError(
                f"overall_confidence ({self.overall_confidence}) does not match "
                f"compute_overall_confidence(findings) ({expected}); construct "
                "Review with overall_confidence=compute_overall_confidence(findings) "
                "rather than an independently-chosen value"
            )
        return self
