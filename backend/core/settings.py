"""Application configuration.

Owns: reading configuration from environment variables (and an optional local
``.env`` file for development) into a single typed, validated settings object.
Nothing else in the codebase should read ``os.environ`` directly for these
values — this module is the one source of truth, which is why it lives in
``backend.core``: per ADR-002 every other layer may depend on it, and it must
never import outward to any of them.

Why pydantic-settings: it gives us validation (a missing/blank secret fails
fast at startup instead of producing a validator that always rejects real
signatures) and a documented, typed surface instead of scattered
``os.environ.get(...)`` calls with silent string defaults.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# GitHub retries a failed webhook delivery for roughly 24 hours before
# giving up (per GitHub's documented redelivery window). The idempotency
# TTL must comfortably outlive that so a legitimate late retry is still
# recognized as a duplicate rather than being treated as new once the key
# has expired. One week gives a wide safety margin over 24h for clock
# skew, a slow/delayed retry queue on GitHub's side, or a delivery that
# arrives unusually late, while still bounding Redis memory growth (this
# is exactly the fix for the M2-deferred "InMemoryJobQueue grows
# unboundedly" finding: every key now expires, it just doesn't expire
# before it can plausibly still matter).
_DEFAULT_IDEMPOTENCY_TTL_SECONDS = 7 * 24 * 60 * 60

# M5's HITL gate (backend.hitl.queue.route_review) auto-posts a review whose
# overall_confidence is at/above this value (and has no CRITICAL finding);
# below it, the review is queued for human review. PLAN.md pins the default
# to 0.75 explicitly, so this is the one place that default is spelled out —
# route_review itself takes the threshold as a required argument rather than
# a second hardcoded default, so there is exactly one number to keep in sync.
_DEFAULT_HITL_CONFIDENCE_THRESHOLD = Decimal("0.750")

# M6 reliability-layer defaults. These back RedisJobQueue's retry/circuit
# breaker/timeout wrapping of its two real Redis calls (the idempotency SET
# and the cross-thread ARQ enqueue) and are deliberately conservative for a
# request-path dependency: few, fast retries (a webhook POST handler should
# not sit retrying for many seconds before answering GitHub) and a breaker
# that trips quickly but also recovers on a human-scale timeline.
_DEFAULT_RETRY_MAX_ATTEMPTS = 3
_DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.1
_DEFAULT_RETRY_MAX_DELAY_SECONDS = 2.0
# Replaces the M3-era hardcoded `future.result(timeout=10)` /
# `_ENQUEUE_TIMEOUT_SECONDS` / `_POOL_CREATE_TIMEOUT_SECONDS` magic numbers
# in backend/job_queue/redis_arq.py with one configurable value.
_DEFAULT_RELIABILITY_TIMEOUT_SECONDS = 10.0
_DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
_DEFAULT_CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS = 30.0


class Settings(BaseSettings):
    """Runtime configuration for the pr-review-agent backend.

    Attributes:
        github_webhook_secret: The shared secret GitHub signs webhook request
            bodies with (HMAC-SHA256). Required, never hardcoded, and never
            given a default value here — a default would mean a misconfigured
            deployment silently accepts a well-known secret instead of
            failing to start. See ``.env.example`` for the documented
            environment variable name and local-dev instructions.
        job_queue_backend: Which ``JobQueue`` implementation
            ``backend.api.main.create_app`` wires up by default when the
            caller doesn't pass one in explicitly. ``"in_memory"`` (the
            default) keeps local unit/webhook tests fast and
            Redis-independent; ``"redis"`` is what the M3 docker-compose
            demo and real deployments use.
        redis_url: Connection string for the Redis instance backing
            ``RedisJobQueue`` and the ARQ worker. Defaults to the port this
            project's ``docker-compose.yml`` publishes locally (6380, not
            Redis's usual 6379 — see that file's comment for why).
        idempotency_ttl_seconds: How long a ``X-GitHub-Delivery`` id is
            remembered in Redis before it expires and its key is
            reclaimed. See module-level comment above for why the default
            is one week.
        hitl_confidence_threshold: The HITL confidence gate's cutoff (M5).
            A review with ``overall_confidence`` at or above this value,
            and no CRITICAL finding, auto-posts; otherwise it is queued for
            human review. See ``backend.hitl.queue.route_review``. Default
            0.750, per PLAN.md.
        retry_max_attempts: M6 reliability layer. Total attempts (including
            the first) ``RedisJobQueue`` makes per Redis call before giving
            up. See ``backend.reliability.retry.RetryPolicy``.
        retry_base_delay_seconds: M6 reliability layer. Delay before the
            second attempt; grows exponentially (capped by
            ``retry_max_delay_seconds``) after that.
        retry_max_delay_seconds: M6 reliability layer. Upper bound the
            exponential backoff delay is clamped to.
        reliability_timeout_seconds: M6 reliability layer. Bound (seconds)
            on each individual Redis call ``RedisJobQueue`` makes — both the
            synchronous idempotency ``SET`` and the cross-thread ARQ
            enqueue future. Replaces the previous hardcoded
            ``future.result(timeout=10)``.
        circuit_breaker_failure_threshold: M6 reliability layer. Consecutive
            failures required to trip ``RedisJobQueue``'s breaker to OPEN.
        circuit_breaker_reset_timeout_seconds: M6 reliability layer. How
            long the breaker stays OPEN before allowing a single HALF_OPEN
            probe through.
    """

    github_webhook_secret: str = Field(
        min_length=1,
        description=(
            "Shared secret used to verify GitHub webhook HMAC-SHA256 "
            "signatures. Set via the GITHUB_WEBHOOK_SECRET environment "
            "variable or a local .env file; never commit a real value."
        ),
    )

    job_queue_backend: Literal["in_memory", "redis"] = Field(
        default="in_memory",
        description=(
            "Which JobQueue implementation create_app() wires up by "
            "default. 'in_memory' for tests/local dev without Docker, "
            "'redis' to use the real Redis/ARQ queue."
        ),
    )

    redis_url: str = Field(
        default="redis://localhost:6380/0",
        description=(
            "Redis connection string for RedisJobQueue and the ARQ worker. "
            "Defaults to the host port this project's docker-compose.yml "
            "publishes (6380)."
        ),
    )

    idempotency_ttl_seconds: int = Field(
        default=_DEFAULT_IDEMPOTENCY_TTL_SECONDS,
        gt=0,
        description=(
            "Seconds a delivery-id idempotency key survives in Redis "
            "before expiring. Must comfortably outlive GitHub's ~24h "
            "webhook redelivery window. Default is one week."
        ),
    )

    hitl_confidence_threshold: Decimal = Field(
        default=_DEFAULT_HITL_CONFIDENCE_THRESHOLD,
        ge=Decimal("0.000"),
        le=Decimal("1.000"),
        decimal_places=3,
        description=(
            "HITL gate cutoff: a review with overall_confidence >= this "
            "value and no CRITICAL finding auto-posts; otherwise it is "
            "queued for human review. Default 0.750."
        ),
    )

    retry_max_attempts: int = Field(
        default=_DEFAULT_RETRY_MAX_ATTEMPTS,
        ge=1,
        description=(
            "Total attempts (including the first) RedisJobQueue makes per "
            "Redis call before giving up. Default 3."
        ),
    )

    retry_base_delay_seconds: float = Field(
        default=_DEFAULT_RETRY_BASE_DELAY_SECONDS,
        gt=0,
        description=(
            "Delay (seconds) before the second retry attempt; grows "
            "exponentially after that, capped at "
            "RETRY_MAX_DELAY_SECONDS. Default 0.1."
        ),
    )

    retry_max_delay_seconds: float = Field(
        default=_DEFAULT_RETRY_MAX_DELAY_SECONDS,
        gt=0,
        description="Upper bound (seconds) the exponential backoff delay is clamped to. Default 2.0.",
    )

    reliability_timeout_seconds: float = Field(
        default=_DEFAULT_RELIABILITY_TIMEOUT_SECONDS,
        gt=0,
        description=(
            "Bound (seconds) on each individual outbound Redis call "
            "RedisJobQueue makes. Replaces the previous hardcoded "
            "future.result(timeout=10). Default 10.0."
        ),
    )

    circuit_breaker_failure_threshold: int = Field(
        default=_DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        ge=1,
        description=(
            "Consecutive failures required to trip RedisJobQueue's "
            "circuit breaker to OPEN. Default 5."
        ),
    )

    circuit_breaker_reset_timeout_seconds: float = Field(
        default=_DEFAULT_CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS,
        gt=0,
        description=(
            "How long (seconds) RedisJobQueue's circuit breaker stays "
            "OPEN before allowing a single HALF_OPEN probe through. "
            "Default 30.0."
        ),
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings singleton, built once and cached.

    Cached (rather than re-read per call) because environment configuration
    does not change within a process lifetime, and re-parsing on every
    request would be pure overhead in the webhook request path.
    """
    # mypy sees `github_webhook_secret` as a required constructor argument
    # because it has no default; at runtime pydantic-settings populates it
    # from the environment / .env file instead, which the type checker has
    # no way to see. This is the standard, documented false positive for
    # pydantic-settings with required fields.
    return Settings()  # type: ignore[call-arg]
