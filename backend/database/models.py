"""Typed representation of one ``agent_events`` table row.

Owns: ``EventType`` (the fixed vocabulary migration 0001's ``CHECK``
constraint also enforces in the database itself -- kept in exactly one
Python place so a call site can never construct a value the table would
reject, and so the Python list and the SQL ``CHECK`` list can only drift
apart if someone edits both and forgets to keep them in sync, not
silently) and ``AgentEvent`` (the pydantic model mirroring every column
``backend/database/migrations/0001_agent_events.sql`` creates).

Per ADR-002, ``backend.database`` follows the inward-only dependency rule:
this module only depends on the standard library and pydantic, never
outward toward ``backend.observability`` or any orchestrator/webhook code
-- ``backend.observability`` is the layer that depends on this one, not the
reverse (see that package's docstring).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """The five event kinds ``agent_events.event_type`` accepts.

    Mirrors migration 0001's ``CHECK (event_type IN (...))`` constraint
    exactly -- per the spec: a span starting, a span ending, an LLM call, a
    tool call, and a routing/ingress decision.
    """

    SPAN_START = "span.start"
    SPAN_END = "span.end"
    LLM_CALL = "llm.call"
    TOOL_CALL = "tool.call"
    DECISION = "decision"


class AgentEvent(BaseModel):
    """One append-only row of ``agent_events``.

    Attributes:
        review_id: Correlates this event with the review/run it belongs to.
            For orchestrator-sourced events this is
            ``backend.orchestrator.state.GraphState["review_id"]``; for
            webhook-ingress events (which precede any orchestrator run) it
            is a synthetic id built by
            ``backend.observability.workflow_context.run_id_for_delivery``.
        event_type: One of ``EventType``'s five values.
        ts: When the event occurred. Passed explicitly by every emitter in
            ``backend.observability.events`` (not left to the column's
            ``DEFAULT now()``) so call sites record the actual moment a
            span started/ended rather than whenever the INSERT happened to
            reach Postgres, and so tests can assert exact time-ordering
            deterministically.
        agent: Which specialist/component produced this event (e.g.
            "security", "aggregator"), or ``None`` for a webhook-ingress
            decision event that isn't attributed to any specialist.
        model: The LLM model name, for ``llm.call`` events; ``None``
            otherwise.
        tokens_in: Input token count, for ``llm.call`` events.
        tokens_out: Output token count, for ``llm.call`` events.
        cost_usd: Estimated cost in USD. ``NUMERIC(10, 6)`` -- 6 decimal
            places, the spec's pinned precision, so a sub-cent cost cannot
            silently round to ``$0.00``.
        latency_ms: Wall-clock duration of a span/call, in milliseconds.
        outcome: Free-text outcome label (e.g. "ok", "error", "POSTED",
            "QUEUED_FOR_HITL", "accepted", "duplicate", "rejected").
        confidence: ``NUMERIC(4, 3)`` -- 3 decimal places, the spec's
            pinned precision, matching
            ``backend.models.findings.Finding.confidence``'s own
            precision. Used by ``decision`` events to record the routing
            confidence.
        id: The row's primary key. ``None`` for an event not yet inserted
            (every ``emit_*`` call site constructs one this way); populated
            when read back via ``EventRepository.fetch_events_for_review``.
    """

    review_id: str = Field(min_length=1, max_length=200)
    event_type: EventType
    ts: datetime
    agent: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        decimal_places=6,
        max_digits=10,
        description="NUMERIC(10, 6): 6 decimal places, per the spec's pinned precision.",
    )
    latency_ms: int | None = Field(default=None, ge=0)
    outcome: str | None = Field(default=None, max_length=100)
    confidence: Decimal | None = Field(
        default=None,
        ge=Decimal("0.000"),
        le=Decimal("1.000"),
        decimal_places=3,
        max_digits=4,
        description="NUMERIC(4, 3): 3 decimal places, per the spec's pinned precision.",
    )
    id: int | None = Field(default=None, description="Primary key; set only on rows read back from the database.")
