"""ARQ worker process for the real job queue.

Owns: the consumer side of the M3 hand-off. ``RedisJobQueue.enqueue``
(``backend/job_queue/redis_arq.py``) puts verified webhook events onto
ARQ's queue; this module is what actually dequeues and does something
with them, run as a separate OS process via ``arq
backend.job_queue.arq_worker.WorkerSettings``.

At M3, "does something with them" is deliberately minimal: record that the
job arrived. The real multi-agent review workflow (LangGraph orchestration,
specialist agents, aggregation, HITL routing) is M4+ scope and is not
built here — this handler is a stub proving the async hand-off works, not
a place to start building agent logic.
"""

from __future__ import annotations

import logging
from typing import Any

from arq.connections import RedisSettings

from backend.core.settings import get_settings

logger = logging.getLogger(__name__)

# A short-lived marker so a test (or a human watching Redis) can observe
# that a specific job actually ran, without needing to parse ARQ's own
# result-keeping. Kept short (1 hour) since it exists only to prove
# "this job was processed just now", not as durable state — unlike the
# idempotency keys in redis_arq.py, this is not a correctness mechanism.
_PROCESSED_MARKER_PREFIX = "pr-review-agent:processed:"
_PROCESSED_MARKER_TTL_SECONDS = 60 * 60


async def process_webhook_event(ctx: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """M3 stub job handler: log and record that a webhook event was received.

    Args:
        ctx: ARQ's per-worker context dict; ``ctx["redis"]`` is the same
            ``ArqRedis`` connection the worker uses internally, provided
            by ARQ so handlers can talk to Redis without opening their own
            connection.
        event: The ``WebhookEvent`` as enqueued, i.e.
            ``WebhookEvent.model_dump(mode="json")`` from
            ``RedisJobQueue.enqueue`` — a plain JSON-compatible dict
            (ARQ's own serializer, not this project's, encodes/decodes the
            job payload), not a ``WebhookEvent`` instance. Re-validating it
            back into a ``WebhookEvent`` is deferred to M4, where the real
            handler will need the typed fields to drive the orchestrator.

    Returns:
        A small JSON-compatible summary. ARQ stores whatever a job
        function returns as that job's result (see ``keep_result`` on
        ``WorkerSettings``/``Worker``); at M3 nothing reads this back
        except tests asserting the handler ran.
    """
    delivery_id = event.get("delivery_id", "unknown")
    logger.info("processing webhook job", extra={"delivery_id": delivery_id})

    redis_conn = ctx["redis"]
    await redis_conn.set(
        f"{_PROCESSED_MARKER_PREFIX}{delivery_id}",
        "1",
        ex=_PROCESSED_MARKER_TTL_SECONDS,
    )
    return {"received": True, "delivery_id": delivery_id}


class WorkerSettings:
    """ARQ ``WorkerSettings``: what ``arq backend.job_queue.arq_worker.WorkerSettings`` runs.

    ``functions`` is the set of job functions this worker knows how to
    run — just the one M3 stub for now. ``redis_settings`` points the
    worker at the same Redis instance ``RedisJobQueue`` enqueues onto
    (``Settings.redis_url``, read fresh at class-definition/import time
    from the environment, matching how ARQ's CLI expects to find this
    attribute on the class itself rather than an instance).
    """

    functions = [process_webhook_event]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
