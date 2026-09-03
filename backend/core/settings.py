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

from psycopg.conninfo import make_conninfo
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

# M7 events spine. Host port 5433 (not Postgres's usual 5432) for the same
# reason Redis uses 6380 instead of 6379 -- see docker-compose.yml's
# postgres service comment. Two separate connection strings, two separate
# roles (both created by backend/database/migrations/0001_agent_events.sql):
# `database_url` is the restricted `agent_events_writer` role (SELECT+INSERT
# only, UPDATE/DELETE explicitly revoked) that application code actually
# writes through; `database_admin_url` is the "postgres" superuser, used
# ONLY to apply migrations (backend.database.postgres.apply_migrations),
# never by any request-path code.
_DEFAULT_DATABASE_URL = (
    "postgresql://agent_events_writer:agent_events_writer@localhost:5433/pr_review_agent"
)
_DEFAULT_DATABASE_ADMIN_URL = "postgresql://postgres:postgres@localhost:5433/pr_review_agent"

# M7 L2 DEBUG fix (post-L4-REJECT): reliability knobs for
# EventRepository's own outbound Postgres calls -- deliberately separate
# from the M6 knobs above, which guard RedisJobQueue's Redis calls (an
# unrelated dependency; conflating the two would mean tuning one outbound
# call's timeout/breaker also silently retunes the other's). The
# pre-existing `connect_timeout` on EventRepository bounds only the TCP
# handshake, never query execution or lock-wait time -- an independent L4
# VERIFY session proved that gap empirically (a table-locked INSERT
# stalled a live webhook request for seconds with no bound at all).
# `events_statement_timeout_ms` sets Postgres's own `statement_timeout` GUC
# on every connection `EventRepository` opens, so a stalled query --
# including one still waiting to acquire a lock -- is cancelled by Postgres
# itself after this many milliseconds. Same order of magnitude as
# connect_timeout's own 2-second default, since both bound the same class
# of "how long is one outbound call to this dependency allowed to take".
_DEFAULT_EVENTS_STATEMENT_TIMEOUT_MS = 2000
_DEFAULT_EVENTS_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
_DEFAULT_EVENTS_CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS = 30.0

# M8: the driver model for every LLM-backed specialist agent. Pinned to
# Haiku (not Opus, not Sonnet) per this milestone's explicit instruction --
# a cheap, fast model is the right default for a per-PR-diff specialist
# call that runs four times per review; Sonnet-tier judge calls (M13) are a
# separate, deliberately more expensive concern. Configurable via
# ANTHROPIC_MODEL so a future milestone (or an operator) can repoint every
# specialist at a different model without a code change.
_DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"

# M8 BudgetGuard: default daily USD cap on LLM spend, per PLAN.md's M8 text
# ("BudgetGuard stub, daily cap read from BUDGET_DAILY_CAP_USD env var,
# default 20"). Deliberately conservative for local development -- four
# specialists x however many PRs a day is expected to stay well under this
# for a haiku-tier model; a real production deployment should tune this to
# its own expected volume.
_DEFAULT_BUDGET_DAILY_CAP_USD = Decimal("20")

# M8 reliability knobs for backend.tools.llm_client.AnthropicLLMClient's
# outbound Anthropic API calls -- deliberately a THIRD, independent set
# from the M6 (Redis) and M7 (events Postgres) knobs above, mirroring the
# project's established pattern of one dependency, one set of knobs (tuning
# one outbound call's timeout/breaker must never silently retune another
# unrelated dependency's). An LLM completion call is expected to take
# meaningfully longer than a Redis round trip or a single-row Postgres
# insert, hence the larger default timeout and coarser backoff below.
_DEFAULT_LLM_TIMEOUT_SECONDS = 30.0
_DEFAULT_LLM_RETRY_MAX_ATTEMPTS = 3
_DEFAULT_LLM_RETRY_BASE_DELAY_SECONDS = 0.5
_DEFAULT_LLM_RETRY_MAX_DELAY_SECONDS = 8.0
_DEFAULT_LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
_DEFAULT_LLM_CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS = 30.0

# M9: local pgvector memory store (backend/memory/). Host port 5434 -- not
# 5432 (ampliphi-postgres-1, an unrelated project's container), not 5433
# (this project's own M7 events-spine Postgres), not 6379/6380 (Redis,
# unrelated protocol anyway) -- verified free with `lsof -i :5434` before
# choosing it (see this milestone's build report for the command output).
_DEFAULT_PGVECTOR_URL = "postgresql://postgres:postgres@localhost:5434/pr_review_memory"

# M9: which Embedder backend is used when no credential is available. The
# fixture path is the default deliberately -- this project's credential
# policy (see backend/memory/embedder.py) is that every test and this
# milestone's own demo command must run with no OPENAI_API_KEY / network
# access at all; "openai" is an explicit opt-in for a real embedding run.
_DEFAULT_EMBEDDER_BACKEND: Literal["fixture", "openai"] = "fixture"

