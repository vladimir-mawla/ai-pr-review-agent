"""Redis/ARQ-backed ``JobQueue`` implementation.

Owns: the M3 real queue. Two responsibilities, both required by the
``JobQueue`` contract (``backend.job_queue.interface``):

1. Idempotency, backed by an atomic Redis ``SET NX EX`` per
   ``WebhookEvent.delivery_id`` — a positive result means this call is the
   first to see that id (proceed to enqueue); a negative result means it
   was already seen (no-op). The TTL on that key is what fixes the
   M2-deferred finding that ``InMemoryJobQueue``'s ``_seen_delivery_ids``
   set grew without bound: every key placed in Redis by this class expires
   on its own, configurably, via ``Settings.idempotency_ttl_seconds``.
2. Handing the verified event off to ARQ so a separate worker process
   (``backend.job_queue.arq_worker``) can actually consume it.

Why a background event loop thread: ``JobQueue.enqueue`` is a synchronous
method — ``backend.webhook_receiver.router`` calls it as a plain
(non-awaited) call from inside an ``async def`` route, and M3 is
deliberately forbidden from changing that router (the M2 interface is
being swapped, not rewritten). ARQ's client (``arq.connections.ArqRedis``)
is async-only; there is no synchronous ``enqueue_job``. Reaching for
``asyncio.run()`` (or a fresh ``loop.run_until_complete()``) from inside a
callable that is itself already executing on uvicorn's running event loop
is a well-known hazard (nested-loop / "this event loop is already running"
failures, depending on exactly how it's invoked). The documented-safe way
to call async code from sync code in the same process is to run the async
side on its own dedicated thread with its own event loop, and bridge into
it with ``asyncio.run_coroutine_threadsafe`` — which is what this class
does. This also means the enqueue actually goes through ARQ's real
wire format (msgpack-encoded job data, its queue zset, etc.), so the ARQ
worker in ``arq_worker.py`` can consume it with no special-casing.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import cast

import redis
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.constants import default_queue_name

from backend.core.settings import Settings, get_settings
from backend.job_queue.interface import EnqueueResult, JobQueue
from backend.models import WebhookEvent

_IDEMPOTENCY_KEY_PREFIX = "pr-review-agent:webhook-delivery:"
_IDEMPOTENCY_VALUE = "1"

# Must match the function name arq_worker.WorkerSettings registers, so the
# worker actually has a handler for jobs this class enqueues.
ARQ_JOB_FUNCTION_NAME = "process_webhook_event"

_POOL_CREATE_TIMEOUT_SECONDS = 10.0
_ENQUEUE_TIMEOUT_SECONDS = 10.0
_CLOSE_TIMEOUT_SECONDS = 5.0


class RedisJobQueue(JobQueue):
    """Real job queue: Redis for idempotency, ARQ for the actual job hand-off.

    Not a drop-in for every conceivable ``JobQueue`` consumer forever —
    it opens a background thread + Redis connections at construction time
    and expects ``close()`` to be called at shutdown (FastAPI lifespan, or
    a test fixture's teardown) to release them cleanly. Tests that don't
    need Redis should keep using ``InMemoryJobQueue``.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._ttl_seconds = self._settings.idempotency_ttl_seconds

        # Plain synchronous client for the fast, atomic idempotency check
        # in the request path — no event-loop bridging needed for this
        # part since redis-py's default client is synchronous.
        self._redis_sync: redis.Redis = redis.Redis.from_url(self._settings.redis_url)

        # Dedicated background loop + thread so ARQ's async client can be
        # driven from this class's synchronous enqueue() method without
        # colliding with whatever event loop the caller (e.g. uvicorn) is
        # already running on its own thread.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="redis-job-queue-loop", daemon=True
        )
        self._loop_thread.start()
        self._arq_pool: ArqRedis = asyncio.run_coroutine_threadsafe(
            self._create_pool(), self._loop
        ).result(timeout=_POOL_CREATE_TIMEOUT_SECONDS)

    def _run_loop(self) -> None:
        """Thread target: install and run this instance's private event loop forever."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _create_pool(self) -> ArqRedis:
        """Build the ARQ connection pool. Runs on ``self._loop``, not the caller's thread."""
        return await create_pool(RedisSettings.from_dsn(self._settings.redis_url))

    def enqueue(self, event: WebhookEvent) -> EnqueueResult:
        """Enqueue ``event`` unless its ``delivery_id`` was already seen.

        The idempotency check is a single atomic ``SET key value NX EX
        ttl`` — "set only if not already set, with this expiry" in one
        round trip. A separate ``EXISTS`` followed by a ``SET`` would be a
        classic check-then-act race: two concurrent deliveries of the same
        id could both observe "not present" before either writes, and both
        would proceed to enqueue. ``SET ... NX EX ...`` is atomic in Redis
        (a single command), so exactly one caller ever observes "I set
        it" for a given key.
        """
        key = f"{_IDEMPOTENCY_KEY_PREFIX}{event.delivery_id}"
        was_newly_set = self._redis_sync.set(
            key, _IDEMPOTENCY_VALUE, nx=True, ex=self._ttl_seconds
        )
        if not was_newly_set:
            return EnqueueResult(enqueued=False, delivery_id=event.delivery_id)

        future = asyncio.run_coroutine_threadsafe(
            self._arq_pool.enqueue_job(
                ARQ_JOB_FUNCTION_NAME,
                event.model_dump(mode="json"),
            ),
            self._loop,
        )
        future.result(timeout=_ENQUEUE_TIMEOUT_SECONDS)
        return EnqueueResult(enqueued=True, delivery_id=event.delivery_id)

    def size(self) -> int:
        """Number of jobs currently waiting in ARQ's queue (its zset length)."""
        # redis-py's command mixin types `zcard`/`ttl` as `Awaitable[Any] |
        # Any` because the same method signatures are shared between its
        # sync and async client classes; the `cast` documents that this
        # class only ever uses the synchronous `redis.Redis` client, which
        # returns a plain int here, never a coroutine.
        return cast(int, self._redis_sync.zcard(default_queue_name))

    def ttl_for(self, delivery_id: str) -> int:
        """Return the current TTL (seconds) on ``delivery_id``'s idempotency key.

        Exposed for tests to assert the TTL was actually set (a positive
        value, not just that the key exists) rather than merely trusting
        the ``ex=`` argument was passed through correctly. Returns -2 if
        the key doesn't exist, -1 if it exists with no expiry (would be a
        bug in ``enqueue`` if ever observed).
        """
        key = f"{_IDEMPOTENCY_KEY_PREFIX}{delivery_id}"
        return cast(int, self._redis_sync.ttl(key))

    def close(self) -> None:
        """Release the ARQ pool, stop the background loop, and join its thread.

        Safe to call once at shutdown (FastAPI lifespan or a test
        fixture's teardown). Not called automatically — this class doesn't
        assume it's used as a context manager everywhere it's constructed.
        """
        close_future = asyncio.run_coroutine_threadsafe(self._arq_pool.aclose(), self._loop)
        close_future.result(timeout=_CLOSE_TIMEOUT_SECONDS)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=_CLOSE_TIMEOUT_SECONDS)
        self._redis_sync.close()

    def __del__(self) -> None:  # pragma: no cover - best-effort safety net only
        # Best-effort cleanup if a caller forgets close(); never raises.
        with contextlib.suppress(Exception):
            if self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
