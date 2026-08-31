"""M10: the queue -> worker -> orchestrator path, end to end (fake LLM).

Owns closing the gap deferred since M4 and reaffirmed unresolved at every
milestone since ("the orchestrator built at M4 is not yet wired into the
webhook/queue path... expected to land in a later milestone" -- M4's own
Deferred entry; M10 is that later milestone). Proves the FULL real chain
for real, not just each half in isolation (the M5 lesson this project's own
build instructions keep repeating: "the interaction between correctly-
tested halves is where the last blocking bugs lived"):

    RedisJobQueue.enqueue()  (real Redis, M3)
        -> a real ARQ Worker in burst mode dequeues it (M3)
            -> backend.job_queue.arq_worker.process_webhook_event (M10)
                -> backend.orchestrator.langgraph_engine.LangGraphWorkflowEngine
                   runs the real compiled graph (M4/M5/M8, M10's real
                   quality/tests/docs agents)
                    -> backend.integrations.github_client.MockGitHubClient
                       records exactly one post/queue_for_hitl call (M10)

Needs a real, reachable Redis (``docker compose up -d redis``) -- skipped,
not failed, when unreachable, mirroring
``tests/integration/test_queue_roundtrip.py``'s own skip condition exactly.
No ``ANTHROPIC_API_KEY`` anywhere: every specialist agent is overridden
with a fake LLM client via the same test-only hooks
``tests/integration/test_orchestrator_fanout.py`` and
``tests/e2e/test_full_local_review.py`` already use, and the engine/GitHub
client the worker uses are overridden (``set_workflow_engine_for_testing``/
``set_github_client_for_testing``) so this test never touches the shared
production checkpoint database or claims to talk to a real repository.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
import redis as redis_sync
from arq.connections import RedisSettings
from arq.worker import Worker

from backend.agents.docs_agent import DocsAgent
from backend.agents.quality_agent import QualityAgent
from backend.agents.security_agent import SecurityAgent
from backend.agents.test_agent import TestsAgent
from backend.core.settings import get_settings
from backend.integrations.github_client import MockGitHubClient
from backend.job_queue import arq_worker
from backend.job_queue.arq_worker import process_webhook_event
from backend.job_queue.redis_arq import RedisJobQueue
from backend.models import ReviewStatus, WebhookEvent
from backend.orchestrator import nodes
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.tools.llm_client import LLMResponse

_BASE_SETTINGS = get_settings()
_REDIS_URL = _BASE_SETTINGS.redis_url
_BURST_POLL_DELAY_SECONDS = 0.1

def _findings_response(file_path: str, category: str) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "severity": "MEDIUM",
                    "category": category,
                    "file_path": file_path,
                    "line_start": 1,
                    "line_end": 1,
                    "confidence": "0.900",
                    "rationale": f"a fake {category} finding for the queue-to-orchestrator integration test",
                }
            ]
        }
    )


# A distinct file_path/category per agent, so the four fake findings land
# at four different (file_path, line_start) keys and none collide in the
# real aggregator's dedupe step -- mirroring
# tests/e2e/test_full_local_review.py's identical fix for the identical bug.
_RESPONSES_BY_AGENT = {
    "security": _findings_response("app/x_security.py", "sql_injection"),
    "quality": _findings_response("app/x_quality.py", "excessive_complexity"),
    "tests": _findings_response("app/x_tests.py", "missing_test_coverage"),
    "docs": _findings_response("app/x_docs.py", "stale_docstring"),
}


def _redis_reachable(redis_url: str) -> bool:
    try:
        client = redis_sync.Redis.from_url(redis_url, socket_connect_timeout=1)
        return bool(client.ping())
    except (redis_sync.RedisError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _redis_reachable(_REDIS_URL),
    reason=f"Redis not reachable at {_REDIS_URL} -- run `docker compose up -d redis` first",
)


class _FakeLLMClient:
    def complete(
        self,
        *,
        system: str,
        user: str,
        agent: str,
        review_id: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            text=_RESPONSES_BY_AGENT[agent],
            model="fake-model",
            tokens_in=10,
            tokens_out=10,
            cost_usd=Decimal("0.000100"),
            latency_ms=1,
        )


def _make_event(delivery_id: str | None = None) -> WebhookEvent:
    return WebhookEvent(
        action="opened",
        pr_number=1,
        repository_owner="acme",
        repository_name="widgets",
        head_sha="a" * 40,
        delivery_id=delivery_id or str(uuid.uuid4()),
        received_at="2026-08-31T00:00:00+00:00",
    )


@pytest.fixture
def redis_queue() -> Iterator[RedisJobQueue]:
    queue = RedisJobQueue(_BASE_SETTINGS)
    try:
        yield queue
    finally:
        queue.close()


@pytest.fixture
def isolated_worker_dependencies(tmp_path: Path) -> Iterator[MockGitHubClient]:
    """Install fakes for everything the worker touches: 4 agents, engine, GitHub client.

    Yields the ``MockGitHubClient`` so a test can inspect exactly which
    call it recorded.
    """
    fake_llm_client = _FakeLLMClient()
    nodes.set_security_agent_for_testing(SecurityAgent(fake_llm_client))
    nodes.set_quality_agent_for_testing(QualityAgent(fake_llm_client))
    nodes.set_tests_agent_for_testing(TestsAgent(fake_llm_client))
    nodes.set_docs_agent_for_testing(DocsAgent(fake_llm_client))

    github_client = MockGitHubClient(default_diff="diff --git a/x b/x\n+def f():\n+    pass\n")
    arq_worker.set_github_client_for_testing(github_client)

    engine = LangGraphWorkflowEngine(tmp_path / "checkpoints.sqlite3")
    arq_worker.set_workflow_engine_for_testing(engine)

    try:
        yield github_client
    finally:
        engine.close()
        arq_worker.set_workflow_engine_for_testing(None)
        arq_worker.set_github_client_for_testing(None)
        nodes.set_security_agent_for_testing(None)
        nodes.set_quality_agent_for_testing(None)
        nodes.set_tests_agent_for_testing(None)
        nodes.set_docs_agent_for_testing(None)


async def _run_burst_worker() -> None:
    """Run a real ARQ worker in burst mode: process what's queued, then exit."""
    worker = Worker(
        functions=[process_webhook_event],
        redis_settings=RedisSettings.from_dsn(_REDIS_URL),
        burst=True,
        poll_delay=_BURST_POLL_DELAY_SECONDS,
    )
    await worker.async_run()
    await worker.close()


