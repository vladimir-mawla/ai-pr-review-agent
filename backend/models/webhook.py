"""Webhook event domain contract.

This module defines the WebhookEvent model: parsed and validated GitHub
pull_request webhook payload, plus delivery/idempotency metadata.

Why this shape: GitHub webhook payloads are large and deeply nested. We extract
only the fields actually needed by the system (action, PR number, repo, head SHA,
delivery UUID) and model them as a distinct contract. This makes the system
resilient to GitHub API shape changes and explicit about what we actually depend on.
The delivery_id enables idempotency checking per ADR-IDEMPOTENCY.
"""

from typing import Any

from pydantic import BaseModel, Field


class WebhookPullRequest(BaseModel):
    """Minimal PR fields from a GitHub webhook pull_request event."""

    number: int = Field(description="PR number")
    action: str = Field(description="Action that triggered the webhook (opened, synchronize, etc.)")
    head: dict[str, Any] = Field(
        description="Head branch commit info; we extract sha from here",
    )
    base: dict[str, Any] = Field(
        description="Base branch info; we extract repo details from here",
    )


class WebhookRepository(BaseModel):
    """Minimal repository fields from a GitHub webhook."""

    name: str = Field(description="Repository name")
    owner: dict[str, Any] = Field(description="Owner info; we extract login from here")


class WebhookEvent(BaseModel):
    """Parsed and validated GitHub pull_request webhook event.

    This is the ingress contract: what the webhook_receiver module validates
    and enqueues. Fields are extracted from GitHub's raw payload.

    Attributes:
        action: The webhook action (e.g., "opened", "synchronize", "reopened").
        pr_number: GitHub PR number (unique within the repo).
        repository_owner: Repository owner username or org name.
        repository_name: Repository name.
        head_sha: Full commit SHA of the PR head branch (40 hex chars).
        delivery_id: GitHub's X-GitHub-Delivery UUID; used for idempotency dedup.
        received_at: ISO 8601 timestamp when the webhook was received.
    """

    action: str = Field(
        min_length=1,
        max_length=50,
        description="GitHub webhook action (opened, synchronize, etc.)",
    )
    pr_number: int = Field(gt=0)
    repository_owner: str = Field(min_length=1, max_length=255)
    repository_name: str = Field(min_length=1, max_length=255)
    head_sha: str = Field(
        min_length=40,
        max_length=40,
        pattern="^[0-9a-f]{40}$",
        description="Full commit SHA (40 hex characters)",
    )
    delivery_id: str = Field(
        min_length=36,
        max_length=36,
        description="GitHub X-GitHub-Delivery UUID for idempotency",
    )
    received_at: str = Field(
        description="ISO 8601 timestamp when webhook was received",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "action": "opened",
                    "pr_number": 12345,
                    "repository_owner": "myorg",
                    "repository_name": "myapp",
                    "head_sha": "abc123def456abc123def456abc123def456abc1",
                    "delivery_id": "12345678-1234-1234-1234-123456789012",
                    "received_at": "2025-01-15T10:30:00Z",
                }
            ]
        }
    }
