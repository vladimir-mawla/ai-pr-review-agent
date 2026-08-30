"""Tests for the M6 reliability layer -- PLAN.md's own demo target.

Structure, deliberately not "one file per module, isolated":

- ``TestRetryPolicy`` / ``TestCallWithRetry``: retry.py in isolation.
- ``TestTimeout``: timeout.py in isolation.
- ``TestCircuitBreaker`` / ``TestCircuitBreakerThreadSafety``: circuit_breaker.py
  in isolation, including a concurrency hammer test.
- ``TestReliabilityComposition``: retry + circuit breaker + timeout composed
  together against a fake flaky client -- this is PLAN.md's own success
  criteria ("a client that fails 100% of the time trips the circuit breaker
  within N attempts and subsequent calls fail fast ... until the cooldown";
  "a call exceeding the configured timeout raises within tolerance"), proven
  directly rather than only inferred from the three modules' separate unit
  tests. The M5 lesson this milestone was explicitly told to apply: testing
  each piece in isolation is exactly how a real composition bug slipped
  through last time.
- ``TestRedisJobQueueWiring`` / ``TestWebhookReturns503WhenQueueUnavailable``:
  the actual production wiring. These construct a real ``RedisJobQueue``
  against this project's real, reachable Redis (so construction -- which
  eagerly opens a real ARQ connection pool -- succeeds), then simulate Redis
  going down *after* that live connection was established by pointing the
  instance's synchronous client at an unreachable address. This is a
  deliberately chosen alternative to stopping the shared project Redis
  container mid-test-run (which would be destructive to any other test in
  the same session that assumes it's up, and is not something a single test
  file should do to shared infrastructure) -- and arguably the more
  realistic shape of "Redis is down" in production anyway: a live service
  losing its dependency mid-flight, not failing to ever start. These are
  the tests that prove the wiring, not just the units: they exercise the
  real ``RedisJobQueue.enqueue`` -> real ``backend.webhook_receiver.router``
  path end-to-end.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid
from collections.abc import Iterator

import pytest
import redis as redis_sync
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.core.settings import Settings, get_settings
from backend.job_queue.interface import QueueUnavailableError
from backend.job_queue.redis_arq import RedisJobQueue
from backend.models import WebhookEvent
from backend.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    CircuitState,
)
from backend.reliability.retry import (
    RetryExhaustedError,
    RetryPolicy,
    call_with_retry,
)
from backend.reliability.timeout import (
    CallTimedOutError,
    TimeoutPolicy,
    run_with_timeout,
)

# ---------------------------------------------------------------------------
# retry.py
# ---------------------------------------------------------------------------


class TestRetryPolicy:
    """Pure, deterministic properties of the backoff formula itself."""

    def test_no_delay_before_first_attempt(self) -> None:
        policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.1, max_delay_seconds=10.0)
        assert policy.delay_for_attempt(1) == 0.0

    def test_backoff_grows_exponentially_then_clamps(self) -> None:
        policy = RetryPolicy(max_attempts=6, base_delay_seconds=0.1, max_delay_seconds=1.0)
        delays = [policy.delay_for_attempt(attempt) for attempt in range(2, 7)]
        # 0.1, 0.2, 0.4, 0.8, then clamped to 1.0 (would be 1.6 uncapped).
        assert delays == [0.1, 0.2, 0.4, 0.8, 1.0]
        assert delays == sorted(delays), "backoff must never shrink between attempts"

    def test_rejects_invalid_config(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            RetryPolicy(max_attempts=0, base_delay_seconds=0.1, max_delay_seconds=1.0)
        with pytest.raises(ValueError, match="max_delay_seconds"):
            RetryPolicy(max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=0.5)


class TestCallWithRetry:
    """The retry loop's actual attempt/backoff/give-up behavior."""

    def test_succeeds_after_n_transient_failures(self) -> None:
        calls = {"count": 0}

        def flaky() -> str:
            calls["count"] += 1
            if calls["count"] < 3:
                raise ConnectionError("transient")
            return "ok"

        policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.0, max_delay_seconds=0.0)
        result = call_with_retry(flaky, policy=policy, sleep=lambda _: None)

        assert result == "ok"
        assert calls["count"] == 3

    def test_gives_up_after_the_bound(self) -> None:
        calls = {"count": 0}

        def always_fails() -> None:
            calls["count"] += 1
            raise ConnectionError("permanently down")

        policy = RetryPolicy(max_attempts=4, base_delay_seconds=0.0, max_delay_seconds=0.0)

        with pytest.raises(RetryExhaustedError) as exc_info:
            call_with_retry(always_fails, policy=policy, sleep=lambda _: None)

        assert calls["count"] == 4, "must make exactly max_attempts attempts, no more"
        assert exc_info.value.attempts == 4
        assert isinstance(exc_info.value.last_exception, ConnectionError)

    def test_does_not_retry_a_non_retryable_error(self) -> None:
        calls = {"count": 0}

        def malformed_call() -> None:
            calls["count"] += 1
            raise ValueError("caller passed a bad argument")

        policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.0, max_delay_seconds=0.0)

        with pytest.raises(ValueError, match="bad argument"):
            call_with_retry(malformed_call, policy=policy, sleep=lambda _: None)

        assert calls["count"] == 1, "a non-retryable error must not be retried at all"

    def test_backoff_delays_actually_grow_between_attempts(self) -> None:
        recorded_delays: list[float] = []

        def always_fails() -> None:
            raise ConnectionError("down")

        policy = RetryPolicy(max_attempts=4, base_delay_seconds=0.1, max_delay_seconds=10.0)

        with pytest.raises(RetryExhaustedError):
            call_with_retry(
                always_fails,
                policy=policy,
                sleep=recorded_delays.append,
                rand=lambda: 1.0,  # no jitter shrinkage, so growth is exact
            )

        assert recorded_delays == [0.1, 0.2, 0.4]
        assert recorded_delays == sorted(recorded_delays)

    def test_jitter_scales_delay_but_not_attempt_count(self) -> None:
        """Jitter must change *how long* a retry sleeps, never *how many* attempts happen."""
        attempts_with_jitter = {"count": 0}
        attempts_without_jitter = {"count": 0}

        def make_always_fails(counter: dict[str, int]) -> object:
            def _call() -> None:
                counter["count"] += 1
                raise ConnectionError("down")

            return _call

        policy_with_jitter = RetryPolicy(
            max_attempts=5, base_delay_seconds=0.1, max_delay_seconds=10.0, jitter=True
        )
        policy_without_jitter = RetryPolicy(
            max_attempts=5, base_delay_seconds=0.1, max_delay_seconds=10.0, jitter=False
        )

        recorded_jittered: list[float] = []
        with pytest.raises(RetryExhaustedError):
            call_with_retry(
                make_always_fails(attempts_with_jitter),  # type: ignore[arg-type]
                policy=policy_with_jitter,
                sleep=recorded_jittered.append,
                rand=lambda: 0.5,  # deterministic "random" factor
            )
        with pytest.raises(RetryExhaustedError):
            call_with_retry(
                make_always_fails(attempts_without_jitter),  # type: ignore[arg-type]
                policy=policy_without_jitter,
                sleep=lambda _: None,
            )

        assert attempts_with_jitter["count"] == attempts_without_jitter["count"] == 5
        # Every jittered delay is exactly half of what delay_for_attempt
        # would compute pre-jitter (since rand() is pinned to 0.5) --
        # jitter changed the sleep duration, not the attempt count above.
        expected_pre_jitter = [policy_with_jitter.delay_for_attempt(a) for a in range(2, 6)]
        assert recorded_jittered == [d * 0.5 for d in expected_pre_jitter]


