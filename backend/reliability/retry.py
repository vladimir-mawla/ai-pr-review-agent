"""Retry with exponential backoff and jitter -- the retry leg of the reliability layer.

Owns: turning a single flaky call into a bounded sequence of attempts, with
a delay between attempts that grows exponentially (so a struggling
dependency gets increasing breathing room, not a hammering at a fixed
interval) and is randomized within that growing bound (so a fleet of
callers that all failed at the same instant do not all wake up and retry in
lockstep, synchronizing a second thundering herd against a dependency that
is trying to recover).

The one design point this milestone calls out explicitly: retrying is only
correct for *transient* failures. A ``TypeError``/``ValueError`` raised
because a caller passed the wrong shape of argument will never succeed no
matter how many times it is retried -- retrying it anyway is not resilience,
it is a bug that burns the whole retry budget (and, composed with a circuit
breaker, needlessly trips it) on a call that was never going to work.
``call_with_retry`` therefore takes an explicit ``non_retryable_exceptions``
tuple and re-raises immediately (no sleep, no further attempts) for any
exception that is an instance of one of them. The default covers the
classic "this is a programmer error, not a flaky dependency" set; call
sites (see ``backend/job_queue/redis_arq.py``) extend it with their own
non-retryable types, e.g. ``CircuitOpenError`` -- once the breaker is open,
retrying immediately would defeat the entire point of failing fast.

``call_with_retry`` takes a zero-argument callable (``Callable[[], T]``),
not ``func(*args, **kwargs)`` — a caller with arguments to pass wraps them
in a lambda/``functools.partial`` (every call site in this codebase already
does, since they're composing this with the circuit breaker and timeout
layers around it anyway). This is a deliberate simplification, not a
missing feature: mixing a ``ParamSpec``-typed ``*args``/``**kwargs`` pass-
through with this function's *own* required keyword arguments
(``policy``, ``non_retryable_exceptions``, ...) is not expressible under
PEP 612's typing rules (no additional named parameter may follow
``*args: P.args`` besides ``**kwargs: P.kwargs``) -- so accepting arbitrary
``func`` arguments here would mean giving up strict typing on either this
function's own options or the wrapped call's signature. A thin wrapper
closure costs nothing at the call site and keeps both fully typed.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

# Exceptions that indicate a bug in the call itself, not a transient failure
# of whatever it was calling. Retrying these can never help -- the
# arguments/state that caused them do not change between attempts.
DEFAULT_NON_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
)


@dataclass(frozen=True)
class RetryPolicy:
    """Tunable knobs for one retry loop.

    Attributes:
        max_attempts: Total number of attempts, including the first (a
            value of 3 means "try, and if it fails, try up to 2 more
            times" -- never unbounded).
        base_delay_seconds: Delay before the *second* attempt. Attempt N's
            delay (before N > 1) is ``base_delay_seconds * 2 ** (N - 2)``,
            capped at ``max_delay_seconds``.
        max_delay_seconds: Upper bound the exponential growth is clamped to,
            so a large ``max_attempts`` cannot produce an hours-long wait
            between two attempts.
        jitter: When true (the default), the actual sleep for an attempt is
            drawn uniformly from ``[0, computed_delay]`` rather than being
            exactly ``computed_delay`` -- "full jitter", the standard
            mitigation for synchronized retry storms. Attempt *counting* is
            unaffected either way: jitter only changes how long a retry
            loop sleeps between attempts, never how many attempts it makes,
            so tests can assert a deterministic attempt count regardless of
            this flag.
    """

    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.base_delay_seconds < 0:
            raise ValueError(f"base_delay_seconds must be >= 0, got {self.base_delay_seconds}")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")

    def delay_for_attempt(self, attempt: int) -> float:
        """Computed (pre-jitter) delay before making ``attempt`` (1-indexed).

        ``attempt`` is the attempt *about to be made* (2, 3, ...) -- there is
        no delay before attempt 1. Exposed as its own method so the
        "backoff actually grows" property can be tested directly, without
        needing to also account for jitter's randomness in the same
        assertion.
        """
        if attempt <= 1:
            return 0.0
        exponential = self.base_delay_seconds * float(2 ** (attempt - 2))
        return min(exponential, self.max_delay_seconds)


class RetryExhaustedError(Exception):
    """Every attempt permitted by the policy failed; wraps the last failure."""

    def __init__(self, attempts: int, last_exception: BaseException) -> None:
        super().__init__(
            f"gave up after {attempts} attempt(s); last error: "
            f"{type(last_exception).__name__}: {last_exception}"
        )
        self.attempts = attempts
        self.last_exception = last_exception


def call_with_retry[T](
    func: Callable[[], T],
    *,
    policy: RetryPolicy,
    non_retryable_exceptions: tuple[type[BaseException], ...] = DEFAULT_NON_RETRYABLE_EXCEPTIONS,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Call ``func()``, retrying transient failures per ``policy``.

    A non-retryable exception (an instance of anything in
    ``non_retryable_exceptions``) propagates immediately on the attempt it
    occurs, with no further attempts and no sleep. Any other exception is
    retried until ``policy.max_attempts`` is reached, at which point a
    ``RetryExhaustedError`` wrapping the last exception is raised (the
    original exception is still reachable as ``.last_exception`` /
    ``__cause__``, so no diagnostic information is lost).

    ``sleep`` and ``rand`` are injectable purely so tests can assert on
    backoff growth and jitter bounds without a real test run taking
    seconds -- production call sites never pass them.
    """
    last_exception: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        if attempt > 1:
            delay = policy.delay_for_attempt(attempt)
            if policy.jitter:
                delay *= rand()
            if delay > 0:
                sleep(delay)
        try:
            return func()
        except non_retryable_exceptions:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately broad: see module docstring
            last_exception = exc
            if attempt >= policy.max_attempts:
                raise RetryExhaustedError(attempt, exc) from exc
    # Unreachable: the loop above either returns, raises RetryExhaustedError
    # on the final attempt, or re-raises a non-retryable exception -- there
    # is no path where the loop simply ends. This satisfies mypy --strict's
    # requirement that every code path return T or raise, and documents the
    # invariant for a human reader instead of leaving a bare `assert False`.
    raise AssertionError("unreachable") from last_exception