# M9: the spec's pinned embeddings config -- text-embedding-3-large,
# truncated to 256 dimensions via OpenAI's own `dimensions` API parameter
# (not the model's native 3072-dim output). This is also the fixture
# embedder's output width and code_chunks.embedding's VECTOR(256) column
# width -- all three must agree, since a mismatch fails loudly at insert
# time (see backend.memory.context_retriever).
_DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-large"
_DEFAULT_EMBEDDING_DIMENSION = 256

# M9 reliability knobs for backend.memory.embedder.OpenAIEmbedder's outbound
# OpenAI API calls -- a FOURTH independent set, alongside M6 (Redis), M7
# (events Postgres), and M8 (Anthropic) above, per this project's
# established one-dependency-one-set-of-knobs pattern.
_DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 30.0
_DEFAULT_EMBEDDING_RETRY_MAX_ATTEMPTS = 3
_DEFAULT_EMBEDDING_RETRY_BASE_DELAY_SECONDS = 0.5
_DEFAULT_EMBEDDING_RETRY_MAX_DELAY_SECONDS = 8.0
_DEFAULT_EMBEDDING_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
_DEFAULT_EMBEDDING_CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS = 30.0

# M9: HybridRetriever's own defaults. See Settings.hybrid_retrieval_top_k /
# Settings.rrf_k docstrings below for the reasoning behind each value.
_DEFAULT_HYBRID_RETRIEVAL_TOP_K = 5
_DEFAULT_RRF_K = 60

# M11: which GitHubClient implementation backend.integrations.github_client.
# build_github_client constructs by default, mirroring embedder_backend's
# and job_queue_backend's own "safe local default, explicit opt-in for the
# real thing" pattern. "mock" (default) needs no GitHub App credential at
# all -- a keyless checkout of this repo, or every unit/e2e test, must
# still work. "real" makes actual GitHub REST calls and requires
# github_app_id/github_app_private_key_path to be set.
_DEFAULT_GITHUB_CLIENT_BACKEND: Literal["mock", "real"] = "mock"
_DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"

# M11 reliability knobs for backend.integrations.github_client.
# RealGitHubClient's outbound GitHub REST calls -- a FIFTH independent set,
# alongside M6 (Redis), M7 (events Postgres), M8 (Anthropic), and M9
# (OpenAI embeddings) above, per this project's established
# one-dependency-one-set-of-knobs pattern. Slightly more generous timeout
# and attempts than the Redis/events knobs: GitHub's REST API is a public
# internet dependency (not a same-docker-network service), and a single
# review can legitimately need several sequential calls (PR metadata, diff,
# paginated changed files, post review) each subject to GitHub's own
# request latency.
_DEFAULT_GITHUB_TIMEOUT_SECONDS = 15.0
_DEFAULT_GITHUB_RETRY_MAX_ATTEMPTS = 3
_DEFAULT_GITHUB_RETRY_BASE_DELAY_SECONDS = 0.5
_DEFAULT_GITHUB_RETRY_MAX_DELAY_SECONDS = 8.0
_DEFAULT_GITHUB_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
_DEFAULT_GITHUB_CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS = 30.0


# LangSmith tracing integration (opt-in). "Opt-in" mirrors every other
# backend-selection flag in this file (job_queue_backend/embedder_backend/
# events_backend/github_client_backend) -- a checkout with no LangSmith
# config at all must behave exactly as it did before this integration
# existed, which is why this defaults to False rather than "on whenever a
# key happens to be present". See backend/observability/tracing.py's
# module docstring for the silent-failure guard this backs, and
# .env.example's LangSmith block for the concrete AWS-endpoint +
# missing-workspace-id gotcha that cost a long debugging session: this
# org's LangSmith account is on the AWS deployment
# (aws.api.smith.langchain.com, not the default api.smith.langchain.com),
# and a service-account key there returns a bare 403 Forbidden on EVERY
# endpoint unless LANGSMITH_WORKSPACE_ID is set explicitly -- LangSmith's
# own setup snippet omits that variable entirely.
_DEFAULT_LANGSMITH_PROJECT = "pr-review"


