"""Review domain contract.

This module defines the Review model: aggregates all findings from all agents
for one PR review, along with overall confidence and routing status.

Why this shape: A Review is the complete result of analyzing one PR. It must
track the findings, compute overall confidence (weighted across agents), route
to posting or HITL based on that confidence and the presence of CRITICAL findings,
and record the final outcome status. This is the top-level contract that clients
(the API, dashboard, HITL queue) see.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from backend.models.enums import ReviewStatus
from backend.models.findings import Finding


class Review(BaseModel):
    """Aggregated review result for one pull request.

    Attributes:
        review_id: Unique identifier for this review (e.g., UUID or PR number + run_id).
        pr_number: GitHub PR number.
        repository_owner: Repository owner (username or org name).
        repository_name: Repository name.
        head_sha: The commit SHA at the head of the PR branch.
        findings: List of all findings from all agents, deduplicated and sorted.
        overall_confidence: Weighted average confidence across findings, in [0, 1].
                          Computed as the mean of all finding confidences.
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
        description="Mean confidence across all findings",
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
                    "overall_confidence": "0.750",
                    "status": "POSTED",
                    "created_at": "2025-01-15T10:30:00Z",
                    "posted_at": "2025-01-15T10:30:05Z",
                    "error_message": None,
                }
            ]
        }
    }
