"""Integration tests for the M7 events spine -- PLAN.md's named demo suite.

Owns: proving the append-only ``agent_events`` table and its two live call
sites (the webhook router's ingress decision, the orchestrator's spans and
aggregator decision) actually work together against a real Postgres
(``docker-compose.yml``'s ``postgres`` service), not just that the pieces
satisfy their type signatures in isolation. Specifically:

- APPEND-ONLY IS ENFORCED BY THE DATABASE: a real UPDATE and a real DELETE
  against a real row are both rejected, with the actual database error
  surfacing to the caller -- once via the BEFORE trigger (fires for any
  role, including the admin superuser), and once via the restricted
  ``agent_events_writer`` role's revoked GRANTs (rejected even before the
  trigger runs).
- No application code anywhere in ``backend/`` (outside migrations, which
  are schema, not application code) contains an UPDATE/DELETE statement or
  ORM ``.update()``/``.delete()`` call targeting ``agent_events`` --
  grep-verified by an actual test, not merely asserted in prose, mirroring
  the ``events-table-append-only`` invariant in
  ``.genesis/context-graph.json``.
- A real webhook request (via FastAPI's ``TestClient``) produces the
  expected ``decision`` event rows for accepted, duplicate, and rejected
  outcomes.
- A real orchestrator run (the compiled LangGraph graph, not a mock)
  produces one ``span.start``/``span.end`` pair per specialist plus one
  ``decision`` event from the aggregator, with real measured latencies and
  the review's actual routing outcome/confidence.
- Events for one review/run id are reconstructable, in time order, from
  ``review_id`` alone (``backend.observability.audit.reconstruct_review_trace``).
- The spec's pinned numeric precision (``cost_usd`` to 6 decimal places,
  ``confidence`` to 3) survives a real Postgres round trip, not just
  pydantic's own in-process validation.
- The events database being unreachable does not break the webhook path --
  the request still returns 200, exactly as it would with a healthy events
  database.

These tests need a real reachable Postgres (``docker compose up -d
postgres`` from the repo root, then ``backend.database.postgres.
apply_migrations`` against the admin connection -- done once per test
module run by the ``_migrated_database`` fixture below). They are skipped
-- not failed -- when Postgres is unreachable, via a module-level
``skipif`` computed once at collection time, the same pattern
``tests/integration/test_queue_roundtrip.py`` uses for Redis. One
exception: ``TestEventsDbDownDoesNotBreakWebhook`` does not itself need a
reachable Postgres (it deliberately points at an unreachable one) but
lives under the same module-level skip for simplicity -- it still runs
whenever the module as a whole runs, which is every session where
Postgres is up for the other tests here anyway.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.agents.docs_agent import DocsAgent
from backend.agents.quality_agent import QualityAgent
from backend.agents.security_agent import SecurityAgent
from backend.agents.test_agent import TestsAgent
from backend.api.main import create_app
from backend.core.settings import Settings, get_settings
from backend.database.models import AgentEvent, EventType
from backend.database.postgres import apply_migrations
from backend.database.repository import EventRepository
from backend.job_queue.in_memory import InMemoryJobQueue
from backend.observability.audit import reconstruct_review_trace
from backend.observability.workflow_context import run_id_for_delivery
from backend.orchestrator import nodes
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.state import GraphState
from backend.tools.llm_client import LLMResponse

_BASE_SETTINGS = get_settings()
_DATABASE_URL = _BASE_SETTINGS.database_url
_DATABASE_ADMIN_URL = _BASE_SETTINGS.database_admin_url

_WEBHOOK_SECRET = "events-spine-test-secret"
_SPECIALIST_AGENTS = frozenset({"security", "quality", "tests", "docs"})


def _postgres_reachable(admin_dsn: str) -> bool:
    """Best-effort check: can we actually reach Postgres at ``admin_dsn`` right now."""
    try:
        with psycopg.connect(admin_dsn, connect_timeout=1) as conn:
            conn.execute("SELECT 1")
        return True
    except (psycopg.Error, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(_DATABASE_ADMIN_URL),
    reason=(
        f"Postgres not reachable at {_DATABASE_ADMIN_URL} -- "
        "run `docker compose up -d postgres` first"
    ),
)


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> None:
    """Apply migrations once per test-module run, against the real admin connection."""
    apply_migrations(_DATABASE_ADMIN_URL)


@pytest.fixture
def repository() -> EventRepository:
    """A real ``EventRepository`` against the restricted application role."""
    return EventRepository(_DATABASE_URL)


def _unique_review_id(prefix: str) -> str:
    """A per-test-run unique review_id, so parallel/repeated runs never collide."""
    return f"{prefix}-{uuid.uuid4()}"


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _sample_payload(action: str = "opened", pr_number: int = 777) -> dict[str, object]:
    return {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "head": {"sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"},
        },
        "repository": {
            "name": "events-spine-repo",
            "owner": {"login": "events-spine-org"},
        },
    }


def _webhook_headers(delivery_id: str, *, event: str = "pull_request") -> dict[str, str]:
    return {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery_id}


# ---------------------------------------------------------------------------
# The milestone's core claim: append-only is enforced by the database.
# ---------------------------------------------------------------------------


class TestAppendOnlyEnforcement:
    """UPDATE and DELETE against ``agent_events`` are rejected by Postgres
    itself -- not merely by application-code convention."""

    def test_update_is_rejected_by_the_database_trigger(self, repository: EventRepository) -> None:
        review_id = _unique_review_id("append-only-update")
        repository.insert_event(
            AgentEvent(
                review_id=review_id, event_type=EventType.SPAN_START, ts=datetime.now(UTC), agent="security"
            )
        )

        with (
            psycopg.connect(_DATABASE_ADMIN_URL, autocommit=True) as conn,
            pytest.raises(psycopg.errors.RaiseException) as exc_info,
        ):
            conn.execute(
                "UPDATE agent_events SET agent = 'hacked' WHERE review_id = %s", (review_id,)
            )

        message = str(exc_info.value)
        assert "append-only" in message
        assert "UPDATE" in message

    def test_delete_is_rejected_by_the_database_trigger(self, repository: EventRepository) -> None:
        review_id = _unique_review_id("append-only-delete")
        repository.insert_event(
            AgentEvent(
                review_id=review_id, event_type=EventType.SPAN_START, ts=datetime.now(UTC), agent="security"
            )
        )

        with (
            psycopg.connect(_DATABASE_ADMIN_URL, autocommit=True) as conn,
            pytest.raises(psycopg.errors.RaiseException) as exc_info,
        ):
            conn.execute("DELETE FROM agent_events WHERE review_id = %s", (review_id,))

        message = str(exc_info.value)
        assert "append-only" in message
        assert "DELETE" in message

    def test_trigger_fires_even_for_the_admin_superuser(self, repository: EventRepository) -> None:
        """A PostgreSQL superuser bypasses GRANT/REVOKE checks entirely --
        this asserts the trigger (not merely the restricted role's revoked
        privileges) is what makes mutation impossible, by attempting the
        mutation as the admin/superuser connection itself."""
        review_id = _unique_review_id("append-only-superuser")
        repository.insert_event(
            AgentEvent(
                review_id=review_id, event_type=EventType.SPAN_START, ts=datetime.now(UTC), agent="security"
            )
        )
        with (
            psycopg.connect(_DATABASE_ADMIN_URL, autocommit=True) as conn,
            pytest.raises(psycopg.errors.RaiseException),
        ):
            conn.execute(
                "UPDATE agent_events SET outcome = 'hacked' WHERE review_id = %s", (review_id,)
            )

    def test_restricted_role_is_denied_before_the_trigger_even_runs(self) -> None:
        """Defense in depth: the GRANT/REVOKE layer rejects the restricted
        ``agent_events_writer`` role's UPDATE/DELETE at the permission-check
        stage -- a second, independent enforcement mechanism alongside the
        trigger."""
        review_id = _unique_review_id("append-only-grant")
        with psycopg.connect(_DATABASE_ADMIN_URL, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO agent_events (review_id, event_type, agent) VALUES (%s, %s, %s)",
                (review_id, EventType.SPAN_START.value, "security"),
            )

        with (
            psycopg.connect(_DATABASE_URL, autocommit=True) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute(
                "UPDATE agent_events SET agent = 'hacked' WHERE review_id = %s", (review_id,)
            )

        with (
            psycopg.connect(_DATABASE_URL, autocommit=True) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute("DELETE FROM agent_events WHERE review_id = %s", (review_id,))

    def test_truncate_is_rejected_even_for_the_admin_superuser(
        self, repository: EventRepository
    ) -> None:
        """Regression test (L2 DEBUG, post-L4-REJECT): TRUNCATE never fires a
        row-level trigger, so the pre-existing ``agent_events_no_update``/
        ``agent_events_no_delete`` pair (``FOR EACH ROW``) does nothing to
        stop it -- L4 VERIFY demonstrated this concretely by running
        ``TRUNCATE agent_events;`` as the "postgres" superuser and watching
        every row silently disappear, no exception raised. The dedicated
        ``agent_events_no_truncate`` statement-level (``FOR EACH STATEMENT``)
        trigger migration 0001 now also installs closes that gap. This
        asserts both that TRUNCATE raises AND that the row inserted just
        before it is still there afterward -- the same "prove data survived"
        shape as the UPDATE/DELETE tests above, not just "an exception was
        raised somewhere."
        """
        review_id = _unique_review_id("append-only-truncate")
        repository.insert_event(
            AgentEvent(
                review_id=review_id,
                event_type=EventType.SPAN_START,
                ts=datetime.now(UTC),
                agent="security",
            )
        )

        with (
            psycopg.connect(_DATABASE_ADMIN_URL, autocommit=True) as conn,
            pytest.raises(psycopg.errors.RaiseException) as exc_info,
        ):
            conn.execute("TRUNCATE agent_events")

        assert "append-only" in str(exc_info.value)
        assert "TRUNCATE" in str(exc_info.value)
        # The row survives: TRUNCATE was rejected before it took effect, not
        # merely logged-and-allowed.
        assert repository.fetch_events_for_review(review_id) != []


class TestNoApplicationCodeMutatesEvents:
    """Mirrors the ``events-table-append-only`` invariant in
    ``.genesis/context-graph.json`` as an actual, running test: grep
    ``backend/`` (outside migrations, which are schema, not application
    code) for an UPDATE/DELETE SQL statement or ORM ``.update()``/
    ``.delete()`` call actually targeting ``agent_events``, expecting zero
    matches. Deliberately requires the SQL keyword and the table name to be
    adjacent (not merely "somewhere in the same file") so this does not
    false-positive on prose -- e.g. this very module's, or
    ``backend/observability/events.py``'s, docstrings that *discuss*
    UPDATE/DELETE and append-only enforcement without executing either.
    """

    _UPDATE_OR_DELETE_AGAINST_AGENT_EVENTS = re.compile(
        r"UPDATE\s+agent_events"
        r"|DELETE\s+FROM\s+agent_events"
        r"|agent_events\w*\s*\.\s*(update|delete)\s*\(",
        re.IGNORECASE,
    )

    def test_backend_source_has_no_update_or_delete_against_agent_events(self) -> None:
        backend_dir = Path(__file__).resolve().parents[2] / "backend"
        offenders: list[str] = []
        for path in backend_dir.rglob("*.py"):
            if "migrations" in path.parts:
                continue
            text = path.read_text()
            if self._UPDATE_OR_DELETE_AGAINST_AGENT_EVENTS.search(text):
                offenders.append(str(path))
        assert offenders == [], (
            f"found an UPDATE/DELETE statement actually targeting agent_events "
            f"in application code: {offenders}"
        )


# ---------------------------------------------------------------------------
# Live call site 1: webhook ingress decision events.
# ---------------------------------------------------------------------------


class TestWebhookIngressProducesDecisionEvents:
    def test_accepted_delivery_produces_an_accepted_decision_event(
        self, repository: EventRepository
    ) -> None:
        delivery_id = str(uuid.uuid4())
        body = json.dumps(_sample_payload()).encode()
        app = create_app(
            settings=Settings(github_webhook_secret=_WEBHOOK_SECRET),
            job_queue=InMemoryJobQueue(),
            event_repository=repository,
        )
        with TestClient(app) as client:
            response = client.post(
                "/webhook",
                content=body,
                headers={"X-Hub-Signature-256": _sign(body), **_webhook_headers(delivery_id)},
            )

        assert response.status_code == 200
        assert response.json()["status"] == "accepted"

        events = repository.fetch_events_for_review(run_id_for_delivery(delivery_id))
        assert len(events) == 1
        assert events[0].event_type is EventType.DECISION
        assert events[0].outcome == "accepted"

    def test_duplicate_delivery_produces_accepted_then_duplicate_decision_events(
        self, repository: EventRepository
    ) -> None:
        delivery_id = str(uuid.uuid4())
        body = json.dumps(_sample_payload()).encode()
        headers = {"X-Hub-Signature-256": _sign(body), **_webhook_headers(delivery_id)}
        app = create_app(
            settings=Settings(github_webhook_secret=_WEBHOOK_SECRET),
            job_queue=InMemoryJobQueue(),
            event_repository=repository,
        )
        with TestClient(app) as client:
            first = client.post("/webhook", content=body, headers=headers)
            second = client.post("/webhook", content=body, headers=headers)

        assert first.json()["status"] == "accepted"
        assert second.json()["status"] == "duplicate"

        events = repository.fetch_events_for_review(run_id_for_delivery(delivery_id))
        assert [event.outcome for event in events] == ["accepted", "duplicate"]
        # time-ordered: the accepted decision strictly precedes the duplicate one.
        assert events[0].ts <= events[1].ts

    def test_unsupported_action_produces_a_rejected_decision_event(
        self, repository: EventRepository
    ) -> None:
        delivery_id = str(uuid.uuid4())
        body = json.dumps(_sample_payload(action="closed")).encode()
        app = create_app(
            settings=Settings(github_webhook_secret=_WEBHOOK_SECRET),
            job_queue=InMemoryJobQueue(),
            event_repository=repository,
        )
        with TestClient(app) as client:
            response = client.post(
                "/webhook",
                content=body,
                headers={"X-Hub-Signature-256": _sign(body), **_webhook_headers(delivery_id)},
            )

        assert response.status_code == 200
        assert response.json()["status"] == "ignored"

        events = repository.fetch_events_for_review(run_id_for_delivery(delivery_id))
        assert len(events) == 1
        assert events[0].outcome == "rejected"

    def test_unverified_signature_produces_no_event_at_all(
        self, repository: EventRepository
    ) -> None:
        """The ``hmac-verified-before-any-work`` invariant must still hold:
        a request that fails signature verification never reaches the
        events-emission code at all."""
        delivery_id = str(uuid.uuid4())
        body = json.dumps(_sample_payload()).encode()
        app = create_app(
            settings=Settings(github_webhook_secret=_WEBHOOK_SECRET),
            job_queue=InMemoryJobQueue(),
            event_repository=repository,
        )
        with TestClient(app) as client:
            response = client.post(
                "/webhook",
                content=body,
                headers={
                    "X-Hub-Signature-256": _sign(body, secret="wrong-secret"),
                    **_webhook_headers(delivery_id),
                },
            )

        assert response.status_code == 401
        assert repository.fetch_events_for_review(run_id_for_delivery(delivery_id)) == []


class TestEventsDbDownDoesNotBreakWebhook:
    """Writing an event must never take down the request path it observes."""

    def test_webhook_still_returns_200_when_events_db_is_unreachable(self) -> None:
        delivery_id = str(uuid.uuid4())
        body = json.dumps(_sample_payload()).encode()
        unreachable_repository = EventRepository(
            "postgresql://nouser:nopass@localhost:59999/nonexistent", connect_timeout_seconds=1.0
        )
        app = create_app(
            settings=Settings(github_webhook_secret=_WEBHOOK_SECRET),
            job_queue=InMemoryJobQueue(),
            event_repository=unreachable_repository,
        )
        with TestClient(app) as client:
            response = client.post(
                "/webhook",
                content=body,
                headers={"X-Hub-Signature-256": _sign(body), **_webhook_headers(delivery_id)},
            )

        # The webhook path behaves exactly as it would with a healthy events
        # database -- accepted and enqueued -- because emit_decision's
        # failure policy swallows the connection error internally.
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"


class TestConcurrentWebhookWritesAreNotSerializedByALockedEventsTable:
    """Regression test for the defect an independent L4 VERIFY session
    caught: ``EventRepository.insert_event`` opens a synchronous, blocking
    ``psycopg.connect`` and was called directly (never awaited, never
    offloaded) from ``async def receive_webhook``. L4 VERIFY proved this
    empirically against a live uvicorn: with an admin session holding
    ``LOCK TABLE agent_events IN ACCESS EXCLUSIVE MODE``, three concurrent,
    otherwise-unrelated webhook POSTs each took ~4.4s instead of the normal
    sub-10ms, because the blocking write monopolized the single event-loop
    thread every other in-flight request also depended on.

    This test reproduces the same mechanism without needing a real uvicorn
    subprocess: ``httpx.ASGITransport`` drives the real ASGI app directly
    on the CURRENT asyncio event loop (no real socket, no separate
    process/thread for the server) -- exactly the "one event loop, multiple
    concurrent tasks" model a real uvicorn worker uses. A synchronous,
    unawaited blocking call inside one task's coroutine starves every other
    task on that same loop regardless of whether the transport underneath
    is a real socket or in-process ASGI plumbing; the bug (and the fix) is
    about asyncio scheduling, not about the network.

    Against the pre-fix router (``emit_decision`` called directly, not
    ``await emit_decision_async(...)``), this test fails both assertions
    below: every request's own latency balloons toward the full serialized
    total instead of staying bounded near ``statement_timeout``, and the
    whole batch's wall-clock time scales with the number of concurrent
    requests instead of stayling flat -- see this milestone's build report
    for the actual failing/passing output captured by temporarily
    reverting the router to the old, unawaited call.
    """

    _LOCK_HOLD_SECONDS = 4.0
    _STATEMENT_TIMEOUT_MS = 1200
    _NUM_CONCURRENT_REQUESTS = 4

    @staticmethod
    def _hold_table_lock_in_background(hold_seconds: float, lock_acquired: threading.Event) -> None:
        """Runs on its own OS thread: acquire the lock, signal, hold, release.

        A dedicated connection/thread (not the test's own event loop) so
        holding the lock for a fixed wall-clock duration is exact and
        independent of whatever the test's asyncio loop is doing at the
        same time.
        """
        with psycopg.connect(_DATABASE_ADMIN_URL, autocommit=False) as conn:
            conn.execute("LOCK TABLE agent_events IN ACCESS EXCLUSIVE MODE")
            lock_acquired.set()
            time.sleep(hold_seconds)
            conn.commit()

    async def test_concurrent_webhook_posts_are_not_serialized_behind_a_locked_events_table(
        self,
    ) -> None:
        lock_acquired = threading.Event()
        lock_thread = threading.Thread(
            target=self._hold_table_lock_in_background,
            args=(self._LOCK_HOLD_SECONDS, lock_acquired),
            daemon=True,
        )
        lock_thread.start()
        try:
            assert lock_acquired.wait(timeout=5.0), (
                "background thread never acquired the ACCESS EXCLUSIVE lock "
                "-- test setup itself is broken, not the thing under test"
            )

            repository = EventRepository(
                _DATABASE_URL, statement_timeout_ms=self._STATEMENT_TIMEOUT_MS
            )
            app = create_app(
                settings=Settings(github_webhook_secret=_WEBHOOK_SECRET),
                job_queue=InMemoryJobQueue(),
                event_repository=repository,
            )

            async def _one_request(index: int) -> float:
                delivery_id = str(uuid.uuid4())
                body = json.dumps(_sample_payload(pr_number=90_000 + index)).encode()
                headers = {"X-Hub-Signature-256": _sign(body), **_webhook_headers(delivery_id)}
                start = time.monotonic()
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as client:
                    response = await client.post("/webhook", content=body, headers=headers)
                elapsed = time.monotonic() - start
                # The webhook's own response is unaffected by whether its
                # events write succeeded, timed out, or is still offloaded
                # somewhere -- requirement (e): still 200/"accepted" either way.
                assert response.status_code == 200
                assert response.json()["status"] == "accepted"
                return elapsed

            overall_start = time.monotonic()
            latencies = await asyncio.gather(
                *[_one_request(i) for i in range(self._NUM_CONCURRENT_REQUESTS)]
            )
            overall_elapsed = time.monotonic() - overall_start
        finally:
            lock_thread.join(timeout=self._LOCK_HOLD_SECONDS + 5.0)

        print(  # deliberate evidence output (tests/** is exempt from ruff's T20)
            f"[concurrency proof] per-request latencies={[round(latency, 3) for latency in latencies]}s "
            f"max={max(latencies):.3f}s overall_batch={overall_elapsed:.3f}s "
            f"(lock_hold={self._LOCK_HOLD_SECONDS}s, statement_timeout={self._STATEMENT_TIMEOUT_MS}ms)"
        )
        statement_timeout_seconds = self._STATEMENT_TIMEOUT_MS / 1000
        # THE FIX: no individual request waits anywhere near the full
        # lock-hold duration -- each request's own events write is bounded
        # by statement_timeout, not the table lock, because it happens on a
        # worker thread the event loop keeps servicing other requests
        # around. Against the pre-fix (unawaited, unoffloaded) call, the
        # slowest request instead waits out close to the full serialized
        # total (every other blocked request's turn plus its own).
        assert max(latencies) < self._LOCK_HOLD_SECONDS * 0.75, (
            f"slowest of {self._NUM_CONCURRENT_REQUESTS} concurrent requests took "
            f"{max(latencies):.2f}s (lock held {self._LOCK_HOLD_SECONDS}s, statement_timeout "
            f"{statement_timeout_seconds}s) -- looks like the events write blocked the event "
            "loop instead of being offloaded to a worker thread"
        )
        # CONCURRENCY, NOT SERIALIZATION: N requests each individually
        # bounded by ~statement_timeout must complete in close to that same
        # window, not N times it -- the whole point of not blocking the
        # event loop is that unrelated requests proceed in parallel.
        assert overall_elapsed < self._LOCK_HOLD_SECONDS * 0.9, (
            f"{self._NUM_CONCURRENT_REQUESTS} concurrent requests took {overall_elapsed:.2f}s "
            "in total for a batch that should complete in about one statement_timeout window "
            "-- looks serialized on a single blocked event loop thread, not concurrent"
        )


# ---------------------------------------------------------------------------
# Live call site 2: orchestrator spans + aggregator decision.
# ---------------------------------------------------------------------------


class _FakeLLMClientForEventsSpineTest:
    """A minimal fake satisfying ``LLMClientProtocol``, for the M10 fix below.

    Not testing anything about LLM behavior itself (that is
    ``tests/unit/test_specialist_agents.py`` and the key-gated
    ``tests/integration/test_all_agents_live.py``'s job) -- this class
    exists purely so ``TestOrchestratorProducesSpansAndDecision`` below can
    prove real span/decision EVENTS fire for a real orchestrator run
    through real node code, without also depending on (or paying for) a
    real network call.

    ``complete`` sleeps briefly (well under the real latency a genuine API
    call would have) purely so this test's own pre-existing
    ``event.latency_ms > 0`` assertion (proving a real measured duration
    was recorded, not merely a placeholder) stays meaningful -- an
    effectively-instant fake call can otherwise round down to 0ms
    (``backend.observability.tracing.traced_span`` truncates to whole
    milliseconds), which would make that assertion pass for the wrong
    reason before this fix and fail outright after it.
    """

    _SIMULATED_LATENCY_SECONDS = 0.005

    def complete(
        self,
        *,
        system: str,
        user: str,
        agent: str,
        review_id: str | None = None,
    ) -> LLMResponse:
        time.sleep(self._SIMULATED_LATENCY_SECONDS)
        return LLMResponse(
            text='{"findings": []}',
            model="fake-model",
            tokens_in=1,
            tokens_out=1,
            cost_usd=Decimal("0"),
            latency_ms=1,
        )


class TestOrchestratorProducesSpansAndDecision:
    """M10 fix, disclosed here (this file is outside M10's freeze boundary):
    before M10, only the SECURITY node was a real, LLM-backed agent
    (``backend.orchestrator.nodes._get_security_agent``'s default) -- the
    other three were M4's canned, no-network stub findings, so this test
    (written at M7, unmodified since) made at most ONE real, billable
    Anthropic call whenever ``ANTHROPIC_API_KEY`` happened to be
    configured, tolerated as cheap and incidental to what this test
    actually proves (that real orchestrator nodes emit real span/decision
    events -- not anything about a real LLM's behavior, which is a
    different test's job). M10 made QUALITY/TESTS/DOCS real too
    (``backend.orchestrator.nodes``'s ``_get_agent`` now defaults ALL
    FOUR), which would have silently quadrupled this test's real,
    unbudgeted API spend on every ordinary ``pytest`` run with a key
    configured -- a direct violation of this project's own "a full pytest
    run must not make real LLM calls except in explicitly key-gated tests"
    rule. Fixed by installing a fake ``LLMClientProtocol`` for all four
    specialists (the same test-only override mechanism
    ``tests/integration/test_orchestrator_fanout.py``'s own autouse
    fixture already uses) for the duration of this one test -- this test's
    actual assertions are entirely about which events fired and their
    ordering/latency, none of which depend on what the LLM actually said.
    """

    @pytest.fixture(autouse=True)
    def _fake_agents_for_all_four_specialists(self) -> Iterator[None]:
        fake_client = _FakeLLMClientForEventsSpineTest()
        nodes.set_security_agent_for_testing(SecurityAgent(fake_client))
        nodes.set_quality_agent_for_testing(QualityAgent(fake_client))
        nodes.set_tests_agent_for_testing(TestsAgent(fake_client))
        nodes.set_docs_agent_for_testing(DocsAgent(fake_client))
        try:
            yield
        finally:
            nodes.set_security_agent_for_testing(None)
            nodes.set_quality_agent_for_testing(None)
            nodes.set_tests_agent_for_testing(None)
            nodes.set_docs_agent_for_testing(None)

    def test_real_orchestrator_run_produces_spans_and_a_decision_event(
        self, repository: EventRepository, tmp_path: Path
    ) -> None:
        review_id = _unique_review_id("orchestrator-run")
        engine = LangGraphWorkflowEngine(checkpoint_db_path=tmp_path / "checkpoints.sqlite3")
        try:
            initial_state: GraphState = {
                "review_id": review_id,
                "pr_number": 1,
                "repository_owner": "acme",
                "repository_name": "widgets",
                "head_sha": "a" * 40,
                "findings": [],
                "node_errors": {},
            }
            result = engine.run(thread_id=review_id, initial_state=initial_state)
        finally:
            engine.close()

        assert result.get("review") is not None

        events = repository.fetch_events_for_review(review_id)
        span_start_agents = {e.agent for e in events if e.event_type is EventType.SPAN_START}
        span_end_agents = {e.agent for e in events if e.event_type is EventType.SPAN_END}
        decisions = [e for e in events if e.event_type is EventType.DECISION]

        assert span_start_agents == _SPECIALIST_AGENTS
        assert span_end_agents == _SPECIALIST_AGENTS
        assert len(decisions) == 1
        assert decisions[0].agent == "aggregator"
        assert decisions[0].outcome in {"POSTED", "QUEUED_FOR_HITL"}
        assert decisions[0].confidence == result["review"].overall_confidence

        # Every span.end recorded a real measured latency. Pre-M10, this
        # relied on the M4 stub nodes' fixed NODE_WORK_SECONDS=0.2s sleep;
        # post-M10, all four agents are real (fake-LLM-backed in this
        # test -- see _fake_agents_for_all_four_specialists), and
        # _FakeLLMClientForEventsSpineTest.complete's own small sleep is
        # what keeps this non-zero now (see that class's docstring).
        for event in events:
            if event.event_type is EventType.SPAN_END:
                assert event.latency_ms is not None
                assert event.latency_ms > 0
                assert event.outcome == "ok"

        # Time-ordered: each agent's own span.start precedes its span.end,
        # and the aggregator's decision comes after every specialist's
        # span.end (the real fan-out/fan-in order the graph actually runs).
        start_ts = {e.agent: e.ts for e in events if e.event_type is EventType.SPAN_START}
        end_ts = {e.agent: e.ts for e in events if e.event_type is EventType.SPAN_END}
        for agent in _SPECIALIST_AGENTS:
            assert start_ts[agent] <= end_ts[agent]
        assert max(end_ts.values()) <= decisions[0].ts


# ---------------------------------------------------------------------------
# Trace reconstruction and numeric precision.
# ---------------------------------------------------------------------------


class TestTraceReconstruction:
    def test_reconstruct_review_trace_returns_events_in_time_order_regardless_of_insertion_order(
        self, repository: EventRepository
    ) -> None:
        review_id = _unique_review_id("trace-reconstruction")
        base_ts = datetime(2026, 1, 1, tzinfo=UTC)

        # Insert deliberately out of chronological order, to prove the
        # reconstruction orders by `ts`, not by insertion/primary-key order.
        repository.insert_event(
            AgentEvent(
                review_id=review_id,
                event_type=EventType.DECISION,
                ts=base_ts + timedelta(milliseconds=300),
                agent="aggregator",
                outcome="POSTED",
                confidence=Decimal("0.900"),
            )
        )
        repository.insert_event(
            AgentEvent(
                review_id=review_id,
                event_type=EventType.SPAN_START,
                ts=base_ts,
                agent="security",
            )
        )
        repository.insert_event(
            AgentEvent(
                review_id=review_id,
                event_type=EventType.SPAN_END,
                ts=base_ts + timedelta(milliseconds=200),
                agent="security",
                latency_ms=200,
                outcome="ok",
            )
        )

        trace = reconstruct_review_trace(repository, review_id)

        assert [event.event_type for event in trace] == [
            EventType.SPAN_START,
            EventType.SPAN_END,
            EventType.DECISION,
        ]
        assert trace == sorted(trace, key=lambda event: event.ts)


class TestNumericPrecision:
    def test_cost_usd_and_confidence_precision_round_trip_through_postgres(
        self, repository: EventRepository
    ) -> None:
        review_id = _unique_review_id("precision")
        event = AgentEvent(
            review_id=review_id,
            event_type=EventType.LLM_CALL,
            ts=datetime.now(UTC),
            agent="security",
            model="claude-haiku-4-5",
            tokens_in=1234,
            tokens_out=567,
            cost_usd=Decimal("0.000123"),
            latency_ms=890,
            confidence=Decimal("0.751"),
        )
        repository.insert_event(event)

        [row] = repository.fetch_events_for_review(review_id)
        assert row.cost_usd == Decimal("0.000123")
        assert row.confidence == Decimal("0.751")

    def test_confidence_rejects_a_value_outside_three_decimal_places_at_construction_time(
        self,
    ) -> None:
        with pytest.raises(ValidationError, match="decimal_max_places"):
            AgentEvent(
                review_id="precision-reject",
                event_type=EventType.DECISION,
                ts=datetime.now(UTC),
                confidence=Decimal("0.7511"),
            )


@pytest.fixture(autouse=True)
def _cleanup_iterator() -> Iterator[None]:
    """No teardown needed: `agent_events` is append-only by design, so test
    rows are never deleted -- they simply accumulate under their own
    unique, uuid-suffixed review_id/delivery_id and never collide with a
    later run. Present as an explicit no-op fixture (rather than silence)
    so a future reader does not wonder whether cleanup was forgotten.
    """
    yield