class TestQueueWorkerOrchestratorEndToEnd:
    """A real webhook event, enqueued via real Redis, produces a real Review.

    ANOMALY discovered while writing this test, worth recording here
    directly rather than only in this session's final report: ARQ's burst
    mode drains and processes EVERY job currently sitting in the shared,
    persistent Redis queue, not merely the one a given test just enqueued.
    ``tests/integration/test_queue_roundtrip.py``'s own pre-existing
    ``TestIdempotency``/``TestIdempotencyTtl`` classes (M3/M6, unrelated to
    and unmodified by M10) enqueue jobs via the real ``RedisJobQueue`` but
    never run a burst worker to drain them -- harmless before M10 (nothing
    observable happened per job beyond a Redis marker anyone could ignore),
    but newly significant now that every processed job produces one
    ``GitHubClient`` call this test can observe. A ``pytest`` run that
    executes those tests before this file (alphabetical file order does
    exactly that: ``test_queue_roundtrip.py`` < ``test_queue_to_
    orchestrator.py``) leaves their jobs sitting in the real queue, and
    this file's own burst-worker call would otherwise also process THOSE
    leftover jobs alongside its own -- inflating an exact
    ``total_calls() == 1`` count. Fixed here, not by touching M3's
    unrelated, out-of-freeze-boundary test file: each test below drains
    the queue once, with this file's own fakes already installed (so
    draining costs nothing real), before enqueueing and measuring its OWN
    event -- and asserts on ITS OWN event's review specifically (found by
    ``review_id``), not a brittle global call count.
    """

    async def test_enqueued_event_is_processed_into_a_review(
        self,
        redis_queue: RedisJobQueue,
        isolated_worker_dependencies: MockGitHubClient,
    ) -> None:
        github_client = isolated_worker_dependencies

        # Drain any pre-existing backlog left by unrelated earlier tests
        # (see class docstring's ANOMALY note) before this test's own
        # event is enqueued, so the assertions below are about THIS
        # event's own processing, not contaminated by leftovers.
        await _run_burst_worker()
        github_client.posted_reviews.clear()
        github_client.queued_reviews.clear()

        event = _make_event()
        result = redis_queue.enqueue(event)
        assert result.enqueued is True

        await _run_burst_worker()

        # The worker actually ran the orchestrator and dispatched the
        # resulting Review to the (fake) GitHub client -- exactly one call,
        # never both, same as the CLI's own success criterion.
        assert github_client.total_calls() == 1
        assert not (github_client.posted_reviews and github_client.queued_reviews)

        review = (github_client.posted_reviews + github_client.queued_reviews)[0]
        assert review.review_id == f"webhook-{event.delivery_id}"
        assert len(review.findings) == 4  # one real finding per specialist
        assert review.status in (ReviewStatus.POSTED, ReviewStatus.QUEUED_FOR_HITL)

    async def test_the_processed_marker_is_still_recorded(
        self,
        redis_queue: RedisJobQueue,
        isolated_worker_dependencies: MockGitHubClient,
    ) -> None:
        """The pre-existing M3 behavior (a marker proving the job ran) is unchanged by M10."""
        event = _make_event()
        redis_queue.enqueue(event)

        await _run_burst_worker()

        marker = redis_sync.Redis.from_url(_REDIS_URL).get(
            f"pr-review-agent:processed:{event.delivery_id}"
        )
        assert marker == b"1"