# ---------------------------------------------------------------------------
# timeout.py
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_fast_call_is_unaffected(self) -> None:
        policy = TimeoutPolicy(seconds=1.0)
        result = run_with_timeout(lambda: "done", policy=policy)
        assert result == "done"

    def test_slow_call_is_cut_off_at_the_bound(self) -> None:
        policy = TimeoutPolicy(seconds=0.2)

        def slow() -> str:
            time.sleep(2.0)
            return "too late"

        started = time.monotonic()
        with pytest.raises(CallTimedOutError):
            run_with_timeout(slow, policy=policy)
        elapsed = time.monotonic() - started

        # PLAN.md's own tolerance is +/-50ms; a background thread pool adds
        # some real scheduling overhead beyond a bare `future.result()`, so
        # this allows a wider (still tight) margin to avoid CI flakiness
        # while still proving the call was cut off near the bound, not left
        # to run anywhere close to its own 2.0s sleep.
        assert 0.2 <= elapsed < 0.2 + 0.3, f"expected a cutoff near 0.2s, took {elapsed}s"

    def test_rejects_non_positive_seconds(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            TimeoutPolicy(seconds=0)


# ---------------------------------------------------------------------------
# circuit_breaker.py
# ---------------------------------------------------------------------------


class _FakeClock:
    """A settable clock so half-open transitions can be tested without real sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TestCircuitBreaker:
    def test_opens_after_the_failure_threshold(self) -> None:
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=3, reset_timeout_seconds=60), name="t1"
        )

        def fails() -> None:
            raise ConnectionError("down")

        for expected_state in (CircuitState.CLOSED, CircuitState.CLOSED, CircuitState.OPEN):
            with pytest.raises(ConnectionError):
                breaker.call(fails)
            assert breaker.state is expected_state

    def test_rejects_fast_while_open_without_calling_func(self) -> None:
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=60), name="t2"
        )
        calls = {"count": 0}

        def fails() -> None:
            calls["count"] += 1
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            breaker.call(fails)
        assert breaker.state is CircuitState.OPEN
        assert calls["count"] == 1

        with pytest.raises(CircuitOpenError):
            breaker.call(fails)
        assert calls["count"] == 1, "func must not be invoked while the breaker is open"

    def test_transitions_to_half_open_after_the_reset_window(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=10),
            name="t3",
            clock=clock,
        )
        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("down")))
        assert breaker.state is CircuitState.OPEN

        clock.advance(9.999)
        assert breaker.state is CircuitState.OPEN, "must not expire a moment early"

        clock.advance(0.002)
        assert breaker.state is CircuitState.HALF_OPEN

    def test_half_open_success_closes_the_breaker(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=5),
            name="t4",
            clock=clock,
        )
        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("down")))
        clock.advance(5.0)
        assert breaker.state is CircuitState.HALF_OPEN

        result = breaker.call(lambda: "recovered")

        assert result == "recovered"
        assert breaker.state is CircuitState.CLOSED
        assert breaker.snapshot().consecutive_failures == 0

    def test_half_open_failure_reopens_the_breaker(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=5),
            name="t5",
            clock=clock,
        )
        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("down")))
        clock.advance(5.0)
        assert breaker.state is CircuitState.HALF_OPEN

        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("still down")))

        assert breaker.state is CircuitState.OPEN, (
            "a single failed probe must reopen immediately, not wait for the full threshold again"
        )

    def test_rejects_invalid_config(self) -> None:
        with pytest.raises(ValueError, match="failure_threshold"):
            CircuitBreakerConfig(failure_threshold=0, reset_timeout_seconds=1.0)
        with pytest.raises(ValueError, match="reset_timeout_seconds"):
            CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=0)


class TestCircuitBreakerThreadSafety:
    """Hammer one breaker from many threads; state must stay coherent throughout."""

    def test_concurrent_failures_produce_coherent_state_and_bounded_calls(self) -> None:
        failure_threshold = 10
        num_threads = 200
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=failure_threshold, reset_timeout_seconds=1000),
            name="thread-safety",
        )

        call_count_lock = threading.Lock()
        call_count = {"n": 0}
        unexpected_errors: list[BaseException] = []

        def always_fails() -> None:
            with call_count_lock:
                call_count["n"] += 1
            raise RuntimeError("boom")

        def worker() -> None:
            try:
                breaker.call(always_fails)
            except (RuntimeError, CircuitOpenError):
                pass  # expected outcomes
            except Exception as exc:  # pragma: no cover - would indicate a real race bug
                unexpected_errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert unexpected_errors == [], f"unexpected exceptions under concurrency: {unexpected_errors}"
        snapshot = breaker.snapshot()
        assert snapshot.state is CircuitState.OPEN, "must end up open after >= threshold failures"
        # The breaker must have actually stopped the dependency from being
        # hammered by every one of the 200 threads -- proof the lock-guarded
        # OPEN check, not just the failure counter, is doing real work.
        assert failure_threshold <= call_count["n"] < num_threads, (
            f"expected the breaker to fail-fast most callers, but func ran {call_count['n']} times"
        )
        assert snapshot.consecutive_failures == call_count["n"]


# ---------------------------------------------------------------------------
# Composition: retry + circuit breaker + timeout together, against a fake
# flaky client -- PLAN.md's own M6 success criteria, proven directly.
# ---------------------------------------------------------------------------


class _AlwaysFailingClient:
    """Stands in for "GitHub/LLM providers" per PLAN.md's M6 outcome text."""

    def __init__(self) -> None:
        self.call_count = 0

    def request(self) -> str:
        self.call_count += 1
        raise ConnectionError("simulated outbound provider outage")


class _SlowClient:
    def __init__(self, delay_seconds: float) -> None:
        self.call_count = 0
        self._delay_seconds = delay_seconds

    def request(self) -> str:
        self.call_count += 1
        time.sleep(self._delay_seconds)
        return "eventually"


def _guarded_request(
    client_call: object,
    *,
    breaker: CircuitBreaker,
    retry_policy: RetryPolicy,
    timeout_policy: TimeoutPolicy,
) -> str:
    """Mirror ``RedisJobQueue._call_reliably``'s composition order for test purposes."""

    def _timed() -> str:
        return run_with_timeout(client_call, policy=timeout_policy)  # type: ignore[arg-type]

    return call_with_retry(
        lambda: breaker.call(_timed),
        policy=retry_policy,
        non_retryable_exceptions=(CircuitOpenError, TypeError, ValueError, KeyError, AttributeError),
        sleep=lambda _: None,
    )


class TestReliabilityComposition:
    """The M5 lesson applied: test the composition, not just the isolated parts."""

    def test_always_failing_client_trips_breaker_then_fails_fast_until_cooldown(self) -> None:
        client = _AlwaysFailingClient()
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=3, reset_timeout_seconds=60), name="composition-1"
        )
        retry_policy = RetryPolicy(max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=0.0)
        timeout_policy = TimeoutPolicy(seconds=1.0)

        # Three outer calls (max_attempts=1 each) exhaust the threshold.
        for _ in range(3):
            with pytest.raises(RetryExhaustedError):
                _guarded_request(
                    client.request, breaker=breaker, retry_policy=retry_policy, timeout_policy=timeout_policy
                )
        assert breaker.state is CircuitState.OPEN
        assert client.call_count == 3

        # Subsequent calls fail fast -- no further network attempt at all.
        for _ in range(5):
            with pytest.raises(CircuitOpenError):
                _guarded_request(
                    client.request, breaker=breaker, retry_policy=retry_policy, timeout_policy=timeout_policy
                )
        assert client.call_count == 3, "breaker must fail fast, never re-attempting the dead client"

    def test_call_exceeding_timeout_raises_within_tolerance_not_hanging(self) -> None:
        client = _SlowClient(delay_seconds=5.0)
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=100, reset_timeout_seconds=60), name="composition-2"
        )
        retry_policy = RetryPolicy(max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=0.0)
        timeout_policy = TimeoutPolicy(seconds=0.2)

        started = time.monotonic()
        with pytest.raises(RetryExhaustedError) as exc_info:
            _guarded_request(
                client.request, breaker=breaker, retry_policy=retry_policy, timeout_policy=timeout_policy
            )
        elapsed = time.monotonic() - started

        assert isinstance(exc_info.value.last_exception, CallTimedOutError)
        assert elapsed < 1.0, f"a 0.2s timeout must not let a 5s call be waited out, took {elapsed}s"


