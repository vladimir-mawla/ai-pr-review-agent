"""reliability module.

Fault tolerance primitives for every outbound call this system makes:

- ``retry``: bounded exponential backoff with jitter, distinguishing
  transient failures (retry) from programmer-error failures (never retry).
- ``circuit_breaker``: closed/open/half-open state machine, thread-safe,
  inspectable (for a future ``/health`` endpoint).
- ``timeout``: a bounded-wait wrapper for both in-flight futures and plain
  synchronous callables.

M6 built these three (``idempotency`` is not part of M6's scope -- the
project's real idempotency mechanism, an atomic Redis ``SET NX EX``, already
exists in ``backend.job_queue.redis_arq`` and is a queue-layer concern, not
a generic reliability primitive these modules would own).

These are not unit-tested-and-forgotten: ``backend.job_queue.redis_arq.RedisJobQueue``
composes all three around its real Redis calls (the idempotency SET and the
cross-thread ARQ enqueue) -- see that module for the live call sites, and
``tests/unit/test_reliability.py`` for the composition proof, not just
isolated per-module tests.

Per ADR-002, this module follows the inward-only dependency rule: dependencies point
toward backend.core and backend.models, never outward toward api or orchestrator.
"""

from backend.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerSnapshot,
    CircuitOpenError,
    CircuitState,
    all_breakers,
    register,
)
from backend.reliability.retry import (
    DEFAULT_NON_RETRYABLE_EXCEPTIONS,
    RetryExhaustedError,
    RetryPolicy,
    call_with_retry,
)
from backend.reliability.timeout import (
    CallTimedOutError,
    TimeoutPolicy,
    await_future,
    run_with_timeout,
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerSnapshot",
    "CircuitOpenError",
    "CircuitState",
    "all_breakers",
    "register",
    "DEFAULT_NON_RETRYABLE_EXCEPTIONS",
    "RetryExhaustedError",
    "RetryPolicy",
    "call_with_retry",
    "CallTimedOutError",
    "TimeoutPolicy",
    "await_future",
    "run_with_timeout",
]
