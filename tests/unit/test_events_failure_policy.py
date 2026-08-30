"""Unit tests for `backend.observability.events`'s failure policy (L2 DEBUG,
post-L4-REJECT on M7).

Owns: proving, with no real Postgres required, exactly what `_emit`
(exercised here through the public `emit_decision`) does and does not
swallow:

- `psycopg.Error` (a connectivity/availability failure), `OSError` (a
  lower-level connect failure), and `CircuitOpenError`
  (`backend.reliability.circuit_breaker` -- EventRepository's own breaker
  has tripped) are all logged and swallowed -- the request/orchestrator
  path this milestone protects must never fail because the events store
  is unavailable.
- `psycopg.errors.IntegrityError` (and its subclasses, e.g.
  `CheckViolation`) is NOT swallowed -- that is a bug in our own code (we
  tried to write a row the schema itself rejects), not an availability
  failure, and conflating the two would hide a real defect behind the
  same log line an ordinary outage produces. This is the regression test
  for the "narrow the swallow" fix: against the pre-fix
  `except (psycopg.Error, OSError)`, `IntegrityError` -- being a subclass
  of `psycopg.Error` -- was caught and swallowed right alongside a real
  outage. This test fails against that old, broader except clause and
  passes against the fix.

Uses real `EventRepository` subclasses (not a duck-typed mock) so
`emit_decision`'s `repository: EventRepository` parameter type-checks
under `mypy --strict` the same way production call sites do.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from backend.database.models import AgentEvent
from backend.database.repository import EventRepository
from backend.observability.events import emit_decision
from backend.reliability.circuit_breaker import CircuitOpenError

_UNUSED_DSN = "postgresql://unused:unused@localhost:1/unused"


class _RaisingRepository(EventRepository):
    """An EventRepository whose `insert_event` always raises a fixed exception.

    A real subclass (not a mock) so it satisfies `EventRepository`'s type
    for `mypy --strict` at every call site that type-hints against it.
    """

    def __init__(self, exc: BaseException) -> None:
        super().__init__(_UNUSED_DSN)
        self._exc = exc

    def insert_event(self, event: AgentEvent) -> None:
        raise self._exc


def _call_emit_decision(repository: EventRepository) -> None:
    emit_decision(repository, "review-1", agent=None, outcome="accepted")


class TestSwallowedFailures:
    """Availability/dependency failures: logged and swallowed, never raised."""

    def test_psycopg_operational_error_is_swallowed(self) -> None:
        repository = _RaisingRepository(psycopg.OperationalError("connection refused"))
        _call_emit_decision(repository)  # must not raise

    def test_os_error_is_swallowed(self) -> None:
        repository = _RaisingRepository(OSError("network is unreachable"))
        _call_emit_decision(repository)  # must not raise

    def test_circuit_open_error_is_swallowed(self) -> None:
        """A tripped EventRepository breaker must not break the caller either."""
        repository = _RaisingRepository(CircuitOpenError("events_db"))
        _call_emit_decision(repository)  # must not raise


class TestNarrowedSwallowPropagatesIntegrityErrors:
    """The regression test: a CHECK/constraint violation is OUR bug and must
    propagate, not be swallowed alongside a real outage.

    Against the pre-fix `except (psycopg.Error, OSError)` in `_emit`, both
    tests below would fail (no exception raised, since `IntegrityError` and
    `CheckViolation` are both subclasses of `psycopg.Error`).
    """

    def test_integrity_error_propagates(self) -> None:
        repository = _RaisingRepository(psycopg.IntegrityError("duplicate key or bad row"))
        with pytest.raises(psycopg.IntegrityError):
            _call_emit_decision(repository)

    def test_check_violation_propagates(self) -> None:
        """CheckViolation is IntegrityError's concrete real-world case here:
        agent_events.event_type's CHECK constraint rejecting a bad value."""
        repository = _RaisingRepository(
            psycopg.errors.CheckViolation('new row violates check constraint "agent_events_event_type_check"')
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _call_emit_decision(repository)

    def test_unrelated_value_error_from_repository_still_propagates_too(self) -> None:
        """Sanity check: this policy was never about swallowing everything --
        only the two named availability exception types (plus CircuitOpenError).
        A ValueError (a bug, not `psycopg.Error`/`OSError`/`CircuitOpenError`)
        must propagate exactly as it always has.
        """
        repository = _RaisingRepository(ValueError("some unrelated bug"))
        with pytest.raises(ValueError, match="some unrelated bug"):
            _call_emit_decision(repository)


class TestEventConstructionValidationStillPropagates:
    """Pre-existing behavior, unaffected by this milestone's fix: a bad
    AgentEvent (an in-process validation bug) is never even routed through
    `_emit`'s try/except -- it fails at construction time, before any DB
    call is attempted, so no repository/breaker/swallow policy is even
    reachable."""

    def test_confidence_out_of_bounds_raises_before_any_db_call(self) -> None:
        from decimal import Decimal

        from pydantic import ValidationError

        from backend.database.models import EventType

        with pytest.raises(ValidationError):
            AgentEvent(
                review_id="review-1",
                event_type=EventType.DECISION,
                ts=datetime.now(UTC),
                confidence=Decimal("1.500"),  # out of [0, 1] bounds
            )
