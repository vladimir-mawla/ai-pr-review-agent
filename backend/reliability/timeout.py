"""Bounded-wait primitives: the timeout leg of the reliability layer.

Owns: turning "block until this finishes" into "block until this finishes,
*or* this many seconds, whichever comes first" -- for both of the two shapes
an outbound call can take in this codebase:

1. A ``concurrent.futures.Future`` already in flight (this is exactly what
   ``backend.job_queue.redis_arq.RedisJobQueue`` gets back from
   ``asyncio.run_coroutine_threadsafe`` when it bridges into its private
   event-loop thread to talk to ARQ/Redis). Before M6 that future's wait was
   a hardcoded ``future.result(timeout=10)`` with no way to configure it and
   no shared abstraction other call sites could reuse. ``await_future``
   below is that shared abstraction.
2. A plain synchronous callable (e.g. ``redis.Redis.set``) that may block on
   a socket read/write with no timeout of its own. ``run_with_timeout``
   bounds an arbitrary callable's wall-clock time by running it on a shared
   background thread pool and applying the same ``await_future`` wait to the
   resulting future.

Why a thread pool and not, say, ``signal.alarm`` or a socket-level timeout
configured on the redis client: ``signal``-based timeouts only work on the
main thread of the main interpreter (this codebase already runs a dedicated
non-main event-loop thread in ``RedisJobQueue``, so that approach is a
non-starter), and a per-client socket timeout would only cover redis-py's
own client, not any other future outbound call this layer is meant to wrap
generically (the milestone's fake-flaky-HTTP-client shape, and eventually
M8's LLM client / M11's GitHub client). A thread-pool-based deadline works
uniformly for any blocking callable.

Known, accepted limitation (documented rather than hidden): Python cannot
forcibly kill a running thread. If ``run_with_timeout`` times out, the
worker thread keeps running the original blocking call in the background
until it *eventually* returns or raises on its own -- the caller just stops
waiting for it. This is the same trade-off every thread-based timeout in
Python makes (there is no other portable way to bound an arbitrary
synchronous call). It is safe here because the wrapped calls are idempotent
or side-effect-tolerant at the call sites that use it (see
``backend/job_queue/redis_arq.py``), and because the circuit breaker layer
turns a persistent pattern of "these calls keep timing out" into fast
failures rather than an ever-growing pile of abandoned threads.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeoutError
from dataclasses import dataclass


class CallTimedOutError(Exception):
    """A wrapped call did not complete within its configured bound."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"call did not complete within {seconds}s")
        self.seconds = seconds


@dataclass(frozen=True)
class TimeoutPolicy:
    """How long a wrapped call is allowed to take before it is abandoned.

    A dataclass rather than a bare float so call sites read as
    ``TimeoutPolicy(seconds=5.0)`` instead of an unlabeled number, and so a
    future field (e.g. a per-attempt vs. total budget) can be added without
    changing every call site's signature.
    """

    seconds: float

    def __post_init__(self) -> None:
        if self.seconds <= 0:
            raise ValueError(f"timeout seconds must be positive, got {self.seconds}")


# One shared executor for every synchronous call this process bounds via
# run_with_timeout. Lazily constructed (most processes -- e.g. a pure unit
# test run of retry.py/circuit_breaker.py alone -- never touch it) and
# guarded by a lock since multiple JobQueue instances (or, later, multiple
# outbound clients) may call run_with_timeout concurrently from different
# threads.
_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(thread_name_prefix="reliability-timeout")
        return _executor


def await_future[T](future: Future[T], policy: TimeoutPolicy) -> T:
    """Block on an in-flight future for at most ``policy.seconds``.

    Raises ``CallTimedOutError`` (not the bare ``concurrent.futures.TimeoutError``)
    so callers can catch one project-specific exception type regardless of
    whether the underlying wait was a future they already held (this
    function) or a synchronous callable this module submitted on their
    behalf (``run_with_timeout``, which is built on top of this one).
    """
    try:
        return future.result(timeout=policy.seconds)
    except _FutureTimeoutError as exc:
        raise CallTimedOutError(policy.seconds) from exc


def run_with_timeout[T](func: Callable[[], T], *, policy: TimeoutPolicy) -> T:
    """Run ``func()`` on a background thread, bounded by ``policy``.

    Use this for a plain blocking callable (e.g. a synchronous redis-py
    call) that has no future of its own yet. If a future already exists
    (e.g. from ``asyncio.run_coroutine_threadsafe``), call ``await_future``
    directly instead -- submitting an already-scheduled coroutine's future
    to this function would make no sense, since there is nothing left to
    submit.

    Takes a zero-argument callable, not ``func(*args, **kwargs)`` — a
    caller with arguments to pass wraps them in a lambda (see
    ``backend/job_queue/redis_arq.py``'s ``_set_idempotency_key``). See
    ``call_with_retry``'s docstring in ``backend.reliability.retry`` for
    why: a ``ParamSpec``-typed pass-through cannot coexist with this
    function's own required ``policy`` keyword argument under PEP 612's
    typing rules.
    """
    future = _get_executor().submit(func)
    return await_future(future, policy)
