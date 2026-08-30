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

REVISED (L2 DEBUG, post-L4-REJECT): this file previously reasoned that
``sum_llm_cost_since`` "correctly, for its real production use, has NO
upper bound" and pinned its fixture rows to a day safely in the PAST purely
to dodge that gap, rather than closing it. That reasoning was wrong, and
two independent sessions proved it the hard way: the M8 builder session hit
a spurious ``BudgetExceededError: spent $2119.000446 of $20 cap`` from
stray 2030-dated fixture rows an earlier run had left in this same
append-only table, and an independent L4 VERIFY session -- re-pinning to
the FUTURE specifically to probe the boundary this file's own docstring had
just declared safe to ignore -- reproduced the identical defect class with
its own 2099-dated rows (``$40.00 of $20 cap``). Re-pinning the fixture day
(the original fix) only relocated the symptom; it never touched the actual
defect, which was the query itself having no upper bound.
``EventRepository.sum_llm_cost_for_day`` (renamed from
``sum_llm_cost_since``) now queries the genuine half-open
``[day_start, day_start + 1 day)`` window -- see that method's own
docstring for the full defect history -- which is what
``TestFutureDatedRowsAreExcluded`` and ``TestExactBoundaryInstants`` below
exist to prove directly, not merely to dodge again.

ISOLATION: every event this file writes is still pinned to a fixed day well
in the PAST (2020-06-15) via an explicit ``ts``, and every ``BudgetGuard``
here uses a clock pinned to a timestamp later the same day. This is now
belt-and-suspenders, not the load-bearing fix: with the query correctly
bounded, a fixture row pinned to ANY day other than the one a real
BudgetGuard is actually querying -- past or future -- is unconditionally
excluded from that real query's ``[real_today_start, real_today_start + 1
day)`` window. Keeping the past-day pin costs nothing and preserves defense
in depth against a future regression of this exact bug reintroducing an
unbounded query. Each test also uses its own unique ``review_id`` so the
events table's append-only nature (rows are never cleaned up between tests
within one process run) never lets one test's rows leak into another's
spend total in a way that would matter -- though since
``sum_llm_cost_for_day`` is deliberately NOT scoped by ``review_id`` (a
daily budget is process/organization-wide, not per-review -- see
``backend.database.repository.EventRepository.sum_llm_cost_for_day``), the
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
_PINNED_DAY_END = _PINNED_DAY_START + timedelta(days=1)
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
            f"spend={spend} includes a previous-day event -- sum_llm_cost_for_day's "
            "'ts >= day_start' lower bound is not excluding it"
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


class TestFutureDatedRowsAreExcluded:
    """THE regression: a row timestamped AFTER the queried day must not count.

    This is the actual defect two independent sessions hit in production use
    (not merely a hypothetical): ``sum_llm_cost_since`` (the pre-fix name)
    had no upper bound at all, so a row timestamped years in the future was
    still ``>= day_start`` and got summed into *every* day's spend from then
    until the real calendar caught up to it. ``sum_llm_cost_for_day``'s
    ``ts < day_start + 1 day`` upper bound is what these tests prove.

    Every test below asserts a BEFORE/AFTER equality on ``current_spend_usd()``
    (not an absolute cap comparison) specifically so it stays correct
    regardless of how much this pinned day's cumulative spend already is from
    other tests in this module (the append-only table is never cleaned up
    between tests) -- inserting a row outside the window must change nothing,
    no matter what the running total already was.
    """

    def test_events_from_the_next_day_are_not_counted(self, repository: EventRepository) -> None:
        guard = BudgetGuard(
            repository, daily_cap_usd=Decimal("1000000"), clock=lambda: _PINNED_NOW
        )
        spend_before = guard.current_spend_usd()

        review_id = _unique_review_id()
        # A huge spend, timestamped exactly one second into the NEXT day --
        # just past the queried day's upper bound. Must NOT be included.
        _insert_llm_call(
            repository,
            review_id=review_id,
            ts=_PINNED_DAY_END + timedelta(seconds=1),
            cost_usd=Decimal("999.000000"),
        )

        spend_after = guard.current_spend_usd()
        assert spend_after == spend_before, (
            f"spend changed from {spend_before} to {spend_after} after inserting a "
            "next-day event -- sum_llm_cost_for_day's 'ts < day_start + 1 day' "
            "upper bound is not excluding it"
        )

    def test_a_far_future_dated_row_does_not_inflate_the_queried_days_spend(
        self, repository: EventRepository
    ) -> None:
        """The exact real-world repro: a row dated years ahead (2030/2099-style
        fixture pollution) must not leak into an unrelated day's budget check.
        """
        guard = BudgetGuard(
            repository, daily_cap_usd=Decimal("1000000"), clock=lambda: _PINNED_NOW
        )
        spend_before = guard.current_spend_usd()

        review_id = _unique_review_id()
        _insert_llm_call(
            repository,
            review_id=review_id,
            ts=_PINNED_DAY_START.replace(year=_PINNED_DAY_START.year + 10),
            cost_usd=Decimal("2119.000446"),
        )

        spend_after = guard.current_spend_usd()
        assert spend_after == spend_before, (
            f"spend changed from {spend_before} to {spend_after} after inserting a "
            "far-future-dated row -- it must not count toward this day's spend"
        )
        guard.check_and_raise()  # the generous cap above must still not be hit


class TestExactBoundaryInstants:
    """The half-open interval's two edges, exactly as documented:
    ``[day_start, day_start + 1 day)`` -- ``day_start`` itself counts,
    ``day_start + 1 day`` (the first instant of the next day) does not.
    """

    def test_a_row_at_exactly_day_start_counts(self, repository: EventRepository) -> None:
        guard = BudgetGuard(
            repository, daily_cap_usd=Decimal("1000000"), clock=lambda: _PINNED_NOW
        )
        spend_before = guard.current_spend_usd()

        review_id = _unique_review_id()
        _insert_llm_call(
            repository, review_id=review_id, ts=_PINNED_DAY_START, cost_usd=Decimal("6.000000")
        )

        spend_after = guard.current_spend_usd()
        assert spend_after == spend_before + Decimal("6.000000"), (
            f"spend went from {spend_before} to {spend_after} -- a row at exactly "
            "day_start (the inclusive lower bound) must count in full"
        )

    def test_a_row_at_exactly_day_start_plus_one_day_does_not_count(
        self, repository: EventRepository
    ) -> None:
        guard = BudgetGuard(
            repository, daily_cap_usd=Decimal("1000000"), clock=lambda: _PINNED_NOW
        )
        spend_before = guard.current_spend_usd()

        review_id = _unique_review_id()
        _insert_llm_call(
            repository,
            review_id=review_id,
            ts=_PINNED_DAY_END,
            cost_usd=Decimal("999.000000"),
        )

        spend_after = guard.current_spend_usd()
        assert spend_after == spend_before, (
            f"spend changed from {spend_before} to {spend_after} -- a row at exactly "
            "day_start + 1 day (the exclusive upper bound) must NOT count"
        )
