"""Integration test: BudgetGuard reads real spend from agent_events (real Postgres).

Owns the one M8 proof this milestone's instructions call out explicitly:
"BudgetGuard reads real spend from agent_events (integration test against
real Postgres)". Everything here writes real ``llm.call`` rows via a real
``backend.database.repository.EventRepository`` against the project's own
docker-compose Postgres, then asserts ``BudgetGuard.current_spend_usd()``/
``check_and_raise()`` reflect exactly those rows -- not an in-memory
running total, and not merely a mocked repository.

Needs a real reachable Postgres (``docker compose up -d postgres`` from the
repo root). Skipped -- not failed -- when unreachable, via the same
module-level ``skipif`` pattern ``tests/integration/test_events_spine.py``
and ``tests/integration/test_queue_roundtrip.py`` already establish.

ISOLATION: every event this file writes is pinned to a fixed day well in
the PAST (2020-06-15) via an explicit ``ts``, and every ``BudgetGuard`` here
uses a clock pinned to a timestamp later the same day. Deliberately in the
past, not the future: ``sum_llm_cost_since`` has -- correctly, for its real
production use -- NO upper bound (see that method's own docstring: "at/after
this timestamp"), so a row timestamped in the FUTURE relative to whatever
day a test actually runs on would still satisfy a real BudgetGuard's
"since today's real midnight" query and silently pollute the live daily
budget this same table backs in production. A PAST pinned day has the
opposite property: it is unconditionally excluded from any real "since
today's midnight" query for as long as this test suite exists, so this
file's fixture rows can never leak into a real BudgetGuard's actual
accounting no matter when they run. Each test also uses its own unique
``review_id`` so the events tables' append-only nature (rows are never
cleaned up between tests within one process run) never lets one test's
rows leak into another's spend total in a way that would matter -- though
since ``sum_llm_cost_since`` is deliberately NOT scoped by ``review_id`` (a
daily budget is process/organization-wide, not per-review -- see
``backend.database.repository.EventRepository.sum_llm_cost_since``), the
pinned-day isolation is what actually matters here, not the review_id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from backend.core.settings import get_settings
from backend.database.models import AgentEvent, EventType
from backend.database.postgres import apply_migrations
from backend.database.repository import EventRepository
from backend.economics.budget import BudgetExceededError, BudgetGuard

_BASE_SETTINGS = get_settings()
_DATABASE_URL = _BASE_SETTINGS.database_url
_DATABASE_ADMIN_URL = _BASE_SETTINGS.database_admin_url

# A fixed day well in the past -- see module docstring's ISOLATION note for
# why past (not future) is what actually makes this file's fixture rows
# invisible to a real, present-day BudgetGuard's "since today's midnight"
# query.
_PINNED_DAY_START = datetime(2020, 6, 15, 0, 0, 0, tzinfo=UTC)
_PINNED_NOW = _PINNED_DAY_START + timedelta(hours=12)
_PREVIOUS_DAY_LATE = _PINNED_DAY_START - timedelta(seconds=1)


def _postgres_reachable(admin_dsn: str) -> bool:
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
    apply_migrations(_DATABASE_ADMIN_URL)


@pytest.fixture
def repository() -> EventRepository:
    return EventRepository(_DATABASE_URL)


def _unique_review_id() -> str:
    return f"budget-guard-{uuid.uuid4()}"


def _insert_llm_call(
    repository: EventRepository,
    *,
    review_id: str,
    ts: datetime,
    cost_usd: Decimal,
) -> None:
    """Write one real llm.call row with an explicit ts (bypassing emit_llm_call's
    ``datetime.now(UTC)``, since this test needs full control over which
    "day" each row lands on -- see module docstring's ISOLATION note).
    """
    event = AgentEvent(
        review_id=review_id,
        event_type=EventType.LLM_CALL,
        ts=ts,
        agent="security",
        model="claude-haiku-4-5",
        tokens_in=1000,
        tokens_out=500,
        cost_usd=cost_usd,
        latency_ms=100,
    )
    repository.insert_event(event)


class TestBudgetGuardReadsRealSpendFromAgentEvents:
    def test_sums_real_llm_call_rows_written_today(self, repository: EventRepository) -> None:
        review_id = _unique_review_id()
        _insert_llm_call(
            repository,
            review_id=review_id,
            ts=_PINNED_DAY_START + timedelta(hours=1),
            cost_usd=Decimal("3.500000"),
        )
        _insert_llm_call(
            repository,
            review_id=review_id,
            ts=_PINNED_DAY_START + timedelta(hours=2),
            cost_usd=Decimal("7.000000"),
        )

        guard = BudgetGuard(repository, daily_cap_usd=Decimal("20"), clock=lambda: _PINNED_NOW)
        assert guard.current_spend_usd() >= Decimal("10.500000")

    def test_events_from_a_previous_day_are_not_counted(self, repository: EventRepository) -> None:
        review_id = _unique_review_id()
        # A huge spend, but timestamped one second before the pinned day
        # starts -- must NOT be included in "today"'s total.
        _insert_llm_call(
            repository, review_id=review_id, ts=_PREVIOUS_DAY_LATE, cost_usd=Decimal("999.000000")
        )

        guard = BudgetGuard(repository, daily_cap_usd=Decimal("20"), clock=lambda: _PINNED_NOW)
        spend = guard.current_spend_usd()
        assert spend < Decimal("999"), (
            f"spend={spend} includes a previous-day event -- sum_llm_cost_since's "
            "'ts >= since' boundary is not excluding it"
        )

    def test_hard_blocks_once_real_spend_meets_the_cap(self, repository: EventRepository) -> None:
        review_id = _unique_review_id()
        # A cap tiny enough that this test's own single row already meets
        # it, regardless of any other row this pinned day might accumulate
        # across repeated test runs in the same long-lived database.
        _insert_llm_call(
            repository,
            review_id=review_id,
            ts=_PINNED_DAY_START + timedelta(hours=3),
            cost_usd=Decimal("50.000000"),
        )

        guard = BudgetGuard(repository, daily_cap_usd=Decimal("1"), clock=lambda: _PINNED_NOW)
        with pytest.raises(BudgetExceededError):
            guard.check_and_raise()

    def test_allows_when_real_spend_is_comfortably_under_a_generous_cap(
        self, repository: EventRepository
    ) -> None:
        # A fresh, isolated day-slice (a different hour on the pinned day,
        # queried with a cap large enough that this run's own tiny spend
        # cannot plausibly meet it) -- proves the "allow" path also goes
        # through the real query, not just the "block" path.
        review_id = _unique_review_id()
        _insert_llm_call(
            repository,
            review_id=review_id,
            ts=_PINNED_DAY_START + timedelta(hours=4),
            cost_usd=Decimal("0.000100"),
        )
        guard = BudgetGuard(
            repository, daily_cap_usd=Decimal("1000000"), clock=lambda: _PINNED_NOW
        )
        guard.check_and_raise()  # must not raise
