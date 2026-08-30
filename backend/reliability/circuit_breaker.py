"""Circuit breaker: closed / open / half-open, thread-safe.

Owns: turning "this dependency has been failing repeatedly" into "stop
calling it for a while and fail fast instead" -- the piece of the
reliability layer that a retry loop alone cannot provide. Retrying a call
that will not succeed (a fully unreachable Redis, in this project's only
real outbound I/O) just adds latency and connection-attempt load on top of
an outage; a circuit breaker gives every *caller after the first few* a
cheap, immediate failure instead of paying that cost again.

State machine (the standard three-state design):

    CLOSED --(>= failure_threshold consecutive failures)--> OPEN
    OPEN --(>= reset_timeout_seconds elapsed)--> HALF_OPEN
    HALF_OPEN --(next call succeeds)--> CLOSED
    HALF_OPEN --(next call fails)-----> OPEN (and the clock restarts)

CLOSED: calls go through normally; a success resets the failure count to
zero (so occasional, non-consecutive failures don't accumulate toward the
threshold -- only a *run* of failures should trip the breaker).
OPEN: calls are rejected immediately with ``CircuitOpenError`` -- no attempt
is made to reach the dependency at all. This is the "fail fast" behavior
the milestone's success criteria named explicitly.
HALF_OPEN: exactly one probe is allowed through by nature of the lock (see
below); its outcome decides whether the circuit fully recovers or reopens.

Thread safety: ``RedisJobQueue`` is called from every uvicorn worker thread
handling a webhook request, all sharing one breaker instance. Every state
read and every state mutation happens under a single ``threading.Lock``, so
two concurrent callers can never both observe CLOSED, both proceed, and
then race to mutate ``_failure_count``/``_state`` inconsistently (the classic
unsynchronized-mutable-state bug this milestone calls out by name). The
lock is held only for bookkeeping, never across the wrapped call itself --
``call()`` releases it before invoking ``func`` and reacquires it only to
record the outcome, so a slow dependency call cannot block every other
caller from even checking the breaker's state.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


class CircuitState(StrEnum):
    """The three states a breaker can be in. ``str`` mix-in for a clean /health JSON value."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised by ``CircuitBreaker.call`` when the breaker is open (or half-open and busy)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"circuit breaker {name!r} is open -- failing fast")
        self.name = name


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Tunable knobs for one breaker.

    Attributes:
        failure_threshold: Number of *consecutive* failures (in CLOSED
            state) required to trip the breaker to OPEN.
        reset_timeout_seconds: How long the breaker stays OPEN before
            allowing a single HALF_OPEN probe through.
    """

    failure_threshold: int
    reset_timeout_seconds: float

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {self.failure_threshold}")
        if self.reset_timeout_seconds <= 0:
            raise ValueError(
                f"reset_timeout_seconds must be positive, got {self.reset_timeout_seconds}"
            )


@dataclass(frozen=True)
class CircuitBreakerSnapshot:
    """A point-in-time, read-only view of a breaker's state, for inspection.

    Exists so a future ``/health`` endpoint (explicitly anticipated by this
    milestone) can report each registered breaker's status without reaching
    into its private, lock-protected fields directly.
    """

    name: str
    state: CircuitState
    consecutive_failures: int
    opened_at: float | None


class CircuitBreaker:
    """A single named circuit breaker guarding one outbound dependency."""

    def __init__(
        self,
        config: CircuitBreakerConfig,
        *,
        name: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._name = name
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        # HALF_OPEN allows exactly one probe at a time; this flag (held
        # under the same lock) prevents a second concurrent caller from
        # slipping through as a second "probe" while the first is still in
        # flight, which would defeat the point of a single controlled probe.
        self._probe_in_flight = False

    @property
    def name(self) -> str:
        return self._name

    def _maybe_expire_open_locked(self) -> None:
        """Transition OPEN -> HALF_OPEN once the reset window has elapsed.

        Must be called with ``self._lock`` already held. Pure state
        transition, no I/O -- checking "has enough wall-clock time passed"
        needs no probe call of its own.
        """
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and self._clock() - self._opened_at >= self._config.reset_timeout_seconds
        ):
            self._state = CircuitState.HALF_OPEN
            self._probe_in_flight = False

    def snapshot(self) -> CircuitBreakerSnapshot:
        """Read the current state (auto-expiring OPEN -> HALF_OPEN first if due)."""
        with self._lock:
            self._maybe_expire_open_locked()
            return CircuitBreakerSnapshot(
                name=self._name,
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                opened_at=self._opened_at,
            )

    @property
    def state(self) -> CircuitState:
        return self.snapshot().state

    def call(self, func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Run ``func`` through the breaker.

        Raises ``CircuitOpenError`` without calling ``func`` at all when the
        breaker is OPEN (or HALF_OPEN with a probe already in flight).
        Otherwise calls ``func`` and records the outcome.
        """
        with self._lock:
            self._maybe_expire_open_locked()
            if self._state is CircuitState.OPEN:
                raise CircuitOpenError(self._name)
            if self._state is CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    # Someone else's probe is already deciding this
                    # breaker's fate; fail fast rather than letting a
                    # second concurrent call also hit the still-maybe-down
                    # dependency.
                    raise CircuitOpenError(self._name)
                self._probe_in_flight = True

        # The dependency call itself happens with the lock released, so a
        # slow (or hanging, absent an outer timeout) call never blocks
        # other threads from reading/mutating breaker state.
        try:
            result = func(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        else:
            self._record_success()
            return result

    def _record_failure(self) -> None:
        with self._lock:
            was_half_open = self._state is CircuitState.HALF_OPEN
            self._consecutive_failures += 1
            if was_half_open or self._consecutive_failures >= self._config.failure_threshold:
                # A failed probe re-opens immediately regardless of the
                # threshold (one failure is enough evidence the dependency
                # is still down); in CLOSED, only a genuine run of
                # consecutive_failures >= threshold trips it.
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
            self._probe_in_flight = False

    def _record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._probe_in_flight = False


# Process-wide registry of every breaker constructed, so a future
# ``/health`` endpoint can enumerate all of them by name without each
# caller having to separately thread a reference through to it. Registering
# is a side effect of construction (see ``CircuitBreaker.__init__`` callers
# in ``backend/job_queue/redis_arq.py``), not something call sites do by
# hand, so a breaker can never exist without being discoverable.
_registry_lock = threading.Lock()
_registry: dict[str, CircuitBreaker] = {}


def register(breaker: CircuitBreaker) -> CircuitBreaker:
    """Add ``breaker`` to the process-wide registry and return it unchanged.

    Returns the breaker so it can be used inline:
    ``self._breaker = register(CircuitBreaker(config, name="redis"))``.
    Registering the same name twice (e.g. constructing a second
    ``RedisJobQueue`` in a test) replaces the previous entry -- the registry
    reflects "breakers that currently exist", not a permanent history.
    """
    with _registry_lock:
        _registry[breaker.name] = breaker
    return breaker


def all_breakers() -> dict[str, CircuitBreakerSnapshot]:
    """Snapshot every registered breaker, keyed by name -- what a ``/health`` route needs."""
    with _registry_lock:
        breakers = list(_registry.values())
    return {breaker.name: breaker.snapshot() for breaker in breakers}
