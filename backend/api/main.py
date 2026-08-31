"""FastAPI application assembly.

Owns: constructing the FastAPI app and wiring its dependencies (settings, job
queue) into ``app.state``, so routes read them via dependency injection
instead of module-level globals. This is what lets ``tests/unit/test_webhook_validator.py``
build an isolated app per test (own secret, own empty queue) while
``uvicorn backend.api.main:app`` still gets a normal, real-config instance
for local running.

Which ``JobQueue`` implementation backs a real (non-test-injected) app is
settings-driven (``Settings.job_queue_backend``): ``"in_memory"`` keeps
local dev and the existing webhook tests fast and Docker-independent;
``"redis"`` is what the M3 docker-compose demo uses. Either way, this
module is the *only* place that decides which concrete class to
instantiate — ``backend.webhook_receiver.router`` only ever sees the
``JobQueue`` Protocol, unchanged since M2.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.dashboard import router as dashboard_router
from backend.core.settings import Settings, get_settings
from backend.database.repository import EventRepository
from backend.database.review_store import ReviewRepository
from backend.job_queue.in_memory import InMemoryJobQueue
from backend.job_queue.interface import JobQueue
from backend.job_queue.redis_arq import RedisJobQueue
from backend.webhook_receiver.router import router as webhook_router

# M13: the Next.js dashboard runs on a separate origin in local dev
# (http://localhost:3000 by default) and calls this API's /api/* routes
# directly from the browser (a Client Component fetch -- see
# frontend/src/lib/api.ts), so the dashboard's own dev server origin must
# be allowed cross-origin. Kept to a small, explicit local-dev allowlist
# rather than "*" -- this API also serves the GitHub webhook route, which
# must never send permissive CORS headers by default.
_DASHBOARD_DEV_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def _default_job_queue(settings: Settings) -> JobQueue:
    """Build the JobQueue a real (non-test) app uses, per ``settings.job_queue_backend``."""
    if settings.job_queue_backend == "redis":
        return RedisJobQueue(settings)
    return InMemoryJobQueue()


def _default_event_repository(settings: Settings) -> EventRepository:
    """Build the EventRepository a real (non-test) app uses, from Settings.

    Threads through the M7 L2 DEBUG fix's dedicated reliability knobs
    (``events_statement_timeout_ms`` and its own circuit breaker
    thresholds -- see ``backend.core.settings.Settings`` and
    ``backend.database.repository.EventRepository``) rather than leaving
    them at ``EventRepository``'s bare constructor defaults, so a real
    deployment's behavior is actually controlled by ``.env`` /
    environment configuration like every other tunable in this file.
    """
    return EventRepository(
        settings.database_url,
        statement_timeout_ms=settings.events_statement_timeout_ms,
        circuit_breaker_failure_threshold=settings.events_circuit_breaker_failure_threshold,
        circuit_breaker_reset_timeout_seconds=settings.events_circuit_breaker_reset_timeout_seconds,
    )


def _default_review_repository(settings: Settings) -> ReviewRepository:
    """Build the ``ReviewRepository`` a real (non-test) app uses, from ``Settings``.

    M13: backs the dashboard's HITL-queue/trace views (``backend.api.
    dashboard``). Same connection string as ``_default_event_repository``
    above (``reviews`` lives in the same Postgres database as
    ``agent_events``) -- see ``backend.database.review_store``'s module
    docstring for why this table exists.
    """
    return ReviewRepository(settings.database_url)


def create_app(
    settings: Settings | None = None,
    job_queue: JobQueue | None = None,
    event_repository: EventRepository | None = None,
    review_repository: ReviewRepository | None = None,
) -> FastAPI:
    """Build a FastAPI app instance with explicit, injectable dependencies.

    Args:
        settings: Configuration to use. Defaults to the process-wide
            ``get_settings()`` singleton (reads env / ``.env``) when not
            given — the production/local-run path. Tests pass an explicit
            ``Settings`` instance instead, so they never depend on the real
            environment.
        job_queue: The enqueue target. Defaults to whichever implementation
            ``settings.job_queue_backend`` selects (see
            ``_default_job_queue``) when not given. Tests pass their own
            instance so they can assert on its contents after making
            requests, independent of both the environment and Docker.
        event_repository: M7. Where the webhook router's decision events
            (``backend.observability.emit_decision``) are written. Defaults
            to ``EventRepository(resolved_settings.database_url)`` when not
            given. Tests pass their own instance (e.g. pointed at an
            unreachable DSN) so "the events database is down" can be
            exercised for one isolated app instance without touching the
            real, shared docker-compose Postgres other tests depend on --
            the same per-app isolation ``job_queue`` already gives Redis-
            down simulations (see ``tests/unit/test_reliability.py``).
        review_repository: M13. Backs ``backend.api.dashboard``'s HITL
            queue/trace routes. Defaults to
            ``ReviewRepository(resolved_settings.database_url)`` when not
            given; tests pass their own instance for the same isolation
            reasons as ``event_repository`` above.

    Returns:
        A fully configured FastAPI app with the webhook and dashboard
        routers mounted.
    """
    app = FastAPI(title="pr-review-agent", version="0.1.0")
    resolved_settings = settings if settings is not None else get_settings()
    app.state.settings = resolved_settings
    app.state.job_queue = (
        job_queue if job_queue is not None else _default_job_queue(resolved_settings)
    )
    app.state.event_repository = (
        event_repository
        if event_repository is not None
        else _default_event_repository(resolved_settings)
    )
    app.state.review_repository = (
        review_repository
        if review_repository is not None
        else _default_review_repository(resolved_settings)
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DASHBOARD_DEV_ORIGINS,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.include_router(webhook_router)
    app.include_router(dashboard_router)
    return app


# Module-level app object for `uvicorn backend.api.main:app`. Building this
# eagerly means a missing GITHUB_WEBHOOK_SECRET fails fast at process start
# (via Settings' validation) rather than on the first request.
app = create_app()
