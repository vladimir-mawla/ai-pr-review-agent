"""Enum definitions for the pr-review-agent domain.

This module defines all value-type enums used across the system:
- Severity: finding impact level (CRITICAL to INFO)
- AgentType: specialist agent category (SECURITY, QUALITY, TESTS, DOCS)
- ReviewStatus: overall review outcome state

Enums are immutable and part of the core domain contract.
Why: Enums centralize valid values and prevent stringly-typed fields, making
the system easier to reason about and less brittle to schema changes.
"""

from enum import StrEnum


class Severity(StrEnum):
    """Finding severity level, from highest to lowest impact.

    Used by agents to classify the impact of a code issue.
    Routes on this value for HITL gate (CRITICAL always escalates).

    Member declaration order here happens to already run
    highest-to-lowest impact, but a ``StrEnum``'s declaration order is not
    a load-bearing ordering contract by itself -- nothing stops a future
    edit from reordering these members (e.g. alphabetizing them) without
    realizing anything depends on the order. Any code that needs to rank
    severities (dedup tie-breaking, sorting) must import and use
    ``SEVERITY_RANK`` below instead of relying on declaration position or
    member name, so the ranking survives a reordering here.
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


# The single canonical severity ranking: lower rank = higher impact = wins
# any comparison. Deliberately explicit (not derived from enum declaration
# order or member name) per the note on Severity above -- anything that
# needs to compare two severities (today: backend.agents.contracts's
# dedup tie-break, and Finding.__lt__) must use this map, so there is
# exactly one place that defines "CRITICAL outranks HIGH outranks MEDIUM
# outranks LOW outranks INFO" and every consumer agrees with it.
SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


class AgentType(StrEnum):
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


class ReviewStatus(StrEnum):
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
