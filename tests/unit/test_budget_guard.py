"""Unit tests for backend.economics.budget.BudgetGuard against a fake repository.

Pure decision-logic tests -- no real Postgres involved (see
``tests/integration/test_budget_guard_events.py`` for the real-Postgres
proof that spend is actually read from ``agent_events``). These tests pin
``BudgetGuard``'s clock so "today" is deterministic, and assert the exact
boundary behavior (spend == cap already blocks) plus that the "start of
day" computation is what actually gets queried.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.economics.budget import BudgetExceededError, BudgetGuard


@dataclass
class _FakeRepository:
    spend: Decimal
    queried_since: list[datetime] | None = None

    def sum_llm_cost_since(self, since: datetime) -> Decimal:
        if self.queried_since is not None:
            self.queried_since.append(since)
        return self.spend


def test_under_cap_does_not_raise() -> None:
    guard = BudgetGuard(_FakeRepository(Decimal("5")), daily_cap_usd=Decimal("20"))
    guard.check_and_raise()  # must not raise


def test_over_cap_raises_with_both_figures_attached() -> None:
    guard = BudgetGuard(_FakeRepository(Decimal("25")), daily_cap_usd=Decimal("20"))
    with pytest.raises(BudgetExceededError) as excinfo:
        guard.check_and_raise()
    assert excinfo.value.spent_usd == Decimal("25")
    assert excinfo.value.cap_usd == Decimal("20")


def test_exactly_at_cap_raises() -> None:
    """The conservative boundary: spend == cap already counts as exceeded."""
    guard = BudgetGuard(_FakeRepository(Decimal("20")), daily_cap_usd=Decimal("20"))
    with pytest.raises(BudgetExceededError):
        guard.check_and_raise()


def test_just_under_cap_does_not_raise() -> None:
    guard = BudgetGuard(_FakeRepository(Decimal("19.999999")), daily_cap_usd=Decimal("20"))
    guard.check_and_raise()


def test_current_spend_usd_returns_the_repository_value() -> None:
    guard = BudgetGuard(_FakeRepository(Decimal("12.5")), daily_cap_usd=Decimal("20"))
    assert guard.current_spend_usd() == Decimal("12.5")


def test_queries_midnight_utc_of_the_pinned_clocks_day() -> None:
    queried: list[datetime] = []
    pinned_now = datetime(2030, 6, 15, 17, 45, 30, tzinfo=UTC)
    guard = BudgetGuard(
        _FakeRepository(Decimal("0"), queried_since=queried),
        daily_cap_usd=Decimal("20"),
        clock=lambda: pinned_now,
    )
    guard.current_spend_usd()
    assert queried == [datetime(2030, 6, 15, 0, 0, 0, tzinfo=UTC)]
