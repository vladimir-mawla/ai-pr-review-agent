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

L2 DEBUG note (HIGH PRIORITY item from M7's L4 VERIFY, fixed post-M7):
``enqueue`` itself is deliberately untouched by that fix -- it is still
this exact synchronous method, still bridging onto this class's own
private event-loop thread exactly as described above. What changed is
only how ``backend.webhook_receiver.router`` *calls* it: no longer a bare
unawaited ``queue.enqueue(event)``, but ``await
backend.job_queue.interface.enqueue_async(queue, event)``, which runs this
same method on a worker thread via ``asyncio.to_thread`` so a slow Redis
call blocks only that request's own task, not uvicorn's shared event loop.
See ``backend.job_queue.interface``'s module docstring and
``enqueue_async``'s own docstring for the full defect and why this
class's synchronous contract (and its own internal thread-bridge, which
is still required for the sync/async boundary described in this
docstring) was kept unchanged rather than reworking ``JobQueue`` into an
async Protocol.

M6 reliability layer, wired in here (not merely unit-tested in isolation —
see ``.genesis/DONE.html`` section 2's "live call site" gate): both real
Redis operations this class performs (the idempotency ``SET`` and the
cross-thread ARQ enqueue) go through ``_call_reliably``, which composes, in
order from outermost to innermost:

    retry (backend.reliability.retry.call_with_retry)
      -> circuit breaker (backend.reliability.circuit_breaker.CircuitBreaker)
        -> timeout (backend.reliability.timeout.run_with_timeout / await_future)
          -> the actual Redis call

Retry is outermost and circuit breaker inner (not the other way around) so
that once the breaker opens, ``CircuitOpenError`` is raised by the very
next attempt inside the retry loop — and because it's registered as
non-retryable, the retry loop re-raises it immediately instead of sleeping
and trying again. This is what makes "fail fast while open" actually fast:
a caller who arrives while the breaker is open pays for exactly one cheap,
in-process exception, never a Redis round trip. Both real failure modes
(retries exhausted, or the breaker already open) are translated to the same
``QueueUnavailableError`` so ``backend.webhook_receiver.router`` can answer
with a 503 without needing to know which of the two happened.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from typing import TypeVar, cast

import redis
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.constants import default_queue_name

from backend.core.settings import Settings, get_settings
from backend.job_queue.interface import EnqueueResult, JobQueue, QueueUnavailableError
from backend.models import WebhookEvent
from backend.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    register,
)
from backend.reliability.retry import RetryExhaustedError, RetryPolicy, call_with_retry
from backend.reliability.timeout import TimeoutPolicy, await_future, run_with_timeout

T = TypeVar("T")

_IDEMPOTENCY_KEY_PREFIX = "pr-review-agent:webhook-delivery:"
_IDEMPOTENCY_VALUE = "1"

# Must match the function name arq_worker.WorkerSettings registers, so the
# worker actually has a handler for jobs this class enqueues.
ARQ_JOB_FUNCTION_NAME = "process_webhook_event"

# Pool creation and close() are startup/shutdown housekeeping, not
# request-path calls a webhook POST is waiting on -- they stay outside the
# M6 retry/breaker/timeout composition (which guards the two per-request
# Redis calls below) and keep their own fixed, generous timeouts.
_POOL_CREATE_TIMEOUT_SECONDS = 10.0
_CLOSE_TIMEOUT_SECONDS = 5.0

# Name this queue's circuit breaker is registered under -- what a future
# /health endpoint would look it up by, and what appears in a
# CircuitOpenError's message.
_CIRCUIT_BREAKER_NAME = "redis_job_queue"

