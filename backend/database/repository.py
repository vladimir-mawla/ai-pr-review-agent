"""Read/write access to the ``agent_events`` table.

Owns: the only code in this repository that runs SQL against
``agent_events``. Every statement in this file is a ``SELECT`` or an
``INSERT`` -- there is no ``UPDATE`` or ``DELETE`` here, or anywhere else in
``backend/`` (outside ``backend/database/migrations/``, which is the schema
definition, not application code). That is what makes the
``events-table-append-only`` invariant grep-verifiable, not merely enforced
by the database trigger migration 0001 installs: the trigger is the
belt, this file's contents (or rather, its total absence of any
UPDATE/DELETE statement) is the suspenders.

Each call opens and closes its own short-lived connection rather than
holding a long-lived pool. This deliberately mirrors
``backend.job_queue.redis_arq.RedisJobQueue``'s pattern of a plain
synchronous client for its request-path call, but goes one step simpler:
events are supplementary telemetry, not a request the caller is blocked
waiting on the *result* of, so there is no need for the retry/circuit-
breaker/timeout composition that module wraps its Redis calls in. A short
``connect_timeout`` bounds how long a single write can stall the caller when
Postgres is unreachable; ``backend.observability.events`` is what decides
what happens after that (see its module docstring for the log-and-continue
failure policy).
"""

from __future__ import annotations

from math import ceil

import psycopg

from backend.database.models import AgentEvent, EventType

_INSERT_SQL = """
    INSERT INTO agent_events
        (review_id, event_type, ts, agent, model, tokens_in,
         tokens_out, cost_usd, latency_ms, outcome, confidence)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_SELECT_BY_REVIEW_SQL = """
    SELECT id, review_id, event_type, ts, agent, model, tokens_in,
           tokens_out, cost_usd, latency_ms, outcome, confidence
    FROM agent_events
    WHERE review_id = %s
    ORDER BY ts ASC, id ASC
"""


class EventRepository:
    """Thin wrapper around one Postgres connection string, INSERT/SELECT only."""

    def __init__(self, dsn: str, *, connect_timeout_seconds: float = 2.0) -> None:
        self._dsn = dsn
        # libpq's `connect_timeout` parameter is defined as a whole number of
        # seconds (psycopg's own type stub pins it to `str | int | None`, not
        # `float`); round up to the nearest second (minimum 1) rather than
        # truncate, so a caller-supplied sub-second bound like `1.0` never
        # silently becomes a 0-second (effectively infinite/undefined) one.
        self._connect_timeout_seconds = max(1, ceil(connect_timeout_seconds))

    def insert_event(self, event: AgentEvent) -> None:
        """Append one row. Raises ``psycopg.Error``/``OSError`` on failure.

        Deliberately does not catch anything itself -- ``backend.
        observability.events`` is the layer that decides whether a failure
        here should be logged-and-swallowed (the request-path policy) or
        allowed to propagate (e.g. a future batch/backfill script that
        *should* fail loudly). Keeping this method fail-loud keeps that
        decision in exactly one place instead of duplicating it here too.
        """
        with psycopg.connect(
            self._dsn, connect_timeout=self._connect_timeout_seconds, autocommit=True
        ) as conn:
            conn.execute(
                _INSERT_SQL,
                (
                    event.review_id,
                    event.event_type.value,
                    event.ts,
                    event.agent,
                    event.model,
                    event.tokens_in,
                    event.tokens_out,
                    event.cost_usd,
                    event.latency_ms,
                    event.outcome,
                    event.confidence,
                ),
            )

    def fetch_events_for_review(self, review_id: str) -> list[AgentEvent]:
        """Every event recorded for ``review_id``, in time order.

        The trace-reconstruction query PLAN.md's M7 outcome names ("a
        trace-viewer query reconstructs one review end-to-end from
        ``review_id`` alone") -- ``ORDER BY ts ASC, id ASC`` breaks any
        exact-timestamp tie deterministically by insertion order, since two
        events can share the same millisecond-resolution timestamp on a
        fast local run.
        """
        with psycopg.connect(
            self._dsn, connect_timeout=self._connect_timeout_seconds, autocommit=True
        ) as conn:
            rows = conn.execute(_SELECT_BY_REVIEW_SQL, (review_id,)).fetchall()
        return [
            AgentEvent(
                id=row[0],
                review_id=row[1],
                event_type=EventType(row[2]),
                ts=row[3],
                agent=row[4],
                model=row[5],
                tokens_in=row[6],
                tokens_out=row[7],
                cost_usd=row[8],
                latency_ms=row[9],
                outcome=row[10],
                confidence=row[11],
            )
            for row in rows
        ]
