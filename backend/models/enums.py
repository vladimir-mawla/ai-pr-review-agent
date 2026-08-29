"""Enum definitions for the pr-review-agent domain.

This module defines all value-type enums used across the system:
- Severity: finding impact level (CRITICAL to INFO)
- AgentType: specialist agent category (SECURITY, QUALITY, TESTS, DOCS)
- ReviewStatus: overall review outcome state

Enums are immutable and part of the core domain contract.
Why: Enums centralize valid values and prevent stringly-typed fields, making
the system easier to reason about and less brittle to schema changes.
"""

from enum import Enum


class Severity(str, Enum):
    """Finding severity level, from highest to lowest impact.

    Used by agents to classify the impact of a code issue.
    Routes on this value for HITL gate (CRITICAL always escalates).
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class AgentType(str, Enum):
    """Specialist agent categories.

    Each agent handles a specific review domain:
    - SECURITY: vulnerability, injection, authentication, authorization issues
    - QUALITY: code smell, maintainability, design patterns, technical debt
    - TESTS: coverage gaps, test quality, missing fixtures
    - DOCS: documentation completeness, clarity, API contract documentation

    Used for attribution, routing, and aggregation by domain.
    """

    SECURITY = "SECURITY"
    QUALITY = "QUALITY"
    TESTS = "TESTS"
    DOCS = "DOCS"


class ReviewStatus(str, Enum):
    """Overall review outcome status.

    - POSTED: findings posted as a GitHub comment on the PR
    - QUEUED_FOR_HITL: low-confidence or CRITICAL findings routed to human queue
    - REJECTED: review failed validation before posting
    - ERROR: unexpected system error during review
    """

    POSTED = "POSTED"
    QUEUED_FOR_HITL = "QUEUED_FOR_HITL"
    REJECTED = "REJECTED"
    ERROR = "ERROR"