# M12: which backend agent_events (Stage B) / code_chunks (Stage C)
# actually live on. "local" (the default) needs no Tiger Cloud account at
# all -- a keyless checkout, and every test/demo command that doesn't
# explicitly opt in, must still work end to end against docker-compose.yml's
# local Postgres/pgvector, mirroring embedder_backend's/job_queue_backend's/
# github_client_backend's own "safe local default, explicit opt-in for the
# real thing" pattern. "tiger" opts into the real, paid Tiger Cloud instance
# migrations/scripts/2026-06-tiger-init.sql provisions.
_DEFAULT_EVENTS_BACKEND: Literal["local", "tiger"] = "local"
_DEFAULT_MEMORY_BACKEND: Literal["local", "tiger"] = "local"


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
        database_url: M7 events spine. Connection string application code
            (``backend.observability``) actually writes events through --
            the restricted ``agent_events_writer`` role, never the admin
            superuser. Defaults to the host port this project's
            ``docker-compose.yml`` publishes locally (5433).
        database_admin_url: M7 events spine. Connection string used ONLY to
            apply migrations (``backend.database.postgres.apply_migrations``)
            -- the "postgres" superuser, since creating the restricted role/
            table/trigger requires privileges ``agent_events_writer`` is
            deliberately never granted. Never used by request-path code.
        events_statement_timeout_ms: M7 L2 DEBUG fix. Bound (milliseconds)
            Postgres itself enforces (via the `statement_timeout` GUC) on
            every ``EventRepository`` connection/query, including time
            spent waiting on a lock -- what actually bounds a stalled
            events write, since ``connect_timeout`` alone does not cover
            query execution. Default 2000 (2s).
        events_circuit_breaker_failure_threshold: M7 L2 DEBUG fix.
            Consecutive failures required to trip ``EventRepository``'s own
            circuit breaker to OPEN, independent of ``RedisJobQueue``'s.
        events_circuit_breaker_reset_timeout_seconds: M7 L2 DEBUG fix. How
            long ``EventRepository``'s breaker stays OPEN before allowing a
            single HALF_OPEN probe through.
        anthropic_api_key: M8. Credential for the Anthropic API. Deliberately
            ``str | None`` with no default (unlike ``github_webhook_secret``,
            which fails fast if blank) -- this project's own credential
            policy is that every unit test must pass without this key set
            (a fake/stub LLM client stands in), and only the real live-demo
            path actually needs it. ``backend.tools.llm_client.
            AnthropicLLMClient`` raises its own clear error at the point it
            would actually need this value, not at Settings construction.
        anthropic_model: M8. The driver model every LLM-backed specialist
            calls. Default ``claude-haiku-4-5`` (see module-level comment
            for why Haiku, not Opus/Sonnet).
        budget_daily_cap_usd: M8. ``backend.economics.budget.BudgetGuard``'s
            daily USD cap on total LLM spend (summed from ``agent_events``'
            ``llm.call`` rows) -- once today's spend meets or exceeds this,
            the next call is hard-blocked before it reaches the LLM client.
            Default 20, per PLAN.md.
        llm_timeout_seconds: M8 reliability layer. Bound (seconds) on each
            individual outbound Anthropic API call.
        llm_retry_max_attempts: M8 reliability layer. Total attempts
            (including the first) per LLM call before giving up.
        llm_retry_base_delay_seconds: M8 reliability layer. Delay before the
            second retry attempt; grows exponentially after that, capped by
            ``llm_retry_max_delay_seconds``.
        llm_retry_max_delay_seconds: M8 reliability layer. Upper bound the
            exponential backoff delay is clamped to.
        llm_circuit_breaker_failure_threshold: M8 reliability layer.
            Consecutive failures required to trip the LLM client's circuit
            breaker to OPEN, independent of the M6/M7 breakers above.
        llm_circuit_breaker_reset_timeout_seconds: M8 reliability layer. How
            long the LLM client's breaker stays OPEN before allowing a
            single HALF_OPEN probe through.
        pgvector_url: M9. Connection string for the local pgvector memory
            store (``backend.memory.tiger_client``), backing the
            ``code_chunks`` table. Defaults to the host port
            ``docker-compose.yml``'s ``pgvector`` service publishes locally
            (5434 -- see that file's comment for why not 5432/5433/6379/
            6380).
        embedder_backend: M9. Which ``Embedder`` implementation
            ``backend.memory.embedder.get_embedder`` constructs by default.
            ``"fixture"`` (the default) needs no network or API key --
            every unit/integration test and this milestone's demo command
            use it. ``"openai"`` makes real ``text-embedding-3-large``
            calls and requires ``openai_api_key`` to be set.
        openai_api_key: M9. Credential for OpenAI's embeddings API.
            Deliberately optional with no default, mirroring
            ``anthropic_api_key`` -- every test passes without it; only
            ``embedder_backend="openai"`` actually needs a real value.
            ``OpenAIEmbedder`` raises its own clear error at the point a
            real call would need this, not at Settings construction.
        openai_embedding_model: M9. The embeddings model
            ``OpenAIEmbedder`` calls. Default ``text-embedding-3-large``,
            per the spec's pinned config.
        embedding_dimension: M9. The dimensionality every embedding in
            this system must have -- passed as OpenAI's own ``dimensions``
            truncation parameter for the real embedder, and as the target
            vector length ``DeterministicFixtureEmbedder`` produces, and
            matches ``code_chunks.embedding``'s ``VECTOR(256)`` column type
            (``migrations/scripts/dev-pgvector-init.sql``). Default 256,
            per the spec's pinned text-embedding-3-large-at-256-dims config
            -- changing this without also changing the migration's column
            type would make every insert fail with a dimension mismatch
            (by design; see ``backend.memory.context_retriever``).
        embedding_timeout_seconds: M9 reliability layer. Bound (seconds) on
            each individual outbound OpenAI embeddings API call.
        embedding_retry_max_attempts: M9 reliability layer. Total attempts
            (including the first) per embeddings call before giving up.
        embedding_retry_base_delay_seconds: M9 reliability layer. Delay
            before the 2nd embeddings retry attempt; grows exponentially
            after that, capped by ``embedding_retry_max_delay_seconds``.
        embedding_retry_max_delay_seconds: M9 reliability layer. Upper
            bound the embeddings retry backoff is clamped to.
        embedding_circuit_breaker_failure_threshold: M9 reliability layer.
            Consecutive failures required to trip the embedder's circuit
            breaker to OPEN, independent of the M6/M7/M8 breakers above --
            a fourth, independent outbound dependency.
        embedding_circuit_breaker_reset_timeout_seconds: M9 reliability
            layer. How long the embedder's breaker stays OPEN before
            allowing a single HALF_OPEN probe through.
        hybrid_retrieval_top_k: M9. Default number of fused results
            ``HybridRetriever.hybrid_search`` returns when the caller does
            not pass an explicit ``top_k``.
        rrf_k: M9. The reciprocal rank fusion constant (the ``k`` in
            ``score = sum(1 / (k + rank))``). 60 is the commonly cited
            default in the RRF literature (Cormack et al., 2009) -- large
            enough that a document's exact rank near the top of one ranker
            does not completely dominate its fused score, so a
            second-place hit from one ranker and a first-place hit from
            another can still combine meaningfully rather than one ranker
            unilaterally deciding the top result.
        events_backend: M12. 'local' (default) or 'tiger' -- which store
            agent_events lives on. See its own Field description.
        memory_backend: M12. 'local' (default) or 'tiger' -- which store
            code_chunks lives on. See its own Field description.
        tiger_database_url: M12. Optional explicit Tiger Cloud admin DSN
            override; unset by default in favor of native PG* env vars.
            See ``resolve_tiger_dsn``'s docstring.
        pghost, pgport, pgdatabase, pgsslmode: M12. libpq's own native
            PG* variables, read here only so ``resolve_tiger_writer_dsn``
            can build the restricted role's own connection string.
        tiger_events_writer_password: M12. The restricted
            ``agent_events_writer`` role's password, set out of band.
            Required for ``events_backend='tiger'`` application runtime.
        langsmith_tracing: LangSmith tracing opt-in flag. See its own
            ``Field`` description for the full "opt-in, off by default,
            independent of the ambient environment" reasoning.
        langsmith_api_key: LangSmith service-account API key. Optional,
            no default -- mirrors ``anthropic_api_key``.
        langsmith_endpoint: LangSmith API base URL. ``None`` falls back to
            the SDK's own default, which is the wrong region for this
            project's org -- see the AWS-deployment gotcha documented on
            its own ``Field``.
        langsmith_workspace_id: LangSmith workspace id. Required on this
            project's AWS-hosted LangSmith deployment or every call 403s;
            see its own ``Field`` for the full gotcha.
        langsmith_project: LangSmith project (session) name. Default
            ``"pr-review"``.
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

    database_url: str = Field(
        default=_DEFAULT_DATABASE_URL,
        description=(
            "Postgres connection string the application writes events "
            "through (the restricted agent_events_writer role). Defaults "
            "to the host port docker-compose.yml publishes (5433)."
        ),
    )

    database_admin_url: str = Field(
        default=_DEFAULT_DATABASE_ADMIN_URL,
        description=(
            "Postgres connection string used only to apply migrations "
            "(the postgres superuser). Never used by request-path code."
        ),
    )

    events_backend: Literal["local", "tiger"] = Field(
        default=_DEFAULT_EVENTS_BACKEND,
        description=(
            "M12. Which store agent_events actually lives on. 'local' "
            "(default) needs no Tiger Cloud account -- every test and a "
            "keyless checkout use it. 'tiger' routes EventRepository and "
            "migration application at the real Tiger Cloud hypertable "
            "instead (see resolve_tiger_dsn/resolve_tiger_writer_dsn)."
        ),
    )

    memory_backend: Literal["local", "tiger"] = Field(
        default=_DEFAULT_MEMORY_BACKEND,
        description=(
            "M12. Which store code_chunks actually lives on. 'local' "
            "(default) needs no Tiger Cloud account -- HybridRetriever "
            "talks to docker-compose.yml's pgvector service. 'tiger' "
            "routes it at the real Tiger Cloud DiskANN index instead."
        ),
    )

    tiger_database_url: str | None = Field(
        default=None,
        description=(
            "M12. Optional EXPLICIT libpq connection URI/conninfo for "
            "Tiger Cloud (e.g. 'postgresql://user:pass@host:port/db"
            "?sslmode=require'), used as the ADMIN/migration connection "
            "when events_backend/memory_backend='tiger'. This is NOT the "
            "primary configuration path -- see resolve_tiger_dsn's "
            "docstring for why PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE/"
            "PGSSLMODE (libpq's own native environment variables, which "
            "psycopg and psql both already read without any code here) "
            "are preferred. This field exists only for the cases that "
            "genuinely need one bundled string instead of five separate "
            "variables -- e.g. handing a single secret to a CI/CD "
            "pipeline, or a one-off `psql \"$TIGER_DATABASE_URL\"` from a "
            "shell that does not already have the PG* variables set. When "
            "unset (the common case for local development, including this "
            "project's own actual Tiger Cloud configuration), Tiger "
            "connections fall back to libpq's native PG* environment "
            "variables entirely."
        ),
    )

    pghost: str | None = Field(
        default=None,
        validation_alias="PGHOST",
        description=(
            "M12. Tiger Cloud host, libpq's native PGHOST variable. Read "
            "here (in addition to psycopg/psql's own native libpq "
            "handling) only so resolve_tiger_writer_dsn can build the "
            "restricted agent_events_writer role's own connection string "
            "-- see that method's docstring for why that one connection "
            "cannot simply reuse an empty conninfo the way the admin "
            "connection does."
        ),
    )

    pgport: int | None = Field(
        default=None,
        validation_alias="PGPORT",
        description="M12. Tiger Cloud port, libpq's native PGPORT variable.",
    )

    pgdatabase: str | None = Field(
        default=None,
        validation_alias="PGDATABASE",
        description="M12. Tiger Cloud database name, libpq's native PGDATABASE variable.",
    )

    pgsslmode: str | None = Field(
        default=None,
        validation_alias="PGSSLMODE",
        description="M12. Tiger Cloud SSL mode, libpq's native PGSSLMODE variable.",
    )

    tiger_events_writer_password: str | None = Field(
        default=None,
        description=(
            "M12. Password for the restricted `agent_events_writer` role "
            "migrations/scripts/2026-06-tiger-init.sql creates on Tiger "
            "Cloud (SELECT+INSERT only, UPDATE/DELETE/TRUNCATE revoked -- "
            "see that file's Stage B comment for why this role is "
            "LOAD-BEARING on a hypertable, not merely defense in depth). "
            "Set once, out of band, via `ALTER ROLE agent_events_writer "
            "PASSWORD ...` by whoever provisions a given Tiger instance -- "
            "never generated or written by application code, never "
            "committed. Required for events_backend='tiger' application "
            "runtime (not migrations, which use the admin credential "
            "above); resolve_tiger_writer_dsn raises a clear error if "
            "events_backend='tiger' and this is unset."
        ),
    )

    events_statement_timeout_ms: int = Field(
        default=_DEFAULT_EVENTS_STATEMENT_TIMEOUT_MS,
        gt=0,
        description=(
            "Postgres statement_timeout (ms) applied to every "
            "EventRepository connection -- bounds query execution and "
            "lock-wait time, which connect_timeout alone does not cover. "
            "Default 2000 (2s)."
        ),
    )

    events_circuit_breaker_failure_threshold: int = Field(
        default=_DEFAULT_EVENTS_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        ge=1,
        description=(
            "Consecutive failures required to trip EventRepository's own "
            "circuit breaker to OPEN. Default 5."
        ),
    )

    events_circuit_breaker_reset_timeout_seconds: float = Field(
        default=_DEFAULT_EVENTS_CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS,
        gt=0,
        description=(
            "How long (seconds) EventRepository's circuit breaker stays "
            "OPEN before allowing a single HALF_OPEN probe through. "
            "Default 30.0."
        ),
    )

    anthropic_api_key: str | None = Field(
        default=None,
        description=(
            "Anthropic API key for the M8 LLM-backed specialist agents. "
            "Deliberately optional with no default -- every unit test must "
            "pass without it (a fake LLM client stands in); only the real "
            "live demo command needs a real value."
        ),
    )

    anthropic_model: str = Field(
        default=_DEFAULT_ANTHROPIC_MODEL,
        min_length=1,
        description=(
            "Driver model every LLM-backed specialist agent calls. Default "
            "claude-haiku-4-5 -- deliberately not Opus, per this project's "
            "approved driver-model decision."
        ),
    )

    budget_daily_cap_usd: Decimal = Field(
        default=_DEFAULT_BUDGET_DAILY_CAP_USD,
        gt=Decimal("0"),
        description=(
            "BudgetGuard's daily USD cap on total LLM spend, summed from "
            "agent_events' llm.call rows. Default 20."
        ),
    )

    llm_timeout_seconds: float = Field(
        default=_DEFAULT_LLM_TIMEOUT_SECONDS,
        gt=0,
        description="Bound (seconds) on each individual outbound Anthropic API call. Default 30.0.",
    )

    llm_retry_max_attempts: int = Field(
        default=_DEFAULT_LLM_RETRY_MAX_ATTEMPTS,
        ge=1,
        description=(
            "Total attempts (including the first) the LLM client makes per "
            "call before giving up. Default 3."
        ),
    )

    llm_retry_base_delay_seconds: float = Field(
        default=_DEFAULT_LLM_RETRY_BASE_DELAY_SECONDS,
        gt=0,
        description="Delay (seconds) before the 2nd LLM retry attempt. Default 0.5.",
    )

    llm_retry_max_delay_seconds: float = Field(
        default=_DEFAULT_LLM_RETRY_MAX_DELAY_SECONDS,
        gt=0,
        description="Upper bound (seconds) the LLM retry backoff is clamped to. Default 8.0.",
    )

    llm_circuit_breaker_failure_threshold: int = Field(
        default=_DEFAULT_LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        ge=1,
        description=(
            "Consecutive failures required to trip the LLM client's "
            "circuit breaker to OPEN. Default 5."
        ),
    )

    llm_circuit_breaker_reset_timeout_seconds: float = Field(
        default=_DEFAULT_LLM_CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS,
        gt=0,
        description=(
            "How long (seconds) the LLM client's circuit breaker stays "
            "OPEN before allowing a single HALF_OPEN probe through. "
            "Default 30.0."
        ),
    )

    pgvector_url: str = Field(
        default=_DEFAULT_PGVECTOR_URL,
        description=(
            "Postgres+pgvector connection string backing the code_chunks "
            "hybrid-retrieval table. Defaults to the host port "
            "docker-compose.yml's pgvector service publishes (5434)."
        ),
    )

    embedder_backend: Literal["fixture", "openai"] = Field(
        default=_DEFAULT_EMBEDDER_BACKEND,
        description=(
            "Which Embedder implementation get_embedder() constructs by "
            "default. 'fixture' (default) is deterministic and needs no "
            "network/API key; 'openai' makes real text-embedding-3-large "
            "calls and requires openai_api_key."
        ),
    )

    openai_api_key: str | None = Field(
        default=None,
        description=(
            "OpenAI API key for the M9 real embedder. Deliberately "
            "optional with no default -- every test passes without it "
            "(DeterministicFixtureEmbedder stands in); only "
            "embedder_backend='openai' actually needs a real value."
        ),
    )

    openai_embedding_model: str = Field(
        default=_DEFAULT_OPENAI_EMBEDDING_MODEL,
        min_length=1,
        description="Embeddings model OpenAIEmbedder calls. Default text-embedding-3-large.",
    )

    embedding_dimension: int = Field(
        default=_DEFAULT_EMBEDDING_DIMENSION,
        gt=0,
        description=(
            "Dimensionality every embedding must have -- OpenAI's "
            "'dimensions' truncation parameter for the real embedder, "
            "DeterministicFixtureEmbedder's output width, and "
            "code_chunks.embedding's VECTOR(256) column width. Default "
            "256, per the spec's pinned config."
        ),
    )

    embedding_timeout_seconds: float = Field(
        default=_DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
        gt=0,
        description="Bound (seconds) on each individual outbound OpenAI embeddings call.",
    )

    embedding_retry_max_attempts: int = Field(
        default=_DEFAULT_EMBEDDING_RETRY_MAX_ATTEMPTS,
        ge=1,
        description=(
            "Total attempts (including the first) the embedder makes per "
            "call before giving up. Default 3."
        ),
    )

    embedding_retry_base_delay_seconds: float = Field(
        default=_DEFAULT_EMBEDDING_RETRY_BASE_DELAY_SECONDS,
        gt=0,
        description="Delay (seconds) before the 2nd embeddings retry attempt. Default 0.5.",
    )

    embedding_retry_max_delay_seconds: float = Field(
        default=_DEFAULT_EMBEDDING_RETRY_MAX_DELAY_SECONDS,
        gt=0,
        description="Upper bound (seconds) the embeddings retry backoff is clamped to. Default 8.0.",
    )

    embedding_circuit_breaker_failure_threshold: int = Field(
        default=_DEFAULT_EMBEDDING_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        ge=1,
        description=(
            "Consecutive failures required to trip the embedder's "
            "circuit breaker to OPEN. Default 5."
        ),
    )

    embedding_circuit_breaker_reset_timeout_seconds: float = Field(
        default=_DEFAULT_EMBEDDING_CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS,
        gt=0,
        description=(
            "How long (seconds) the embedder's circuit breaker stays OPEN "
            "before allowing a single HALF_OPEN probe through. Default 30.0."
        ),
    )

    hybrid_retrieval_top_k: int = Field(
        default=_DEFAULT_HYBRID_RETRIEVAL_TOP_K,
        ge=1,
        description="Default number of fused results HybridRetriever.hybrid_search returns.",
    )

    rrf_k: int = Field(
        default=_DEFAULT_RRF_K,
        ge=1,
        description=(
            "Reciprocal rank fusion constant k in score = sum(1/(k+rank)). "
            "Default 60, the commonly cited RRF literature default."
        ),
    )

    github_app_id: str | None = Field(
        default=None,
        description=(
            "M11. This project's GitHub App id (the 'iss' claim of every "
            "app-level JWT it mints). Deliberately optional with no "
            "default, mirroring anthropic_api_key/openai_api_key -- every "
            "test passes without it (MockGitHubClient stands in); only "
            "github_client_backend='real' actually needs a real value."
        ),
    )

    github_app_private_key_path: str | None = Field(
        default=None,
        description=(
            "M11. Filesystem path to the GitHub App's PEM-encoded RSA "
            "private key (e.g. secrets/github-app.pem -- see .gitignore). "
            "Read fresh from disk by RealGitHubClient at construction, "
            "never embedded in an environment variable or logged."
        ),
    )

    github_client_backend: Literal["mock", "real"] = Field(
        default=_DEFAULT_GITHUB_CLIENT_BACKEND,
        description=(
            "Which GitHubClient implementation "
            "backend.integrations.github_client.build_github_client "
            "constructs by default. 'mock' (default) needs no GitHub App "
            "credential -- every test and a keyless checkout use it. "
            "'real' makes actual GitHub REST calls and requires "
            "github_app_id/github_app_private_key_path."
        ),
    )

    github_api_base_url: str = Field(
        default=_DEFAULT_GITHUB_API_BASE_URL,
        min_length=1,
        description="Base URL for the real GitHub REST API. Overridable for testing against a fake server.",
    )

    github_timeout_seconds: float = Field(
        default=_DEFAULT_GITHUB_TIMEOUT_SECONDS,
        gt=0,
        description="Bound (seconds) on each individual outbound GitHub REST API call. Default 15.0.",
    )

    github_retry_max_attempts: int = Field(
        default=_DEFAULT_GITHUB_RETRY_MAX_ATTEMPTS,
        ge=1,
        description=(
            "Total attempts (including the first) RealGitHubClient makes "
            "per GitHub REST call before giving up. Default 3."
        ),
    )

    github_retry_base_delay_seconds: float = Field(
        default=_DEFAULT_GITHUB_RETRY_BASE_DELAY_SECONDS,
        gt=0,
        description="Delay (seconds) before the 2nd GitHub retry attempt. Default 0.5.",
    )

    github_retry_max_delay_seconds: float = Field(
        default=_DEFAULT_GITHUB_RETRY_MAX_DELAY_SECONDS,
        gt=0,
        description="Upper bound (seconds) the GitHub retry backoff is clamped to. Default 8.0.",
    )

    github_circuit_breaker_failure_threshold: int = Field(
        default=_DEFAULT_GITHUB_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        ge=1,
        description=(
            "Consecutive failures required to trip RealGitHubClient's "
            "circuit breaker to OPEN. Default 5."
        ),
    )

    github_circuit_breaker_reset_timeout_seconds: float = Field(
        default=_DEFAULT_GITHUB_CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS,
        gt=0,
        description=(
            "How long (seconds) RealGitHubClient's circuit breaker stays "
            "OPEN before allowing a single HALF_OPEN probe through. "
            "Default 30.0."
        ),
    )

    langsmith_tracing: bool = Field(
        default=False,
        description=(
            "Whether LangGraph/LangChain callbacks actually emit traces to "
            "LangSmith. Opt-in, off by default -- a checkout with no "
            "LangSmith config behaves exactly as before this integration "
            "existed. Setting this alone is not sufficient for real traces "
            "to land: the LangSmith SDK's own tracing machinery reads "
            "LANGSMITH_TRACING/LANGSMITH_API_KEY/LANGSMITH_ENDPOINT/"
            "LANGSMITH_WORKSPACE_ID/LANGSMITH_PROJECT from the process "
            "environment directly (that is how LangChain's global callback "
            "manager decides whether to attach a tracer at all) -- this "
            "field is this project's OWN gate for whether "
            "backend.observability.tracing.assert_tracing_healthy runs, "
            "kept independent of the ambient environment so the guard "
            "itself is deterministic and testable."
        ),
    )

    langsmith_api_key: str | None = Field(
        default=None,
        description=(
            "LangSmith service-account API key (an lsv2_sk_... value). "
            "Deliberately optional with no default, mirroring "
            "anthropic_api_key/openai_api_key -- every test passes without "
            "it; only langsmith_tracing=True actually needs a real value. "
            "Never logged or printed by any code in this project."
        ),
    )

    langsmith_endpoint: str | None = Field(
        default=None,
        description=(
            "LangSmith API base URL. None (unset) lets the LangSmith SDK "
            "fall back to its own default (api.smith.langchain.com) -- "
            "which is the WRONG endpoint for any org whose LangSmith "
            "account lives on a regional deployment. This project's own "
            "org is on the AWS deployment, "
            "https://aws.api.smith.langchain.com -- see .env.example."
        ),
    )

    langsmith_workspace_id: str | None = Field(
        default=None,
        description=(
            "LangSmith workspace id. THE GOTCHA THIS FIELD EXISTS TO "
            "DOCUMENT: on this project's AWS-hosted LangSmith deployment, "
            "a service-account key returns a bare 403 Forbidden on EVERY "
            "endpoint (create a run, read a run, everything) unless this "
            "is set -- even though the key itself is valid and LangSmith's "
            "own quickstart snippet does not mention this variable at "
            "all. Also note: /api/v1/api-key/current returns a "
            "DIFFERENT, unrelated 401 ('User ID required for this "
            "endpoint. Cannot use a service account.') even with a "
            "correct key + workspace id -- that endpoint is not a usable "
            "health check for a service key; "
            "backend.observability.tracing.assert_tracing_healthy uses a "
            "real probe run instead, precisely because of this footgun."
        ),
    )

    langsmith_project: str = Field(
        default=_DEFAULT_LANGSMITH_PROJECT,
        min_length=1,
        description=(
            "LangSmith project (session) name traces and the startup "
            "probe run are grouped under. Default 'pr-review'."
        ),
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def resolve_tiger_dsn(self) -> str:
        """The ADMIN/migration connection string for Tiger Cloud (tsdbadmin-equivalent).

        Returns ``tiger_database_url`` verbatim when it is set (the
        explicit-override path -- see that field's docstring for when this
        is the right choice). Otherwise returns the empty string, which is
        the deliberate PRIMARY path this milestone chose: psycopg (and
        psql) both pass an empty conninfo straight through to libpq, which
        then fills every connection parameter from its own native
        PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE/PGSSLMODE environment
        variables -- exactly what the user configured in ``.env`` for this
        project's real Tiger Cloud instance, and exactly what a bare
        ``psql`` with no arguments already does. This means every
        connection-opening call site in this codebase (``psycopg.connect``,
        ``backend.database.postgres.apply_migrations``/``init_tiger_schema``,
        ``backend.memory.tiger_client.connect``) needs NO code change at all
        to support Tiger -- it already accepts an arbitrary DSN string, and
        "" is simply a valid one.
        """
        return self.tiger_database_url if self.tiger_database_url else ""

    def resolve_tiger_writer_dsn(self) -> str:
        """The restricted ``agent_events_writer`` role's connection string for Tiger Cloud.

        Unlike ``resolve_tiger_dsn`` (the admin connection, used only to
        apply migrations), this is what application code actually writes
        ``agent_events`` through when ``events_backend='tiger'`` -- mirroring
        local's own ``database_url`` (writer) vs. ``database_admin_url``
        (admin) split. It cannot simply fall back to an empty conninfo the
        way the admin connection does, because libpq's PG* environment
        variables name the ADMIN user (``PGUSER=tsdbadmin`` in this
        project's own ``.env``) -- this method explicitly overrides the user
        and password while reusing the same host/port/database/sslmode, via
        ``psycopg.conninfo.make_conninfo`` (not hand-rolled string
        formatting) so a password containing special characters is quoted
        correctly rather than corrupting the conninfo string.

        Raises:
            RuntimeError: ``pghost`` or ``tiger_events_writer_password`` is
                unset -- both are required to build this connection string,
                and there is no safe default for either (unlike the admin
                path, there is no "just use libpq's environment" fallback,
                since PGUSER/PGPASSWORD in the environment name the admin
                role, not this restricted one).
        """
        if not self.pghost or not self.tiger_events_writer_password:
            raise RuntimeError(
                "events_backend='tiger' requires PGHOST and "
                "TIGER_EVENTS_WRITER_PASSWORD to build the restricted "
                "agent_events_writer connection string -- see "
                "Settings.tiger_events_writer_password's docstring."
            )
        return make_conninfo(
            host=self.pghost,
            port=self.pgport if self.pgport is not None else 5432,
            dbname=self.pgdatabase if self.pgdatabase is not None else "tsdb",
            user="agent_events_writer",
            password=self.tiger_events_writer_password,
            sslmode=self.pgsslmode if self.pgsslmode is not None else "require",
        )

    @property
    def effective_database_url(self) -> str:
        """The connection string application code actually writes ``agent_events`` through.

        ``events_backend='local'`` (default): ``database_url`` unchanged --
        every existing call site's behavior is byte-for-byte identical to
        before this property existed. ``events_backend='tiger'``: the
        restricted ``agent_events_writer`` role's DSN (never the admin
        credential -- see ``resolve_tiger_writer_dsn``).
        """
        if self.events_backend == "tiger":
            return self.resolve_tiger_writer_dsn()
        return self.database_url

    @property
    def effective_database_admin_url(self) -> str:
        """The connection string used ONLY to apply migrations against ``agent_events``.

        ``events_backend='local'`` (default): ``database_admin_url``
        unchanged. ``events_backend='tiger'``: the Tiger admin DSN (see
        ``resolve_tiger_dsn``) -- used by
        ``backend.database.postgres.init_tiger_schema``, never by
        request-path code.
        """
        if self.events_backend == "tiger":
            return self.resolve_tiger_dsn()
        return self.database_admin_url

    @property
    def effective_pgvector_url(self) -> str:
        """The connection string ``HybridRetriever``/``code_chunks`` actually use.

        ``memory_backend='local'`` (default): ``pgvector_url`` unchanged.
        ``memory_backend='tiger'``: the Tiger admin DSN (see
        ``resolve_tiger_dsn``) -- ``code_chunks`` has no append-only
        invariant (it is a fully rebuildable retrieval index, see
        ``migrations/scripts/dev-pgvector-init.sql``'s module docstring),
        so unlike ``agent_events`` there is no restricted-role split here:
        one Tiger credential is used for both migrations and application
        reads/writes against this table.
        """
        if self.memory_backend == "tiger":
            return self.resolve_tiger_dsn()
        return self.pgvector_url


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
