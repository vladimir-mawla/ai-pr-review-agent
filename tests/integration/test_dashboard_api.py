"""Integration tests for ``backend.api.dashboard``'s three (plus one) JSON API routes.

FREE -- no LLM call, needs a real reachable Postgres. Every test runs
against a fresh, per-module-run, disposable schema (mirroring
``tests/integration/test_budget_guard_events.py``'s own isolation
pattern), so this file never depends on -- or pollutes -- whatever
production/other-tests have already written into the shared long-lived
``agent_events``/``reviews`` tables. This isolation is also what makes
"an empty DB produces an honest empty state" directly testable: a fresh
schema genuinely has zero rows, not "zero rows unless a previous test run
left something behind".

Covers this milestone's named test requirements:
- the aggregation queries return correct numbers against known seeded events
- the HITL queue endpoint returns queued reviews and excludes posted ones
- trace reconstruction returns events in order for a review_id
- an empty DB produces an honest empty state, not a crash and not
  fabricated data
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.core.settings import Settings, get_settings
from backend.database.models import AgentEvent, EventType
from backend.database.postgres import MIGRATIONS_DIR
from backend.database.repository import EventRepository
from backend.database.review_store import ReviewRepository
from backend.job_queue.in_memory import InMemoryJobQueue

_BASE_SETTINGS = get_settings()
_DATABASE_URL = _BASE_SETTINGS.database_url
_DATABASE_ADMIN_URL = _BASE_SETTINGS.database_admin_url


def _postgres_reachable(admin_dsn: str) -> bool:
    try:
        with psycopg.connect(admin_dsn, connect_timeout=1) as conn:
            conn.execute("SELECT 1")
        return True
    except (psycopg.Error, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(_DATABASE_ADMIN_URL),
    reason=f"Postgres not reachable at {_DATABASE_ADMIN_URL} -- run `docker compose up -d postgres` first",
)


def _create_isolated_schema(admin_dsn: str, schema: str) -> None:
    """Mirrors test_budget_guard_events.py's ``_create_isolated_events_schema``, extended to both tables."""
    with psycopg.connect(admin_dsn, autocommit=True, cursor_factory=psycopg.ClientCursor) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}, public")
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.execute(path.read_text())
        conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO agent_events_writer")


