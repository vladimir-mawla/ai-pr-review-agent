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

from backend.core.settings import Settings, get_settings
from backend.job_queue.in_memory import InMemoryJobQueue
from backend.job_queue.interface import JobQueue
from backend.job_queue.redis_arq import RedisJobQueue
from backend.webhook_receiver.router import router as webhook_router


def _default_job_queue(settings: Settings) -> JobQueue:
    """Build the JobQueue a real (non-test) app uses, per ``settings.job_queue_backend``."""
    if settings.job_queue_backend == "redis":
        return RedisJobQueue(settings)
    return InMemoryJobQueue()


def create_app(settings: Settings | None = None, job_queue: JobQueue | None = None) -> FastAPI:
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

    Returns:
        A fully configured FastAPI app with the webhook router mounted.
    """
    app = FastAPI(title="pr-review-agent", version="0.1.0")
    resolved_settings = settings if settings is not None else get_settings()
    app.state.settings = resolved_settings
    app.state.job_queue = (
        job_queue if job_queue is not None else _default_job_queue(resolved_settings)
    )
    app.include_router(webhook_router)
    return app


# Module-level app object for `uvicorn backend.api.main:app`. Building this
# eagerly means a missing GITHUB_WEBHOOK_SECRET fails fast at process start
# (via Settings' validation) rather than on the first request.
app = create_app()
