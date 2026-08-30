"""database module.

Persistent storage layer for the events spine (M7). Owns the Postgres
connection/migration management (``postgres.py``), the typed row shape and
event-type vocabulary (``models.py``), and INSERT/SELECT-only access to
``agent_events`` (``repository.py``). Per ADR-002, this module follows the
inward-only dependency rule: dependencies point toward backend.core,
backend.models, and (since the L4-VERIFY-driven M7 fix) backend.reliability
-- the same sideways infrastructure-layer dependency
``backend.job_queue.redis_arq`` already has on ``backend.reliability`` --
never outward toward api, orchestrator, or observability --
``backend.observability`` is the layer that depends on this one, not the
reverse.
"""

from backend.database.models import AgentEvent, EventType
from backend.database.postgres import apply_migrations
from backend.database.repository import EventRepository

__all__ = [
    "AgentEvent",
    "EventType",
    "EventRepository",
    "apply_migrations",
]