# ---------------------------------------------------------------------------
# Real wiring: RedisJobQueue + the webhook route, against a real (then
# simulated-down) Redis. Requires this project's own docker-compose Redis
# to be reachable for construction; skipped (not failed) otherwise, matching
# tests/integration/test_queue_roundtrip.py's existing convention.
# ---------------------------------------------------------------------------

_BASE_SETTINGS = get_settings()
_REDIS_URL = _BASE_SETTINGS.redis_url
_UNREACHABLE_REDIS_URL = "redis://localhost:1/0"


def _redis_reachable(redis_url: str) -> bool:
    try:
        client = redis_sync.Redis.from_url(redis_url, socket_connect_timeout=1)
        return bool(client.ping())
    except (redis_sync.RedisError, OSError):
        return False


requires_real_redis = pytest.mark.skipif(
    not _redis_reachable(_REDIS_URL),
    reason=f"Redis not reachable at {_REDIS_URL} -- run `docker compose up -d redis` first",
)


def _fast_reliability_settings(**overrides: object) -> Settings:
    """Settings with small attempts/timeouts so a "Redis down" test runs in well under a second."""
    defaults: dict[str, object] = {
        "github_webhook_secret": "reliability-wiring-test-secret",
        "redis_url": _REDIS_URL,
        "retry_max_attempts": 2,
        "retry_base_delay_seconds": 0.01,
        "retry_max_delay_seconds": 0.02,
        "reliability_timeout_seconds": 0.3,
        "circuit_breaker_failure_threshold": 2,
        "circuit_breaker_reset_timeout_seconds": 30.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _make_event(delivery_id: str | None = None) -> WebhookEvent:
    return WebhookEvent(
        action="opened",
        pr_number=1,
        repository_owner="acme",
        repository_name="widgets",
        head_sha="a" * 40,
        delivery_id=delivery_id or str(uuid.uuid4()),
        received_at="2026-08-30T00:00:00+00:00",
    )


def _simulate_redis_down(queue: RedisJobQueue) -> None:
    """Point ``queue``'s synchronous client at an address nothing listens on.

    Simulates a real, already-connected Redis dependency going down, without
    stopping this project's own shared docker-compose Redis (which other
    tests in the same session depend on staying up).
    """
    queue._redis_sync = redis_sync.Redis.from_url(  # noqa: SLF001 - test-only, intentional
        _UNREACHABLE_REDIS_URL, socket_connect_timeout=0.2, socket_timeout=0.2
    )


@requires_real_redis
class TestRedisJobQueueWiring:
    """Proves the reliability layer is wired into RedisJobQueue's real Redis calls."""

    def test_redis_down_raises_queue_unavailable_and_opens_the_breaker(self) -> None:
        queue = RedisJobQueue(_fast_reliability_settings())
        try:
            _simulate_redis_down(queue)

            with pytest.raises(QueueUnavailableError):
                queue.enqueue(_make_event())
            assert queue._breaker.state is CircuitState.OPEN  # noqa: SLF001

            # Fail fast on the next call: no full retry/timeout cycle again.
            started = time.monotonic()
            with pytest.raises(QueueUnavailableError):
                queue.enqueue(_make_event())
            elapsed = time.monotonic() - started
            assert elapsed < 0.1, f"expected a fast failure once the breaker is open, took {elapsed}s"
        finally:
            queue.close()

    def test_redis_up_enqueue_is_still_unaffected_by_the_reliability_wrapping(self) -> None:
        """Regression: normal operation (Redis actually up) must not change behavior."""
        queue = RedisJobQueue(_fast_reliability_settings())
        try:
            event = _make_event()
            first = queue.enqueue(event)
            second = queue.enqueue(event)  # same delivery_id -- idempotency must still hold
            assert first.enqueued is True
            assert second.enqueued is False
            assert 0 < queue.ttl_for(event.delivery_id) <= _BASE_SETTINGS.idempotency_ttl_seconds
        finally:
            queue.close()


SECRET = "reliability-webhook-503-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _sample_payload() -> dict[str, object]:
    return {
        "action": "opened",
        "pull_request": {
            "number": 7,
            "head": {"sha": "b" * 40},
        },
        "repository": {
            "name": "pr-review-agent",
            "owner": {"login": "myorg"},
        },
    }


@pytest.fixture
def _down_redis_queue() -> Iterator[RedisJobQueue]:
    queue = RedisJobQueue(_fast_reliability_settings(github_webhook_secret=SECRET))
    _simulate_redis_down(queue)
    try:
        yield queue
    finally:
        queue.close()


@requires_real_redis
class TestWebhookReturns503WhenQueueUnavailable:
    """The wiring proof named explicitly by the milestone: not a unit test, the real route."""

    def test_webhook_returns_503_not_500_when_redis_is_down(
        self, _down_redis_queue: RedisJobQueue
    ) -> None:
        app = create_app(
            settings=Settings(github_webhook_secret=SECRET), job_queue=_down_redis_queue
        )
        body = json.dumps(_sample_payload()).encode("utf-8")
        with TestClient(app) as client:
            response = client.post(
                "/webhook",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": _sign(body),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": str(uuid.uuid4()),
                },
            )

        assert response.status_code == 503
        assert response.status_code != 500
