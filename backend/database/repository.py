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
synchronous client for its request-path call.

REVISED (L2 DEBUG, post-L4-REJECT): this module previously reasoned that
"events are supplementary telemetry, not a request the caller is blocked
waiting on the *result* of, so there is no need for the retry/circuit-
breaker/timeout composition [``RedisJobQueue``] wraps its Redis calls in."
That reasoning was wrong, and an independent L4 VERIFY session proved it
empirically: ``insert_event`` opens a *synchronous* ``psycopg.connect`` and
was called directly from ``async def receive_webhook``
(``backend.webhook_receiver.router``) with no offload -- so a slow/stalled
write did not merely "stall the caller waiting for a result", it blocked
the single uvicorn event-loop thread outright, serialising every other
concurrent, unrelated webhook request behind it. With an admin session
holding ``LOCK TABLE agent_events IN ACCESS EXCLUSIVE MODE``, three
concurrent independent webhook POSTs each took ~4.4s instead of the normal
sub-10ms. ``connect_timeout`` bounds only the TCP handshake, not query
execution (including time spent waiting on a lock) -- it did nothing to
bound that stall.

Two changes fix this, both reusing ``backend/reliability/`` rather than
hand-rolling equivalents (mirroring ``RedisJobQueue``'s own composition):

1. A real query-level bound: every connection this class opens now also
   sets Postgres's own ``statement_timeout`` GUC (via libpq's ``options``
   connection parameter), so a query that is executing *or waiting on a
   lock* for longer than ``statement_timeout_ms`` is cancelled by Postgres
   itself -- not merely "the client gave up an unspecified time later".
2. A ``CircuitBreaker`` (``backend.reliability.circuit_breaker``) wraps
   ``insert_event``'s actual connect-and-write, so once the events store
   has failed ``circuit_breaker_failure_threshold`` consecutive times, every
   call after that fails fast (``CircuitOpenError``) without even attempting
   a connection, for ``circuit_breaker_reset_timeout_seconds`` -- a
   persistently down/slow events store stops being retried, at full cost,
   on every single request.

Neither change moves the blocking call off the event-loop thread by
itself -- that is ``backend.observability.events.emit_decision_async``'s
job (offloading via ``asyncio.to_thread`` from the one async call site,
``backend.webhook_receiver.router``). This module's job is only to make
sure that once offloaded, one attempt is cheaply and genuinely bounded
rather than being free to block a worker thread indefinitely.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from math import ceil

import psycopg

from backend.database.models import AgentEvent, EventType
from backend.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, register

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

# M8 (L2 DEBUG, post-L4-REJECT): BudgetGuard's one read query. Deliberately
# NOT scoped to a single review_id (unlike _SELECT_BY_REVIEW_SQL above) -- a
# daily USD cap is a process-wide/organization-wide budget across every
# review that ran today, not a per-review one. `COALESCE(..., 0)` turns "no
# llm.call rows yet today" into a real 0 instead of SQL NULL, so callers
# never have to special-case "no spend recorded yet" as a separate branch
# from "$0.00 spent".
#
# `ts >= %s AND ts < %s` -- a genuine half-open `[day_start, day_start + 1
# day)` interval, BOTH bounds required. This query used to be open-ended
# (`ts >= %s` alone, under the old name `sum_llm_cost_since`), on the
# reasoning that "a real llm.call is never timestamped in the future, so an
# upper bound is unnecessary". That reasoning was wrong, and it caused two
# independent false BudgetExceededError failures in this project's own
# history before it was fixed here: the M8 builder session hit "spent
# $2119.000446 of $20 cap" from stray 2030-dated fixture rows an earlier
# test run had written to the same append-only table, and an independent L4
# VERIFY session hit "$40.00 of $20 cap" from its own 2099-dated boundary-
# test fixture rows -- reproducing the exact defect class a second time
# rather than merely re-pinning fixtures again. A future-dated row is not
# normal, valid spend (nothing in this system's real call path can produce
# `ts` ahead of `datetime.now(UTC)` -- see `backend.observability.events`),
# it is a data-integrity signal: a clock that is wrong, a fixture that
# leaked outside its test, or a bug. Silently counting it toward *today's*
# spend was the defect. This query's fix is to IGNORE it for today's sum
# (it simply falls outside every real day's `[day_start, day_start + 1
# day)` window until that day genuinely arrives) rather than raise or
# special-case it inline: `BudgetGuard.check_and_raise()` runs on this
# milestone's hot path (once per LLM call), so this query stays the single,
# cheap, already-necessary bounded scan it was before -- it does not grow a
# second unbounded "scan every row for anomalies" query alongside it. A
# future-dated row is silently, correctly excluded from every day's total
# it doesn't belong to; surfacing it as an operator-visible anomaly (e.g. a
# periodic off-path audit query) is left as follow-up infrastructure, not
# built here, since nothing has asked for it yet and it does not belong on
# this hot path.
_SUM_LLM_COST_FOR_DAY_SQL = """
    SELECT COALESCE(SUM(cost_usd), 0)
    FROM agent_events
    WHERE event_type = %s AND ts >= %s AND ts < %s
"""