@pytest.fixture
def isolated_schema() -> Iterator[str]:
    """A fresh schema per TEST (not per module) -- these tests assert exact counts/sums."""
    schema = f"test_dashboard_{uuid.uuid4().hex}"
    _create_isolated_schema(_DATABASE_ADMIN_URL, schema)
    try:
        yield schema
    finally:
        with psycopg.connect(_DATABASE_ADMIN_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.fixture
def event_repository(isolated_schema: str) -> EventRepository:
    return EventRepository(_DATABASE_URL, search_path=f"{isolated_schema},public")


@pytest.fixture
def review_repository(isolated_schema: str) -> ReviewRepository:
    return ReviewRepository(_DATABASE_URL, search_path=f"{isolated_schema},public")


@pytest.fixture
def client(event_repository: EventRepository, review_repository: ReviewRepository) -> TestClient:
    app = create_app(
        settings=Settings(github_webhook_secret="dashboard-test-secret"),
        job_queue=InMemoryJobQueue(),
        event_repository=event_repository,
        review_repository=review_repository,
    )
    return TestClient(app)


def _unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


class TestAgentMetricsAggregation:
    def test_aggregates_known_seeded_events_correctly(
        self, event_repository: EventRepository, client: TestClient
    ):
        now = datetime.now(UTC)
        # Two security calls, one quality call -- known, exact figures.
        event_repository.insert_event(
            AgentEvent(
                review_id=_unique_id("r"),
                event_type=EventType.LLM_CALL,
                ts=now,
                agent="security",
                model="claude-haiku-4-5",
                tokens_in=100,
                tokens_out=50,
                cost_usd=Decimal("0.000350"),
                latency_ms=400,
            )
        )
        event_repository.insert_event(
            AgentEvent(
                review_id=_unique_id("r"),
                event_type=EventType.LLM_CALL,
                ts=now,
                agent="security",
                model="claude-haiku-4-5",
                tokens_in=200,
                tokens_out=100,
                cost_usd=Decimal("0.000700"),
                latency_ms=600,
            )
        )
        event_repository.insert_event(
            AgentEvent(
                review_id=_unique_id("r"),
                event_type=EventType.LLM_CALL,
                ts=now,
                agent="quality",
                model="claude-haiku-4-5",
                tokens_in=80,
                tokens_out=40,
                cost_usd=Decimal("0.000280"),
                latency_ms=1000,
            )
        )

        response = client.get("/api/agent-metrics")
        assert response.status_code == 200
        body = response.json()
        assert body["is_empty"] is False

        by_agent = {row["agent"]: row for row in body["metrics"]}
        assert by_agent["security"]["call_count"] == 2
        assert Decimal(by_agent["security"]["total_cost_usd"]) == Decimal("0.001050")
        assert by_agent["security"]["avg_latency_ms"] == 500  # (400 + 600) / 2
        assert by_agent["security"]["total_tokens_in"] == 300
        assert by_agent["security"]["total_tokens_out"] == 150

        assert by_agent["quality"]["call_count"] == 1
        assert Decimal(by_agent["quality"]["total_cost_usd"]) == Decimal("0.000280")
        assert by_agent["quality"]["avg_latency_ms"] == 1000

        assert Decimal(body["total_cost_usd"]) == Decimal("0.001330")

    def test_ignores_non_llm_call_events(self, event_repository: EventRepository, client: TestClient):
        event_repository.insert_event(
            AgentEvent(
                review_id=_unique_id("r"),
                event_type=EventType.SPAN_START,
                ts=datetime.now(UTC),
                agent="security",
            )
        )
        response = client.get("/api/agent-metrics")
        body = response.json()
        assert body["is_empty"] is True
        assert body["metrics"] == []
        assert Decimal(body["total_cost_usd"]) == Decimal("0")


class TestSyntheticRowsExcludedFromTotals:
    """L2 DEBUG (post-L4-REJECT): proves the concrete defect L4 VERIFY found is fixed.

    L4 VERIFY's finding: ``/costs`` summed 19 2030-dated ``budget-guard-*``
    fixture rows (~$40,261) into "Total spend across every agent" at the
    SAME ``(agent, model)`` key genuine calls use, with no date filter, no
    test-prefix exclusion, and no disclosure. Each test below seeds one
    real row plus one synthetic row that would have been silently summed
    together under the old, unfiltered query, and asserts the real row
    alone is counted while the synthetic one is reported as an exclusion,
    not merely absent.
    """

    def test_future_dated_row_is_excluded_and_disclosed(
        self, event_repository: EventRepository, client: TestClient
    ) -> None:
        now = datetime.now(UTC)
        event_repository.insert_event(
            AgentEvent(
                review_id=_unique_id("r"),
                event_type=EventType.LLM_CALL,
                ts=now,
                agent="security",
                model="claude-haiku-4-5",
                tokens_in=100,
                tokens_out=50,
                cost_usd=Decimal("0.000350"),
                latency_ms=400,
            )
        )
        event_repository.insert_event(
            AgentEvent(
                # Not a recognized test-fixture prefix -- proves the
                # future-dated mechanism alone catches this row, independent
                # of the prefix mechanism.
                review_id=_unique_id("webhook"),
                event_type=EventType.LLM_CALL,
                ts=datetime(2030, 6, 15, tzinfo=UTC),
                agent="security",
                model="claude-haiku-4-5",
                tokens_in=1000,
                tokens_out=500,
                cost_usd=Decimal("2119.000446"),
                latency_ms=100,
            )
        )

        response = client.get("/api/agent-metrics")
        assert response.status_code == 200
        body = response.json()

        assert body["is_empty"] is False
        by_agent = {row["agent"]: row for row in body["metrics"]}
        assert by_agent["security"]["call_count"] == 1
        assert Decimal(by_agent["security"]["total_cost_usd"]) == Decimal("0.000350")
        assert Decimal(body["total_cost_usd"]) == Decimal("0.000350")

        exclusions = body["exclusions"]
        assert exclusions["excluded_row_count"] == 1
        assert Decimal(exclusions["excluded_cost_usd"]) == Decimal("2119.000446")
        assert exclusions["future_dated_count"] == 1
        assert Decimal(exclusions["future_dated_cost_usd"]) == Decimal("2119.000446")
        assert exclusions["test_fixture_count"] == 0
        assert "2119.000446" in exclusions["note"]

    def test_past_dated_test_fixture_prefix_row_is_excluded_and_disclosed(
        self, event_repository: EventRepository, client: TestClient
    ) -> None:
        """A past-dated row with a recognized test prefix -- the case a date-only filter would miss.

        This mirrors the real 151 non-future ``budget-guard-*`` rows found
        sitting in this project's own production database: pinned to a day
        in the PAST (2020-06-15, exactly the fixture day
        ``test_budget_guard_events.py`` uses), so ``ts > now()`` alone
        would never catch them -- only the review_id prefix does.
        """
        real_id = _unique_id("r")
        event_repository.insert_event(
            AgentEvent(
                review_id=real_id,
                event_type=EventType.LLM_CALL,
                ts=datetime.now(UTC),
                agent="quality",
                model="claude-haiku-4-5",
                tokens_in=80,
                tokens_out=40,
                cost_usd=Decimal("0.000280"),
                latency_ms=1000,
            )
        )
        event_repository.insert_event(
            AgentEvent(
                review_id=f"budget-guard-{real_id}-synthetic",
                event_type=EventType.LLM_CALL,
                ts=datetime(2020, 6, 15, tzinfo=UTC),  # in the PAST -- not future-dated
                agent="quality",
                model="claude-haiku-4-5",
                tokens_in=1000,
                tokens_out=500,
                cost_usd=Decimal("2119.000446"),
                latency_ms=100,
            )
        )

        response = client.get("/api/agent-metrics")
        assert response.status_code == 200
        body = response.json()

        by_agent = {row["agent"]: row for row in body["metrics"]}
        assert by_agent["quality"]["call_count"] == 1
        assert Decimal(by_agent["quality"]["total_cost_usd"]) == Decimal("0.000280")

        exclusions = body["exclusions"]
        assert exclusions["excluded_row_count"] == 1
        assert Decimal(exclusions["excluded_cost_usd"]) == Decimal("2119.000446")
        assert exclusions["future_dated_count"] == 0
        assert exclusions["test_fixture_count"] == 1
        assert Decimal(exclusions["test_fixture_cost_usd"]) == Decimal("2119.000446")

    def test_no_exclusions_reports_zero_and_a_clean_note(
        self, event_repository: EventRepository, client: TestClient
    ) -> None:
        event_repository.insert_event(
            AgentEvent(
                review_id=_unique_id("webhook"),
                event_type=EventType.LLM_CALL,
                ts=datetime.now(UTC),
                agent="docs",
                model="claude-haiku-4-5",
                tokens_in=10,
                tokens_out=5,
                cost_usd=Decimal("0.000010"),
                latency_ms=50,
            )
        )
        response = client.get("/api/agent-metrics")
        body = response.json()
        exclusions = body["exclusions"]
        assert exclusions["excluded_row_count"] == 0
        assert Decimal(exclusions["excluded_cost_usd"]) == Decimal("0")
        assert exclusions["note"] == "No rows were excluded from the totals above."


class TestHitlQueueEndpoint:
    def test_returns_queued_reviews_and_excludes_posted_ones(
        self, review_repository: ReviewRepository, client: TestClient
    ):
        from backend.models import Finding, Review, ReviewStatus, compute_overall_confidence

        finding = Finding(
            agent_type="SECURITY",
            severity="CRITICAL",
            category="sql_injection",
            file_path="app/db.py",
            line_start=1,
            line_end=1,
            confidence=Decimal("0.950"),
            rationale="test",
        )
        queued_id = _unique_id("queued")
        posted_id = _unique_id("posted")
        queued_review = Review(
            review_id=queued_id,
            pr_number=1,
            repository_owner="o",
            repository_name="r",
            head_sha="a" * 40,
            findings=[finding],
            overall_confidence=compute_overall_confidence([finding]),
            status=ReviewStatus.QUEUED_FOR_HITL,
            created_at=datetime.now(UTC),
            error_message=None,
        )
        posted_review = Review(
            review_id=posted_id,
            pr_number=2,
            repository_owner="o",
            repository_name="r",
            head_sha="b" * 40,
            findings=[],
            overall_confidence=compute_overall_confidence([]),
            status=ReviewStatus.POSTED,
            created_at=datetime.now(UTC),
            error_message=None,
        )
        review_repository.upsert_review(queued_review, reason="a CRITICAL finding is present")
        review_repository.upsert_review(posted_review, reason="auto-posted")

        response = client.get("/api/hitl-queue")
        assert response.status_code == 200
        body = response.json()
        returned_ids = {r["review_id"] for r in body["reviews"]}

        assert queued_id in returned_ids
        assert posted_id not in returned_ids
        queued_out = next(r for r in body["reviews"] if r["review_id"] == queued_id)
        assert queued_out["reason"] == "a CRITICAL finding is present"
        assert len(queued_out["findings"]) == 1
        assert queued_out["findings"][0]["category"] == "sql_injection"
        assert queued_out["findings"][0]["severity"] == "CRITICAL"

    def test_empty_queue_is_an_honest_empty_list_not_a_crash(self, client: TestClient):
        response = client.get("/api/hitl-queue")
        assert response.status_code == 200
        body = response.json()
        assert body["reviews"] == []
        assert body["count"] == 0


class TestTraceReconstruction:
    def test_returns_events_in_time_order_for_a_review_id(
        self, event_repository: EventRepository, client: TestClient
    ):
        review_id = _unique_id("trace")
        base = datetime.now(UTC)
        event_repository.insert_event(
            AgentEvent(review_id=review_id, event_type=EventType.SPAN_START, ts=base, agent="security")
        )
        event_repository.insert_event(
            AgentEvent(
                review_id=review_id,
                event_type=EventType.LLM_CALL,
                ts=base + timedelta(milliseconds=10),
                agent="security",
                model="claude-haiku-4-5",
                tokens_in=10,
                tokens_out=5,
                cost_usd=Decimal("0.000050"),
                latency_ms=200,
            )
        )
        event_repository.insert_event(
            AgentEvent(
                review_id=review_id,
                event_type=EventType.SPAN_END,
                ts=base + timedelta(milliseconds=20),
                agent="security",
                latency_ms=200,
                outcome="ok",
            )
        )
        # An unrelated review's event must never leak into this trace.
        event_repository.insert_event(
            AgentEvent(review_id=_unique_id("other"), event_type=EventType.SPAN_START, ts=base, agent="quality")
        )

        response = client.get(f"/api/trace/{review_id}")
        assert response.status_code == 200
        body = response.json()
        assert [e["event_type"] for e in body["events"]] == ["span.start", "llm.call", "span.end"]
        assert body["review"] is None  # no Review was ever persisted for this id

    def test_unknown_review_id_is_an_honest_empty_trace_not_a_404(self, client: TestClient):
        response = client.get(f"/api/trace/{_unique_id('never-seen')}")
        assert response.status_code == 200
        body = response.json()
        assert body["events"] == []
        assert body["review"] is None


class TestEmptyDatabaseHonestState:
    def test_agent_metrics_on_a_fresh_database_says_so_explicitly(self, client: TestClient):
        response = client.get("/api/agent-metrics")
        body = response.json()
        assert body["metrics"] == []
        assert body["total_cost_usd"] == "0"
        assert body["is_empty"] is True
        # A fresh, empty database has nothing to exclude either -- the
        # exclusions disclosure itself must also render an honest empty
        # state, not omit itself or error.
        assert body["exclusions"]["excluded_row_count"] == 0
        assert body["exclusions"]["note"] == "No rows were excluded from the totals above."

    def test_recent_reviews_on_a_fresh_database_is_an_empty_list(self, client: TestClient):
        response = client.get("/api/reviews")
        assert response.status_code == 200
        assert response.json() == {"reviews": []}