# Exceptions that mean "the call itself was malformed", not "the
# dependency is struggling" -- retrying these can never help. Extends
# retry's own default set with CircuitOpenError: once the breaker is open,
# retrying is the opposite of failing fast.
_NON_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    CircuitOpenError,
)


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

        # M6 reliability policies, built once from Settings so every call
        # this instance makes uses the same configured attempts/backoff/
        # timeout/breaker knobs (see backend/core/settings.py). Each
        # instance gets its OWN CircuitBreaker (not a shared module-level
        # singleton) so independent RedisJobQueue instances -- e.g. in
        # tests that construct several -- don't leak OPEN/HALF_OPEN state
        # into each other; the process-wide `register()` call still makes
        # this instance's breaker discoverable by name for a future
        # /health endpoint (the most-recently-constructed instance's
        # breaker is what that registry entry reflects).
        self._retry_policy = RetryPolicy(
            max_attempts=self._settings.retry_max_attempts,
            base_delay_seconds=self._settings.retry_base_delay_seconds,
            max_delay_seconds=self._settings.retry_max_delay_seconds,
        )
        self._timeout_policy = TimeoutPolicy(seconds=self._settings.reliability_timeout_seconds)
        self._breaker = register(
            CircuitBreaker(
                CircuitBreakerConfig(
                    failure_threshold=self._settings.circuit_breaker_failure_threshold,
                    reset_timeout_seconds=self._settings.circuit_breaker_reset_timeout_seconds,
                ),
                name=_CIRCUIT_BREAKER_NAME,
            )
        )

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

    def _call_reliably(self, func: Callable[[], T]) -> T:
        """Run one real Redis operation through retry -> circuit breaker -> (timeout inside func).

        ``func`` must already enforce its own timeout internally (via
        ``run_with_timeout``/``await_future`` — see ``_set_idempotency_key``
        and ``_enqueue_arq_job`` below), since the timeout bound differs by
        call shape (a plain synchronous call vs. an already-in-flight
        future). This method adds the two layers that are the same for
        both: retrying a transient failure with backoff, and giving up
        immediately (no retry, no further Redis attempt) once the breaker
        has opened.

        Raises ``QueueUnavailableError`` — never lets a raw
        ``RetryExhaustedError``/``CircuitOpenError`` escape this class — so
        ``backend.webhook_receiver.router`` has exactly one exception type
        to catch regardless of which of the two actually happened.
        """
        try:
            return call_with_retry(
                lambda: self._breaker.call(func),
                policy=self._retry_policy,
                non_retryable_exceptions=_NON_RETRYABLE_EXCEPTIONS,
            )
        except (RetryExhaustedError, CircuitOpenError) as exc:
            raise QueueUnavailableError(
                "job queue is temporarily unavailable (Redis unreachable "
                f"or circuit breaker open): {exc}"
            ) from exc

    def _set_idempotency_key(self, key: str) -> bool:
        """The idempotency check as a single atomic ``SET key value NX EX ttl``.

        "Set only if not already set, with this expiry" in one round trip
        — a separate ``EXISTS`` followed by a ``SET`` would be a classic
        check-then-act race: two concurrent deliveries of the same id could
        both observe "not present" before either writes, and both would
        proceed to enqueue. ``SET ... NX EX ...`` is atomic in Redis (a
        single command), so exactly one caller ever observes "I set it"
        for a given key. Wrapped in ``run_with_timeout`` since redis-py's
        synchronous client has no timeout of its own on this call.
        """
        return bool(
            run_with_timeout(
                lambda: self._redis_sync.set(key, _IDEMPOTENCY_VALUE, nx=True, ex=self._ttl_seconds),
                policy=self._timeout_policy,
            )
        )

    def _enqueue_arq_job(self, event: WebhookEvent) -> None:
        """Hand ``event`` to ARQ across the sync/async thread bridge, with a real bound.

        Replaces the old hardcoded ``future.result(timeout=10)`` — the
        wait is still on the same kind of future (from
        ``asyncio.run_coroutine_threadsafe``), just bounded through the
        shared, configurable ``await_future`` primitive instead of a
        magic number local to this method.
        """
        future = asyncio.run_coroutine_threadsafe(
            self._arq_pool.enqueue_job(
                ARQ_JOB_FUNCTION_NAME,
                event.model_dump(mode="json"),
            ),
            self._loop,
        )
        await_future(future, self._timeout_policy)

    def enqueue(self, event: WebhookEvent) -> EnqueueResult:
        """Enqueue ``event`` unless its ``delivery_id`` was already seen.

        Both real Redis operations below go through ``_call_reliably`` —
        retry-with-backoff wrapping a circuit breaker wrapping a bounded
        timeout — rather than calling Redis/ARQ directly. See this
        module's docstring for why retry is outermost and the exact
        composition order.
        """
        key = f"{_IDEMPOTENCY_KEY_PREFIX}{event.delivery_id}"
        was_newly_set = self._call_reliably(lambda: self._set_idempotency_key(key))
        if not was_newly_set:
            return EnqueueResult(enqueued=False, delivery_id=event.delivery_id)

        self._call_reliably(lambda: self._enqueue_arq_job(event))
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
