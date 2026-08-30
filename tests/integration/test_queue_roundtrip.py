"""Integration tests for the real (Redis/ARQ) job queue -- M3's demo target.

Owns: proving ``RedisJobQueue`` and the ARQ worker
(``backend/job_queue/{redis_arq,arq_worker}.py``) actually work together
against a real Redis instance (``docker-compose.yml``'s ``redis``
service), not just that they satisfy their type signatures. Specifically:

- enqueue-then-consume: a job put on the queue is actually picked up and
  run by a real ARQ ``Worker``, not merely written to Redis.
- idempotency: enqueuing the same ``delivery_id`` twice produces exactly
  one job.
- the TTL fix for the M2-deferred "InMemoryJobQueue grows unboundedly"
  finding: the idempotency key's TTL is read back from Redis directly
  (via ``TTL``) and asserted positive and bounded, not merely assumed
  because ``ex=`` was passed somewhere.
- an interface-contract test shared across *both* ``JobQueue``
  implementations, so "the router didn't need to change, only the queue
  did" is verified by a test rather than only claimed in prose.

These tests need a real reachable Redis (``docker compose up -d redis``
from the repo root). They are skipped -- not failed -- when Redis is
unreachable, via a module-level ``skipif`` computed once at collection
time; they are not permanently ``xfail`` or otherwise disabled, so they
run for real whenever Redis is up (as it must be for this file to count
as a passing gate).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import redis as redis_sync
from arq.connections import RedisSettings
from arq.worker import Worker

from backend.core.settings import Settings, get_settings
from backend.job_queue.arq_worker import process_webhook_event
from backend.job_queue.in_memory import InMemoryJobQueue
from backend.job_queue.interface import EnqueueResult, JobQueue
from backend.job_queue.redis_arq import RedisJobQueue
from backend.models import WebhookEvent

_BASE_SETTINGS = get_settings()
_REDIS_URL = _BASE_SETTINGS.redis_url

# Worker burst-mode polling: fast enough to keep the test snappy, slow
# enough not to busy-spin against Redis before the job has actually landed.
_BURST_POLL_DELAY_SECONDS = 0.1


def _redis_reachable(redis_url: str) -> bool:
    """Best-effort check: can we actually reach Redis at ``redis_url`` right now."""
    try:
        client = redis_sync.Redis.from_url(redis_url, socket_connect_timeout=1)
        return bool(client.ping())
    except (redis_sync.RedisError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _redis_reachable(_REDIS_URL),
    reason=f"Redis not reachable at {_REDIS_URL} -- run `docker compose up -d redis` first",
)


def _make_event(delivery_id: str | None = None) -> WebhookEvent:
    """Build a minimal valid ``WebhookEvent`` for test purposes.

    Each call defaults to a fresh random ``delivery_id`` so tests are
    isolated from each other (and from leftover state from previous runs
    against the same shared Redis instance) without needing a full flush.
    """
    return WebhookEvent(
        action="opened",
        pr_number=1,
        repository_owner="acme",
        repository_name="widgets",
        head_sha="a" * 40,
        delivery_id=delivery_id or str(uuid.uuid4()),
        received_at="2026-08-30T00:00:00+00:00",
    )


@pytest.fixture
def redis_queue() -> Iterator[RedisJobQueue]:
    """A ``RedisJobQueue`` against the real configured Redis, closed after the test."""
    queue = RedisJobQueue(_BASE_SETTINGS)
    try:
        yield queue
    finally:
        queue.close()


async def _run_burst_worker() -> None:
    """Run a real ARQ worker in burst mode: process what's queued, then exit.

    Uses its own connection (``redis_settings=``, not a borrowed pool) so
    this always runs on the test's own asyncio loop, independent of
    ``RedisJobQueue``'s private background-thread loop.
    """
    worker = Worker(
        functions=[process_webhook_event],
        redis_settings=RedisSettings.from_dsn(_REDIS_URL),
        burst=True,
        poll_delay=_BURST_POLL_DELAY_SECONDS,
    )
    await worker.async_run()
    await worker.close()


class TestEnqueueThenWorkerProcesses:
    """A job put on the queue is actually retrievable/processed by the worker."""

    async def test_worker_processes_enqueued_job(self, redis_queue: RedisJobQueue) -> None:
        event = _make_event()
        result = redis_queue.enqueue(event)
        assert result.enqueued is True

        await _run_burst_worker()

        marker = redis_sync.Redis.from_url(_REDIS_URL).get(
            f"pr-review-agent:processed:{event.delivery_id}"
        )
        assert marker == b"1", "worker did not record that it processed the job"


class TestIdempotency:
    """The same delivery id enqueued twice results in exactly one job."""

    def test_same_delivery_id_enqueued_twice_results_in_one_job(
        self, redis_queue: RedisJobQueue
    ) -> None:
        event = _make_event()
        before = redis_queue.size()

        first = redis_queue.enqueue(event)
        after_first = redis_queue.size()

        second = redis_queue.enqueue(event)
        after_second = redis_queue.size()

        assert first.enqueued is True
        assert second.enqueued is False
        assert after_first == before + 1
        assert after_second == after_first, "duplicate delivery added a second job"


class TestIdempotencyTtl:
    """The idempotency key actually carries a positive, bounded TTL in Redis."""

    def test_ttl_is_set_on_idempotency_key(self) -> None:
        short_ttl_settings = Settings(
            github_webhook_secret="test-secret-for-ttl-assertion",
            redis_url=_REDIS_URL,
            idempotency_ttl_seconds=30,
        )
        queue = RedisJobQueue(short_ttl_settings)
        try:
            event = _make_event()
            result = queue.enqueue(event)
            assert result.enqueued is True

            ttl = queue.ttl_for(event.delivery_id)
            # Not just "the key exists" (ttl != -2) -- a real, positive,
            # bounded expiry matching what was configured (ttl != -1,
            # which would mean "no expiry ever set").
            assert 0 < ttl <= 30, f"expected a positive TTL <= 30s, got {ttl}"
        finally:
            queue.close()


def _make_in_memory_queue() -> JobQueue:
    return InMemoryJobQueue()


def _make_redis_queue() -> JobQueue:
    return RedisJobQueue(_BASE_SETTINGS)


@pytest.fixture(params=["in_memory", "redis"])
def any_job_queue(request: pytest.FixtureRequest) -> Iterator[JobQueue]:
    """Both ``JobQueue`` implementations, exercised through the same contract.

    This is what proves the M2->M3 swap claim: the router only ever
    depends on the ``JobQueue`` Protocol, and both concrete
    implementations satisfy the exact same contract it relies on.
    """
    queue: JobQueue = (
        _make_in_memory_queue() if request.param == "in_memory" else _make_redis_queue()
    )
    try:
        yield queue
    finally:
        close = getattr(queue, "close", None)
        if callable(close):
            close()


class TestJobQueueContract:
    """Both implementations must satisfy the same ``JobQueue`` contract."""

    def test_first_enqueue_of_a_delivery_id_succeeds(self, any_job_queue: JobQueue) -> None:
        event = _make_event()
        result = any_job_queue.enqueue(event)
        assert result == EnqueueResult(enqueued=True, delivery_id=event.delivery_id)

    def test_repeat_enqueue_of_same_delivery_id_is_a_noop(self, any_job_queue: JobQueue) -> None:
        event = _make_event()
        any_job_queue.enqueue(event)
        result = any_job_queue.enqueue(event)
        assert result == EnqueueResult(enqueued=False, delivery_id=event.delivery_id)

    def test_size_reflects_distinct_enqueued_jobs(self, any_job_queue: JobQueue) -> None:
        before = any_job_queue.size()
        any_job_queue.enqueue(_make_event())
        any_job_queue.enqueue(_make_event())
        after = any_job_queue.size()
        assert after == before + 2
