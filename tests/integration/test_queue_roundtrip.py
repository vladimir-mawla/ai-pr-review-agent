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

import asyncio
import hashlib
import hmac
import json
import threading
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
import redis as redis_sync
from arq.connections import RedisSettings
from arq.worker import Worker

from backend.api.main import create_app
from backend.core.settings import Settings, get_settings
from backend.database.repository import EventRepository
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


# ---------------------------------------------------------------------------
# Regression test for the L2 DEBUG HIGH PRIORITY defect (M7 L4 VERIFY,
# fixed in M6-scope code): a slow-but-not-down Redis must not serialize
# concurrent, unrelated webhook requests.
# ---------------------------------------------------------------------------

_SLOW_REDIS_SECRET = "slow-redis-l2-debug-secret"


def _sign(body: bytes, secret: str = _SLOW_REDIS_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _sample_payload(pr_number: int) -> dict[str, object]:
    return {
        "action": "opened",
        "pull_request": {
            "number": pr_number,
            "head": {"sha": "c" * 40},
        },
        "repository": {
            "name": "pr-review-agent",
            "owner": {"login": "myorg"},
        },
    }


def _webhook_headers(delivery_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
    }


class TestConcurrentWebhookEnqueuesAreNotSerializedBySlowRedis:
    """Regression test for the defect an independent L4 VERIFY session found
    living in M6-scope code, after M7's own L4 VERIFY fixed the identical
    defect class on the events-write path: ``backend.webhook_receiver.
    router``'s ``async def receive_webhook`` called ``queue.enqueue(event)``
    directly -- a plain, unawaited, synchronous call -- and
    ``RedisJobQueue.enqueue`` blocks the calling thread (via
    ``backend.reliability.timeout.await_future``'s
    ``future.result(timeout=...)``) whenever Redis is slow to respond. Since
    that call ran on uvicorn's single event-loop thread, one webhook whose
    Redis call was merely SLOW (not down) would stall every other
    concurrent, unrelated webhook request behind it -- the same mechanism
    M7's ``TestConcurrentWebhookWritesAreNotSerializedByALockedEventsTable``
    proved for the events write, and this test models the same way, using
    Redis's own ``DEBUG SLEEP`` (which blocks the entire, single-threaded
    Redis server for a fixed duration) in place of an ``ACCESS EXCLUSIVE``
    Postgres table lock.

    Configuration is deliberately short and deterministic so the test runs
    fast and is not sensitive to jitter/backoff: ``retry_max_attempts=1``
    (a single attempt, no backoff sleep -- each ``enqueue()`` call takes
    almost exactly ``reliability_timeout_seconds`` before giving up) and a
    high ``circuit_breaker_failure_threshold`` (so the breaker never opens
    mid-test and starts short-circuiting later attempts instantly, which
    would hide the very serialization this test exists to catch). The
    Redis sleep duration is set comfortably longer than
    ``NUM_CONCURRENT_REQUESTS * RELIABILITY_TIMEOUT_SECONDS`` so Redis is
    still genuinely asleep for every one of the concurrent attempts, in
    both the broken and fixed code paths.

    Against the pre-fix router (``queue.enqueue(event)`` called directly,
    not ``await enqueue_async(queue, event)``), this test fails both
    assertions below: each request can only begin its own Redis attempt
    once every earlier request's own attempt has *already* timed out (the
    event loop was blocked the whole time), so latencies stack up as
    ``1x, 2x, 3x, 4x`` the per-attempt timeout instead of all landing near
    ``1x`` -- see this session's final report for the actual failing/
    passing output captured by temporarily reverting the router to the
    old, unawaited call.
    """

    _REDIS_SLEEP_SECONDS = 6.0
    _RELIABILITY_TIMEOUT_SECONDS = 1.0
    _NUM_CONCURRENT_REQUESTS = 4

    @classmethod
    def _make_settings(cls) -> Settings:
        return Settings(
            github_webhook_secret=_SLOW_REDIS_SECRET,
            redis_url=_REDIS_URL,
            reliability_timeout_seconds=cls._RELIABILITY_TIMEOUT_SECONDS,
            retry_max_attempts=1,
            circuit_breaker_failure_threshold=10,
        )

    @staticmethod
    def _sleep_redis_in_background(seconds: float) -> None:
        """Runs on its own OS thread/connection: block the WHOLE Redis server.

        ``DEBUG SLEEP`` blocks Redis's single command-processing thread for
        ``seconds`` -- every client, not just this connection -- which is
        exactly the "slow but not down" condition the milestone asks for.
        Issued from a dedicated connection/thread (not the test's own
        asyncio loop) so it genuinely runs in the background while the test
        fires concurrent webhook requests. This call itself blocks until
        Redis wakes back up and answers it -- it is not a signal the caller
        can synchronize on for "sleep has started" (see
        ``_wait_until_redis_is_provably_asleep``, which is why this test
        does not use an ``Event`` set just before this call: a connection
        being open is not proof the SLEEP command has reached Redis yet).
        """
        client = redis_sync.Redis.from_url(_REDIS_URL)
        client.execute_command("DEBUG", "SLEEP", seconds)

    @staticmethod
    def _wait_until_redis_is_provably_asleep(max_wait_seconds: float) -> float:
        """Poll with short-timeout PINGs until one of them actually blocks.

        HARNESS WARNING mitigation: a connection being open on the
        background thread (or even that thread having called
        ``execute_command``) is not proof Redis has *started executing*
        ``DEBUG SLEEP`` yet -- this test's own probe could race ahead of it
        and observe a still-responsive server, giving a false "the slow
        condition is active" reading. Instead of guessing a fixed grace
        delay, this polls with a short per-attempt ``socket_timeout``: the
        first attempt that actually raises ``TimeoutError`` is unambiguous,
        direct proof the server is currently unresponsive, and its own
        elapsed wait is returned as evidence. A successful (non-timing-out)
        PING is expected on early iterations while the race is still live;
        it just means "try again", not "give up".
        """
        deadline = time.monotonic() + max_wait_seconds
        while time.monotonic() < deadline:
            probe = redis_sync.Redis.from_url(_REDIS_URL, socket_timeout=0.3)
            started = time.monotonic()
            try:
                probe.ping()
            except redis_sync.exceptions.TimeoutError:
                return time.monotonic() - started
            finally:
                probe.close()
        raise AssertionError(
            f"Redis never became unresponsive within {max_wait_seconds}s -- DEBUG "
            "SLEEP may not have taken effect (a test-setup problem, not the thing "
            "under test)"
        )

    async def test_concurrent_webhook_posts_are_not_serialized_by_a_slow_redis(
        self,
    ) -> None:
        # Build the queue (and therefore its ARQ connection pool + sync
        # Redis client) BEFORE Redis goes to sleep, not after. RedisJobQueue
        # construction itself talks to Redis (creating the ARQ pool) --
        # doing this while Redis is asleep would block construction for
        # (most of) the sleep duration and only start the actual webhook
        # requests once Redis had already woken back up, hiding the very
        # defect this test exists to catch.
        queue = RedisJobQueue(self._make_settings())
        event_repository = EventRepository(_BASE_SETTINGS.database_url)
        app = create_app(
            settings=self._make_settings(), job_queue=queue, event_repository=event_repository
        )

        sleep_thread = threading.Thread(
            target=self._sleep_redis_in_background,
            args=(self._REDIS_SLEEP_SECONDS,),
            daemon=True,
        )
        sleep_thread.start()
        try:
            # HARNESS WARNING mitigation: prove the slow condition is
            # genuinely active right now, via an INDEPENDENT connection,
            # rather than trusting that starting the background thread (or
            # even it having opened a connection) means Redis is actually
            # blocked yet -- see `_wait_until_redis_is_provably_asleep`'s
            # own docstring for the exact race this avoids.
            probe_elapsed = self._wait_until_redis_is_provably_asleep(max_wait_seconds=3.0)
            print(  # deliberate evidence output (tests/** is exempt from ruff's T20)
                f"[slow-redis probe] PING blocked for {probe_elapsed:.3f}s once observed "
                "unresponsive (proves DEBUG SLEEP is genuinely active, not merely issued)"
            )

            async def _one_request(index: int) -> tuple[float, int]:
                delivery_id = str(uuid.uuid4())
                body = json.dumps(_sample_payload(pr_number=80_000 + index)).encode()
                headers = {"X-Hub-Signature-256": _sign(body), **_webhook_headers(delivery_id)}
                start = time.monotonic()
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as client:
                    response = await client.post("/webhook", content=body, headers=headers)
                elapsed = time.monotonic() - start
                return elapsed, response.status_code

            try:
                overall_start = time.monotonic()
                results = await asyncio.gather(
                    *[_one_request(i) for i in range(self._NUM_CONCURRENT_REQUESTS)]
                )
                overall_elapsed = time.monotonic() - overall_start
            finally:
                queue.close()

            latencies = [elapsed for elapsed, _status in results]
            statuses = [status for _elapsed, status in results]
        finally:
            sleep_thread.join(timeout=self._REDIS_SLEEP_SECONDS + 5.0)
            assert not sleep_thread.is_alive(), (
                "DEBUG SLEEP background thread never finished -- the slow "
                "condition may have leaked into later tests"
            )

        print(
            f"[concurrency proof] per-request latencies={[round(latency, 3) for latency in latencies]}s "
            f"max={max(latencies):.3f}s overall_batch={overall_elapsed:.3f}s statuses={statuses} "
            f"(redis_sleep={self._REDIS_SLEEP_SECONDS}s, "
            f"reliability_timeout={self._RELIABILITY_TIMEOUT_SECONDS}s)"
        )

        # Every request must see the queue as unavailable (503, not a hang,
        # not a 200) -- Redis is genuinely unresponsive for the whole
        # window each attempt is bounded by reliability_timeout_seconds.
        assert statuses == [503] * self._NUM_CONCURRENT_REQUESTS, (
            f"expected all {self._NUM_CONCURRENT_REQUESTS} requests to see 503 while "
            f"Redis is asleep, got {statuses}"
        )

        # THE FIX: no individual request waits anywhere near
        # NUM_CONCURRENT_REQUESTS x the per-attempt timeout -- each
        # request's own Redis attempt is bounded by
        # reliability_timeout_seconds and runs on its own worker thread
        # (`enqueue_async`), concurrently with every other in-flight
        # request, not queued behind them on a blocked event loop.
        serialized_bound = (
            self._NUM_CONCURRENT_REQUESTS * self._RELIABILITY_TIMEOUT_SECONDS * 0.75
        )
        assert max(latencies) < serialized_bound, (
            f"slowest of {self._NUM_CONCURRENT_REQUESTS} concurrent requests took "
            f"{max(latencies):.2f}s (redis_sleep={self._REDIS_SLEEP_SECONDS}s, "
            f"reliability_timeout={self._RELIABILITY_TIMEOUT_SECONDS}s) -- looks like "
            "the enqueue call blocked the event loop instead of being offloaded to a "
            "worker thread"
        )
        # CONCURRENCY, NOT SERIALIZATION: N requests each individually
        # bounded by ~reliability_timeout_seconds must complete in close to
        # that same window, not N times it.
        assert overall_elapsed < serialized_bound, (
            f"{self._NUM_CONCURRENT_REQUESTS} concurrent requests took "
            f"{overall_elapsed:.2f}s in total for a batch that should complete in "
            "about one reliability-timeout window -- looks serialized on a single "
            "blocked event loop thread, not concurrent"
        )
