"""ARQ worker process for the real job queue.

Owns: the consumer side of the M3 hand-off. ``RedisJobQueue.enqueue``
(``backend/job_queue/redis_arq.py``) puts verified webhook events onto
ARQ's queue; this module is what actually dequeues and does something
with them, run as a separate OS process via ``arq
backend.job_queue.arq_worker.WorkerSettings``.

M3-M9: "does something with them" was deliberately minimal (record that the
job arrived) -- the real multi-agent review workflow did not exist yet.

M10 CLOSES THE GAP DEFERRED SINCE M4: this handler now actually runs a
review through the real orchestrator graph (``backend.orchestrator.
langgraph_engine.LangGraphWorkflowEngine`` -- the same compiled graph
``backend.cli.review_local`` drives), and dispatches the resulting
``Review`` to the (M10: always mocked; M11: settings-driven --
``backend.integrations.github_client.build_github_client``) GitHub client.
This was flagged as a known gap at M4 ("The orchestrator built at M4 is
not yet wired into the webhook/queue path... expected to land in a later
milestone") and reaffirmed unresolved at every milestone since (M5 through
M9's own Deferred lists) -- M10 closed the orchestrator half; M11 closes
the GitHub half (``_get_github_client`` below now returns a real,
GitHub-App-authenticated client whenever ``Settings.github_client_backend
== "real"``, and the same M10 ``MockGitHubClient`` otherwise -- default
mock, so this worker still runs keyless out of the box).

NON-BLOCKING INGRESS, STILL: this module runs as its OWN OS process (a
dedicated ARQ worker, started separately from uvicorn -- see this file's
own module-level ``arq backend.job_queue.arq_worker.WorkerSettings``
invocation), so nothing here runs on the webhook ingress's event loop at
all; ``backend.webhook_receiver.router.receive_webhook`` still only
enqueues and returns, unchanged since M3/M6/M7's fixes for the exact
"blocking call on the shared event loop" defect class this project has now
fixed three times. That said, ARQ's *worker* process itself is also
asyncio-based (so it can run many jobs concurrently within one process),
and the actual review -- LangGraph's ``graph.invoke``, which makes four
real, synchronous, network-bound LLM calls -- is a long, blocking call.
Running it directly inside this ``async def`` handler would block ARQ's
OWN event loop for the whole review's duration, serializing every other
concurrently-queued job behind it -- the identical defect class, just on a
different event loop. ``asyncio.to_thread`` (the same fix already applied
to ``backend.job_queue.interface.enqueue_async``,
``backend.observability.events.emit_decision_async``, and
``backend.tools.llm_client.AnthropicLLMClient.complete_async``) offloads
the blocking orchestrator run onto ARQ's own worker thread pool, so one
slow review does not stall the worker's ability to pick up and start other
jobs concurrently.

GitHub client and workflow engine are both process-wide, lazily-constructed
singletons (``_get_github_client`` / ``_get_workflow_engine``), mirroring
``backend.orchestrator.nodes``'s ``_get_agent`` laziness -- and, like that
module, both have a test-only override hook
(``set_github_client_for_testing`` / ``set_workflow_engine_for_testing``)
so a test can inject a ``MockGitHubClient`` with pre-configured diffs and an
isolated, ``tmp_path``-backed engine without touching a real repository or
the shared on-disk checkpoint database.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from arq.connections import RedisSettings

from backend.core.settings import get_settings
from backend.core.workflow_engine import WorkflowEngine
from backend.integrations.github_client import GitHubClient, build_github_client, post_or_queue
from backend.models import WebhookEvent
from backend.observability.tracing import assert_tracing_healthy
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.state import GraphState

logger = logging.getLogger(__name__)

# A short-lived marker so a test (or a human watching Redis) can observe
# that a specific job actually ran, without needing to parse ARQ's own
# result-keeping. Kept short (1 hour) since it exists only to prove
# "this job was processed just now", not as durable state -- unlike the
# idempotency keys in redis_arq.py, this is not a correctness mechanism.
_PROCESSED_MARKER_PREFIX = "pr-review-agent:processed:"
_PROCESSED_MARKER_TTL_SECONDS = 60 * 60

# ---------------------------------------------------------------------------
# M10: process-wide, lazily-constructed singletons for the GitHub client and
# the orchestrator engine, each with a test-only override -- mirrors
# backend.orchestrator.nodes's _get_agent/_agent_overrides pattern exactly.
# ---------------------------------------------------------------------------

_github_client_override: GitHubClient | None = None
_real_github_client: GitHubClient | None = None
_workflow_engine_override: WorkflowEngine[GraphState] | None = None
_real_workflow_engine: WorkflowEngine[GraphState] | None = None


def set_github_client_for_testing(client: GitHubClient | None) -> None:
    """Test-only: override which ``GitHubClient`` the worker dispatches completed reviews to.

    Production code never calls this. Pass ``None`` to clear a previously
    installed override and fall back to the settings-driven default (mock
    unless ``Settings.github_client_backend == "real"`` -- see
    ``_get_github_client``).
    """
    global _github_client_override
    _github_client_override = client


def set_workflow_engine_for_testing(engine: WorkflowEngine[GraphState] | None) -> None:
    """Test-only: override which orchestrator engine the worker runs reviews through.

    Lets a test inject an engine backed by an isolated ``tmp_path``
    checkpoint database instead of this process's shared default one --
    mirroring why ``tests/integration/test_orchestrator_fanout.py``
    constructs its own ``LangGraphWorkflowEngine`` per test rather than
    sharing one across the whole suite.
    """
    global _workflow_engine_override
    _workflow_engine_override = engine


def _get_github_client() -> GitHubClient:
    """M11: settings-driven -- ``build_github_client`` returns the real client when
    ``Settings.github_client_backend == "real"`` (a configured GitHub App),
    and the same M10 ``MockGitHubClient`` otherwise. Defaults to mock (see
    ``backend.core.settings.Settings.github_client_backend``'s docstring),
    so a keyless checkout of this repo still runs the worker without ever
    attempting a real network call.
    """
    if _github_client_override is not None:
        return _github_client_override
    global _real_github_client
    if _real_github_client is None:
        _real_github_client = build_github_client(get_settings())
    return _real_github_client


def _get_workflow_engine() -> WorkflowEngine[GraphState]:
    if _workflow_engine_override is not None:
        return _workflow_engine_override
    global _real_workflow_engine
    if _real_workflow_engine is None:
        _real_workflow_engine = LangGraphWorkflowEngine()
    return _real_workflow_engine


def _run_review_blocking(
    engine: WorkflowEngine[GraphState], review_id: str, initial_state: GraphState
) -> GraphState:
    """The actual blocking orchestrator run.

    See module docstring for why this is offloaded via
    ``asyncio.to_thread`` rather than called directly from
    ``process_webhook_event``.
    """
    return engine.run(thread_id=review_id, initial_state=initial_state)


async def process_webhook_event(ctx: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """M10: run a real review through the orchestrator for a dequeued webhook event.

    Args:
        ctx: ARQ's per-worker context dict; ``ctx["redis"]`` is the same
            ``ArqRedis`` connection the worker uses internally, provided
            by ARQ so handlers can talk to Redis without opening their own
            connection.
        event: The ``WebhookEvent`` as enqueued, i.e.
            ``WebhookEvent.model_dump(mode="json")`` from
            ``RedisJobQueue.enqueue`` -- a plain JSON-compatible dict
            (ARQ's own serializer, not this project's, encodes/decodes the
            job payload), re-validated back into a real ``WebhookEvent``
            here so this handler has the typed fields it needs to drive
            the orchestrator.

    Returns:
        A small JSON-compatible summary: whether the job was received, its
        ``delivery_id``, and (M10 addition) the resulting review's status
        string, or ``None`` if no ``Review`` was produced (there is no
        currently-known way for that to happen in production -- the
        aggregator always returns one -- but this handler does not assume
        it and reports ``None`` rather than raising ``KeyError`` if it ever
        did).
    """
    delivery_id = event.get("delivery_id", "unknown")
    logger.info("processing webhook job", extra={"delivery_id": delivery_id})

    redis_conn = ctx["redis"]
    await redis_conn.set(
        f"{_PROCESSED_MARKER_PREFIX}{delivery_id}",
        "1",
        ex=_PROCESSED_MARKER_TTL_SECONDS,
    )

    webhook_event = WebhookEvent.model_validate(event)
    review_id = f"webhook-{webhook_event.delivery_id}"
    github_client = _get_github_client()

    # M11: a real GitHub-App-authenticated RealGitHubClient.fetch_diff makes
    # an actual GitHub API call for the PR's real patch when
    # Settings.github_client_backend == "real"; the default MockGitHubClient
    # still returns whatever diff was pre-configured for this PR number (or
    # an empty diff by default) -- see backend.integrations.github_client's
    # module docstring.
    diff = github_client.fetch_diff(
        repository_owner=webhook_event.repository_owner,
        repository_name=webhook_event.repository_name,
        pr_number=webhook_event.pr_number,
        head_sha=webhook_event.head_sha,
    )

    initial_state: GraphState = {
        "review_id": review_id,
        "pr_number": webhook_event.pr_number,
        "repository_owner": webhook_event.repository_owner,
        "repository_name": webhook_event.repository_name,
        "head_sha": webhook_event.head_sha,
        "findings": [],
        "node_errors": {},
        "diff": diff,
    }

    engine = _get_workflow_engine()
    # Offload the blocking, multi-LLM-call orchestrator run -- see module
    # docstring for why this must not run directly on ARQ's own event loop.
    result = await asyncio.to_thread(_run_review_blocking, engine, review_id, initial_state)

    review = result.get("review")
    review_status: str | None = None
    if review is not None:
        # Exactly one of post_review_comment/queue_for_hitl -- see
        # backend.integrations.github_client.post_or_queue's docstring.
        post_or_queue(github_client, review)
        review_status = review.status.value
    else:
        logger.error(
            "no Review produced for review_id=%r -- the aggregator node did "
            "not run or did not write GraphState['review']",
            review_id,
        )

    return {
        "received": True,
        "delivery_id": delivery_id,
        "review_id": review_id,
        "review_status": review_status,
    }


async def _on_worker_startup(ctx: dict[str, Any]) -> None:
    """ARQ ``on_startup`` hook: the real production "at application startup" LangSmith check.

    Of this project's three processes (the webhook FastAPI app, this ARQ
    worker, and the M10 CLI), this worker is the only one that actually
    executes ``graph.invoke`` unattended in production (see
    ``process_webhook_event``/``_run_review_blocking`` above) -- which is
    exactly why ``backend.observability.tracing``'s module docstring
    chose this hook, and not the FastAPI app's lifespan, as the wired-in
    startup check: the webhook app never runs the graph at all, so
    checking tracing health there would verify the wrong process while
    creating a real hazard for the free pytest suite (see that module's
    docstring for the full reasoning).

    A no-op when ``settings.langsmith_tracing`` is ``False`` (the default
    -- ``assert_tracing_healthy`` itself is a no-op in that case). Offloads
    the check via ``asyncio.to_thread`` for the same reason
    ``process_webhook_event`` offloads the orchestrator run itself: this
    is a blocking, network-bound call (a real probe run write + a short
    read-back retry loop), and running it directly on this coroutine
    would block the worker's own event loop for its duration.

    No test in this project drives this function: ``tests/integration/
    test_queue_to_orchestrator.py``/``test_queue_roundtrip.py`` construct
    a bare ``arq.worker.Worker(functions=[process_webhook_event], ...)``
    directly, never referencing ``WorkerSettings`` (and therefore never
    ``on_startup``) at all -- so this hook only ever runs against a real
    ``arq backend.job_queue.arq_worker.WorkerSettings`` process, never
    during ``pytest``.
    """
    settings = get_settings()
    if not settings.langsmith_tracing:
        return
    logger.info("LangSmith tracing enabled -- running startup health check.")
    await asyncio.to_thread(assert_tracing_healthy, settings)


class WorkerSettings:
    """ARQ ``WorkerSettings``: what ``arq backend.job_queue.arq_worker.WorkerSettings`` runs.

    ``functions`` is the set of job functions this worker knows how to
    run. ``redis_settings`` points the worker at the same Redis instance
    ``RedisJobQueue`` enqueues onto (``Settings.redis_url``, read fresh at
    class-definition/import time from the environment, matching how ARQ's
    CLI expects to find this attribute on the class itself rather than an
    instance). ``on_startup`` is the real production wiring for the
    LangSmith silent-failure guard -- see ``_on_worker_startup``.
    """

    functions = [process_webhook_event]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = _on_worker_startup
