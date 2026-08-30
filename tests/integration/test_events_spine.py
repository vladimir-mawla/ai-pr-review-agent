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

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api.main import create_app
from backend.core.settings import Settings, get_settings
from backend.database.models import AgentEvent, EventType
from backend.database.postgres import apply_migrations
from backend.database.repository import EventRepository
from backend.job_queue.in_memory import InMemoryJobQueue
from backend.observability.audit import reconstruct_review_trace
from backend.observability.workflow_context import run_id_for_delivery
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.state import GraphState

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


# ---------------------------------------------------------------------------
# Live call site 2: orchestrator spans + aggregator decision.
# ---------------------------------------------------------------------------


class TestOrchestratorProducesSpansAndDecision:
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

        # Every span.end recorded a real measured latency (specialists sleep
        # NODE_WORK_SECONDS=0.2s, so this is never zero on a real run).
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
