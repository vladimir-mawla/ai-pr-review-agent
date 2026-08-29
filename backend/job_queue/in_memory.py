"""In-memory ``JobQueue`` implementation.

Owns: the M2 stand-in for a real queue. Holds enqueued jobs and the set of
delivery IDs already seen, both in process memory. This is deliberately not
durable and not shared across processes — it exists only so the ingress
handler has a real, working implementation to call behind the ``JobQueue``
interface until M3 replaces it with a Redis/ARQ-backed one.
"""

from __future__ import annotations

from backend.job_queue.interface import EnqueueResult, JobQueue
from backend.models import WebhookEvent


class InMemoryJobQueue(JobQueue):
    """Process-local job queue, idempotent per ``WebhookEvent.delivery_id``.

    Not thread-safe under real concurrent writers — acceptable for M2, where
    it backs a single FastAPI app instance in tests and a local demo run, not
    a multi-worker production deployment.
    """

    def __init__(self) -> None:
        self._seen_delivery_ids: set[str] = set()
        self._jobs: list[WebhookEvent] = []

    def enqueue(self, event: WebhookEvent) -> EnqueueResult:
        """Add ``event`` unless its delivery_id has already been enqueued."""
        if event.delivery_id in self._seen_delivery_ids:
            return EnqueueResult(enqueued=False, delivery_id=event.delivery_id)
        self._seen_delivery_ids.add(event.delivery_id)
        self._jobs.append(event)
        return EnqueueResult(enqueued=True, delivery_id=event.delivery_id)

    def size(self) -> int:
        """Return the number of distinct jobs enqueued so far."""
        return len(self._jobs)
