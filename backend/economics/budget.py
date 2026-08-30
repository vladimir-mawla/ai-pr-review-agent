"""BudgetGuard: hard-blocks the next LLM call once today's spend meets the daily cap.

Owns: the one piece of DONE.html section 2's gate list this module exists
for by name -- "BudgetGuard hard-blocks the next LLM call once the daily
cap is exceeded". Two design decisions follow directly from that gate's
wording:

1. HARD-BLOCK, not warn. ``check_and_raise`` raises ``BudgetExceededError``
   -- there is no "log a warning and proceed anyway" path anywhere in this
   module. A budget guard that only warns is not a guard.
2. Spend is read from the real ``agent_events`` table
   (``backend.database.repository.EventRepository.sum_llm_cost_for_day``),
   never an in-memory running total. An in-memory counter would reset on
   every process restart and disagree with any other process spending
   against the same budget (e.g. a future worker process alongside the API
   server) -- the whole reason M7 built an append-only events spine with
   real ``cost_usd`` on every ``llm.call`` row was so a later milestone
   would have exactly this real, shared source of truth to query instead of
   inventing a second, parallel accounting mechanism. This is that
   milestone.

WHERE THIS IS CALLED FROM: ``backend.tools.llm_client.AnthropicLLMClient.
complete`` calls ``check_and_raise`` as the very first thing it does, before
constructing or touching the underlying Anthropic SDK client at all -- see
that module's docstring for why the check must happen there and not, say,
inside ``backend.agents.security_agent.SecurityAgent``, and
``tests/unit/test_security_agent_schema.py`` for the test that proves the
underlying (fake) Anthropic client's ``messages.create`` is never invoked
once the guard trips.

"Once the daily cap is EXCEEDED": this module treats spend >= cap as
exceeded (not spend > cap) -- the conservative reading. A cap of $20.00
with exactly $20.00 already spent must not permit one more call that would
put real spend over the configured limit; there is no way to know a call's
actual cost before making it, so the only way to guarantee spend never
exceeds the cap is to block at the boundary rather than one call past it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from backend.database.repository import EventRepository


class BudgetExceededError(Exception):
    """Raised by ``BudgetGuard.check_and_raise`` once today's spend meets the cap.

    Carries both figures so a caller (or a caught-and-logged call site) can
    report exactly how far over the line spend is, not just that it is.
    """

    def __init__(self, spent_usd: Decimal, cap_usd: Decimal) -> None:
        super().__init__(
            f"daily LLM budget exceeded: spent ${spent_usd} of ${cap_usd} cap "
            "-- blocking this call before it reaches the LLM client"
        )
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd


class BudgetGuard:
    """Hard-blocks an LLM call once today's real spend meets the configured cap.

    Attributes:
        _repository: Where real spend is read from -- see module docstring.
        _daily_cap_usd: The configured cap
            (``backend.core.settings.Settings.budget_daily_cap_usd``,
            default $20, per PLAN.md's M8 text), passed explicitly rather
            than read from settings internally so this class stays a
            trivially-testable pure decision over an injected repository,
            matching ``backend.hitl.queue.route_review``'s own precedent of
            taking its threshold as an explicit argument.
        _clock: Injectable purely so tests can pin "now" (and therefore
            "the start of today") without depending on the wall clock at
            the moment the test happens to run; production call sites never
            pass this.
    """

    def __init__(
        self,
        repository: EventRepository,
        *,
        daily_cap_usd: Decimal,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._daily_cap_usd = daily_cap_usd
        self._clock = clock if clock is not None else _utc_now

    def _start_of_day_utc(self) -> datetime:
        """Midnight UTC on the current day, per ``self._clock``.

        UTC (not local time) so "today" means the same instant regardless
        of which machine/timezone a caller runs in -- consistent with every
        other timestamp in ``agent_events`` (``AgentEvent.ts`` is always
        constructed via ``datetime.now(UTC)``, see
        ``backend.observability.events``).
        """
        now = self._clock()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def current_spend_usd(self) -> Decimal:
        """Real total ``llm.call`` cost recorded today, per ``agent_events``.

        "Today" is the bounded ``[start_of_day, start_of_day + 1 day)``
        window ``EventRepository.sum_llm_cost_for_day`` sums -- a
        future-dated row (a clock error, a leaked fixture, a bug) is never
        counted toward today's spend, no matter how far in the future it is
        timestamped. See that method's docstring for why an unbounded
        `>=`-only query was a real, twice-reproduced defect, not a
        theoretical one.
        """
        return self._repository.sum_llm_cost_for_day(self._start_of_day_utc())

    def check_and_raise(self) -> None:
        """Raise ``BudgetExceededError`` iff today's spend has met the cap.

        Call this BEFORE making an LLM call, never after -- see this
        module's docstring for the exact call site and why.
        """
        spent = self.current_spend_usd()
        if spent >= self._daily_cap_usd:
            raise BudgetExceededError(spent, self._daily_cap_usd)


def _utc_now() -> datetime:
    return datetime.now(UTC)
