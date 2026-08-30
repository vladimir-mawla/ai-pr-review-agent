"""The events-emission API: what a live call site actually imports.

Owns: one ``emit_*`` function per ``backend.database.models.EventType``
value, plus this milestone's failure policy for all of them.

FAILURE POLICY: log-and-continue, not fail-open-silently
---------------------------------------------------------
Writing an audit/trace event must never take down the request path it is
observing: if the events Postgres is unreachable, a webhook POST must still
verify signatures, dedupe, and enqueue exactly as it would with a healthy
events database, and an orchestrator run must still produce a ``Review``.
So every ``emit_*`` function below catches exactly ``psycopg.Error`` (any
error raised because the database itself is unavailable, refused the
connection, or rejected the statement -- including, notably, our own
append-only trigger firing if a future bug ever attempted an UPDATE/DELETE
through this path) and ``OSError`` (a lower-level connect-timeout/network
failure that never reaches psycopg's own exception hierarchy), logs a
warning, and returns normally.

What this policy deliberately does NOT catch: a ``TypeError``,
``AttributeError``, or pydantic ``ValidationError`` raised while
*constructing* the ``AgentEvent`` itself (e.g. a caller passing a string
where ``tokens_in`` expects an int, or a value outside a field's bounds) is
a bug in our own code, not a database-availability problem -- it must be
allowed to propagate so a real bug is never silently swallowed alongside a
real outage. This is why every ``emit_*`` function constructs
``AgentEvent(...)`` OUTSIDE the ``try``/``except`` in ``_emit``, which only
ever wraps the already-validated event's actual database write.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import psycopg

from backend.database.models import AgentEvent, EventType
from backend.database.repository import EventRepository

logger = logging.getLogger(__name__)


def _emit(repository: EventRepository, event: AgentEvent) -> None:
    """Shared insert-and-swallow-DB-failure body every ``emit_*`` function calls.

    ``event`` is always a fully constructed (and therefore already
    pydantic-validated) ``AgentEvent`` by the time it reaches this
    function -- see the module docstring for why that ordering is what
    keeps a real bug in our own code from being mistaken for a database
    outage.
    """
    try:
        repository.insert_event(event)
    except (psycopg.Error, OSError) as exc:
        logger.warning(
            "failed to write %s event for review_id=%r: %s -- continuing without it",
            event.event_type.value,
            event.review_id,
            exc,
        )


def emit_span_start(repository: EventRepository, review_id: str, agent: str) -> None:
    """Record that ``agent``'s span began, right now."""
    event = AgentEvent(
        review_id=review_id,
        event_type=EventType.SPAN_START,
        ts=datetime.now(UTC),
        agent=agent,
    )
    _emit(repository, event)


def emit_span_end(
    repository: EventRepository,
    review_id: str,
    agent: str,
    *,
    latency_ms: int,
    outcome: str,
) -> None:
    """Record that ``agent``'s span ended, with its measured duration and outcome."""
    event = AgentEvent(
        review_id=review_id,
        event_type=EventType.SPAN_END,
        ts=datetime.now(UTC),
        agent=agent,
        latency_ms=latency_ms,
        outcome=outcome,
    )
    _emit(repository, event)


def emit_decision(
    repository: EventRepository,
    review_id: str,
    *,
    agent: str | None,
    outcome: str,
    confidence: Decimal | None = None,
) -> None:
    """Record a routing/ingress decision.

    Used for both the webhook-ingress decision (outcome one of "accepted",
    "duplicate", "rejected") and the orchestrator aggregator's final
    routing decision (outcome one of ``ReviewStatus``'s values, e.g.
    "POSTED"/"QUEUED_FOR_HITL", with ``confidence`` set to the review's
    ``overall_confidence``).
    """
    event = AgentEvent(
        review_id=review_id,
        event_type=EventType.DECISION,
        ts=datetime.now(UTC),
        agent=agent,
        outcome=outcome,
        confidence=confidence,
    )
    _emit(repository, event)


def emit_llm_call(
    repository: EventRepository,
    review_id: str,
    agent: str,
    *,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: Decimal,
    latency_ms: int,
) -> None:
    """Record a real LLM call's model/token/cost accounting.

    No live call site exists at M7 -- the four specialist nodes are still
    M4's canned stubs, and no real LLM call happens until M8 wires the
    first real agent. Built now (rather than left for M8 to invent) because
    the ``agent_events`` schema and this emission API are M7's whole scope,
    and M8's real agent should have a ready-made function to call instead
    of adding a sixth place that knows how to construct an ``AgentEvent``.
    Forward-looking infrastructure, the same category as M5's
    ``InMemoryHitlQueue`` or M6's ``CircuitBreaker`` registry -- see this
    milestone's Deferred notes.
    """
    event = AgentEvent(
        review_id=review_id,
        event_type=EventType.LLM_CALL,
        ts=datetime.now(UTC),
        agent=agent,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    _emit(repository, event)


def emit_tool_call(
    repository: EventRepository,
    review_id: str,
    agent: str,
    *,
    outcome: str,
    latency_ms: int,
) -> None:
    """Record a tool invocation.

    No live call site exists at M7 -- no agent calls any tool yet
    (``backend.tools`` is still an empty stub package). See
    ``emit_llm_call``'s docstring for why this is built now anyway.
    """
    event = AgentEvent(
        review_id=review_id,
        event_type=EventType.TOOL_CALL,
        ts=datetime.now(UTC),
        agent=agent,
        outcome=outcome,
        latency_ms=latency_ms,
    )
    _emit(repository, event)
