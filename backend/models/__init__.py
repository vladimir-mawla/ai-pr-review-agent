"""Domain models and contracts for the pr-review-agent.

This module defines the core data types used throughout the system:
- Enums: Severity, AgentType, ReviewStatus
- Finding: single code issue discovered by an agent
- Review: aggregated findings + overall outcome for one PR
- WebhookEvent: parsed GitHub webhook payload + metadata

Per ADR-002, backend.models MUST NOT import from any outer layer (api,
orchestrator, agents, integrations). It is a pure domain layer that all others
depend on.

Why this shape: Centralizing domain contracts in one module makes the system
easy to reason about, version independently, and use as a shared interface
between frontend (dashboard) and backend.
"""

from backend.models.enums import AgentType, ReviewStatus, Severity
from backend.models.findings import Finding
from backend.models.review import Review
from backend.models.webhook import WebhookEvent

__all__ = [
    "Severity",
    "AgentType",
    "ReviewStatus",
    "Finding",
    "Review",
    "WebhookEvent",
]
