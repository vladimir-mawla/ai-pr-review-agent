"""FastAPI application assembly.

Owns: constructing the FastAPI app and wiring its dependencies (settings, job
queue) into ``app.state``, so routes read them via dependency injection
instead of module-level globals. This is what lets ``tests/unit/test_webhook_validator.py``
build an isolated app per test (own secret, own empty queue) while
``uvicorn backend.api.main:app`` still gets a normal, real-config instance
for local running.
"""

from __future__ import annotations

from fastapi import FastAPI

from backend.core.settings import Settings, get_settings
from backend.job_queue.in_memory import InMemoryJobQueue
from backend.job_queue.interface import JobQueue
from backend.webhook_receiver.router import router as webhook_router


def create_app(settings: Settings | None = None, job_queue: JobQueue | None = None) -> FastAPI:
    """Build a FastAPI app instance with explicit, injectable dependencies.

    Args:
        settings: Configuration to use. Defaults to the process-wide
            ``get_settings()`` singleton (reads env / ``.env``) when not
            given — the production/local-run path. Tests pass an explicit
            ``Settings`` instance instead, so they never depend on the real
            environment.
        job_queue: The enqueue target. Defaults to a fresh
            ``InMemoryJobQueue`` — the M2 stand-in for the real Redis/ARQ
            queue M3 will add. Tests pass their own instance so they can
            assert on its contents after making requests.

    Returns:
        A fully configured FastAPI app with the webhook router mounted.
    """
    app = FastAPI(title="pr-review-agent", version="0.1.0")
    app.state.settings = settings if settings is not None else get_settings()
    app.state.job_queue = job_queue if job_queue is not None else InMemoryJobQueue()
    app.include_router(webhook_router)
    return app


# Module-level app object for `uvicorn backend.api.main:app`. Building this
# eagerly means a missing GITHUB_WEBHOOK_SECRET fails fast at process start
# (via Settings' validation) rather than on the first request.
app = create_app()