# Name every EventRepository instance's circuit breaker is registered
# under. Mirrors RedisJobQueue's pattern (backend/job_queue/redis_arq.py):
# each instance gets its OWN CircuitBreaker object (so independent
# instances, e.g. one per test, never leak OPEN/HALF_OPEN state into each
# other), while `register()` still makes the most-recently-constructed
# instance's breaker discoverable by this name for a future /health route.
_CIRCUIT_BREAKER_NAME = "events_db"


class EventRepository:
    """Thin wrapper around one Postgres connection string, INSERT/SELECT only."""

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout_seconds: float = 2.0,
        statement_timeout_ms: int = 2000,
        circuit_breaker_failure_threshold: int = 5,
        circuit_breaker_reset_timeout_seconds: float = 30.0,
        search_path: str | None = None,
    ) -> None:
        self._dsn = dsn
        # libpq's `connect_timeout` parameter is defined as a whole number of
        # seconds (psycopg's own type stub pins it to `str | int | None`, not
        # `float`); round up to the nearest second (minimum 1) rather than
        # truncate, so a caller-supplied sub-second bound like `1.0` never
        # silently becomes a 0-second (effectively infinite/undefined) one.
        self._connect_timeout_seconds = max(1, ceil(connect_timeout_seconds))
        if statement_timeout_ms <= 0:
            raise ValueError(f"statement_timeout_ms must be positive, got {statement_timeout_ms}")
        # Bounds query *execution*, including time spent waiting to acquire
        # a lock -- exactly what connect_timeout does NOT cover, and exactly
        # what L4 VERIFY's ACCESS-EXCLUSIVE-lock experiment exploited. Set
        # via libpq's `options` connection parameter (`-c statement_timeout=
        # <ms>`) so it applies to every statement on the connection from the
        # moment it's established, including the very first one.
        self._statement_timeout_ms = statement_timeout_ms
        self._connect_options = f"-c statement_timeout={statement_timeout_ms}"
        # L2 DEBUG (test-isolation fix): every SQL string in this file
        # (_INSERT_SQL, _SELECT_BY_REVIEW_SQL, _SUM_LLM_COST_FOR_DAY_SQL)
        # refers to `agent_events` unqualified, so which actual table it
        # resolves to is entirely a function of the connection's Postgres
        # `search_path`. `search_path=None` (the default, used by every
        # production call site) leaves libpq's own default search_path
        # (`"$user", public`) untouched -- production behavior is
        # byte-for-byte unchanged from before this parameter existed.
        # Tests that need a disposable table with the SAME shape and
        # append-only triggers as production (see
        # tests/integration/test_budget_guard_events.py) pass a per-test-run
        # schema name here instead, so this same unqualified SQL transparently
        # resolves into that schema's own `agent_events` table rather than
        # production's -- no SQL string in this file needs to change, and no
        # production code path is touched.
        if search_path is not None:
            self._connect_options += f" -c search_path={search_path}"
        # Own breaker per instance -- see _CIRCUIT_BREAKER_NAME's comment.
        self._breaker = register(
            CircuitBreaker(
                CircuitBreakerConfig(
                    failure_threshold=circuit_breaker_failure_threshold,
                    reset_timeout_seconds=circuit_breaker_reset_timeout_seconds,
                ),
                name=_CIRCUIT_BREAKER_NAME,
            )
        )

    def insert_event(self, event: AgentEvent) -> None:
        """Append one row, through the circuit breaker.

        Raises ``psycopg.Error``/``OSError`` (a genuine attempt against
        Postgres failed or timed out) or
        ``backend.reliability.circuit_breaker.CircuitOpenError`` (the
        breaker is open -- no connection was even attempted) on failure.

        Deliberately does not catch anything itself -- ``backend.
        observability.events`` is the layer that decides whether a failure
        here should be logged-and-swallowed (the request-path policy) or
        allowed to propagate (e.g. a future batch/backfill script that
        *should* fail loudly). Keeping this method fail-loud keeps that
        decision in exactly one place instead of duplicating it here too.

        Also deliberately does not offload itself to a background thread --
        this method is exactly as blocking/synchronous as it looks. The one
        call site that must not block an event loop
        (``backend.webhook_receiver.router``, via
        ``backend.observability.events.emit_decision_async``) is
        responsible for calling this through ``asyncio.to_thread``; the
        orchestrator's call site (``backend.orchestrator.nodes``) already
        runs on a plain worker thread (LangGraph's own thread pool for a
        sync graph), not an asyncio event loop, so it calls this directly.
        """
        self._breaker.call(self._insert_event_once, event)

    def _insert_event_once(self, event: AgentEvent) -> None:
        """The actual connect-and-write, wrapped by ``insert_event`` in the breaker."""
        with psycopg.connect(
            self._dsn,
            connect_timeout=self._connect_timeout_seconds,
            autocommit=True,
            options=self._connect_options,
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

        Not routed through ``insert_event``'s circuit breaker or offloaded
        anywhere -- this is a read used by tests, the audit/trace-viewer
        query, and (not yet) a health route, never by the webhook request
        path this milestone's fix is scoped to. It still sets the same
        ``statement_timeout`` so a stray slow read cannot hang forever
        either.
        """
        with psycopg.connect(
            self._dsn,
            connect_timeout=self._connect_timeout_seconds,
            autocommit=True,
            options=self._connect_options,
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

    def sum_llm_cost_for_day(self, day_start: datetime) -> Decimal:
        """Total ``cost_usd`` of every ``llm.call`` event within one UTC day.

        The window is the half-open interval ``[day_start, day_start + 1
        day)`` -- ``day_start`` counts, ``day_start + 1 day`` (the instant
        the next day begins) does not. This is what makes it safe for
        ``backend.economics.budget.BudgetGuard`` to derive real spend from
        the events spine, rather than tracking an in-memory running total
        that would reset on every process restart (and disagree with any
        other process, e.g. a worker and the API server, spending against
        the same budget). This is the first real consumer of
        ``agent_events`` beyond the trace-viewer's per-review read above --
        see that method's docstring and M7's own Deferred notes for why the
        events spine exists at all.

        RENAMED from ``sum_llm_cost_since`` (L2 DEBUG, post-L4-REJECT): the
        old name and its `ts >= since`-only query promised (and delivered)
        an unbounded "everything from `since` onward" read, which is not
        what a daily budget check needs or what production ever safely got
        -- see ``_SUM_LLM_COST_FOR_DAY_SQL``'s comment for the real defect
        this caused twice. The new name says exactly what the method now
        does: sum one bounded day, not an open-ended tail.

        A plain ``SELECT ... SUM(...)`` -- still no ``UPDATE``/``DELETE``
        anywhere in this file, preserving the ``events-table-append-only``
        invariant's grep-provability (see this module's docstring). Not
        routed through ``insert_event``'s circuit breaker (this is a read,
        not the write path that milestone's fix was scoped to) but still
        sets the same ``statement_timeout`` so a stray slow aggregate query
        cannot hang forever either.
        """
        day_end = day_start + timedelta(days=1)
        with psycopg.connect(
            self._dsn,
            connect_timeout=self._connect_timeout_seconds,
            autocommit=True,
            options=self._connect_options,
        ) as conn:
            row = conn.execute(
                _SUM_LLM_COST_FOR_DAY_SQL, (EventType.LLM_CALL.value, day_start, day_end)
            ).fetchone()
        if row is None or row[0] is None:
            return Decimal("0")
        return Decimal(row[0])
