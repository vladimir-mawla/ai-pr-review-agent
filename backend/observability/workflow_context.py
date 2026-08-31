"""Shared context helpers correlating events with the run they belong to.

Owns: the small glue that lets both call sites this milestone wires
(``backend.webhook_receiver.router`` and ``backend.orchestrator.nodes``)
agree on one ``review_id`` string per emitted event, and a process-wide
``EventRepository`` singleton so neither call site has to construct its own
Postgres connection details.
"""

from __future__ import annotations

from functools import lru_cache

from backend.core.settings import get_settings
from backend.database.repository import EventRepository


def run_id_for_delivery(delivery_id: str) -> str:
    """Build the ``review_id`` used to correlate webhook-ingress events.

    No ``Review`` or orchestrator run exists yet at webhook-ingress time --
    that starts later, once a job is dequeued by a worker (M3's ARQ hand-
    off). ``delivery_id`` (GitHub's own per-delivery UUID, already required
    for idempotency -- see ``backend.job_queue.redis_arq``) is the only
    identifier available at ingress time, so events for an accepted,
    duplicate, or rejected webhook delivery are correlated by it instead of
    a review id that does not exist yet at this point in the pipeline.
    """
    return f"webhook-{delivery_id}"


@lru_cache
def get_event_repository() -> EventRepository:
    """Process-wide ``EventRepository``, built once from ``Settings``.

    Cached like ``backend.core.settings.get_settings`` for the same reason:
    the connection string does not change within a process's lifetime, and
    every call site (webhook router, orchestrator nodes) should share one
    configuration rather than each reading ``Settings`` independently. Also
    threads through the M7 L2 DEBUG fix's dedicated reliability knobs
    (``events_statement_timeout_ms`` and its own circuit breaker
    thresholds), mirroring ``backend.api.main._default_event_repository``.

    M12: uses ``settings.effective_database_url``, not the raw
    ``database_url`` field, so ``EVENTS_BACKEND=tiger`` routes this
    singleton at the real Tiger Cloud hypertable's restricted writer role
    -- see ``Settings.effective_database_url``'s docstring. Unchanged
    behavior when ``events_backend='local'`` (the default).
    """
    settings = get_settings()
    return EventRepository(
        settings.effective_database_url,
        statement_timeout_ms=settings.events_statement_timeout_ms,
        circuit_breaker_failure_threshold=settings.events_circuit_breaker_failure_threshold,
        circuit_breaker_reset_timeout_seconds=settings.events_circuit_breaker_reset_timeout_seconds,
        events_backend=settings.events_backend,
    )
