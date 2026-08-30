"""Unit tests for backend.tools.llm_client.AnthropicLLMClient.

No test in this file requires ANTHROPIC_API_KEY -- every AnthropicLLMClient
constructed here is given an injected fake ``anthropic_client`` (a bare
object exposing ``.messages.create(...)``), so the real Anthropic SDK
client (``AnthropicLLMClient._client``'s lazy real-client path) is never
reached at all. This is the "fake/stub LLM client" this milestone's
credential policy requires every unit test to use.

Covers, one class per concern:
- ``TestBudgetGuardBlocksBeforeTheClient``: DONE.html's own gate -- a
  forced-over-cap BudgetGuard blocks BEFORE the injected client is ever
  invoked (proven by asserting its call count stays 0, not just that an
  exception was raised).
- ``TestReliabilityComposition``: the M6 retry/circuit-breaker layer,
  reused (not hand-rolled) around this new outbound call, proven by fault
  injection -- a transient failure is retried to success, and a
  persistently failing client trips the breaker and then fails fast
  without invoking the client again.
- ``TestCostAndEventAccounting``: a successful call computes the right USD
  cost from real token usage and emits exactly one ``llm.call`` event --
  M7's ``emit_llm_call`` getting its first live call site, proven
  dynamically here (unit-level; the Postgres-backed proof lives in
  ``tests/integration/test_budget_guard_events.py``).
- ``TestCompleteAsyncDoesNotBlockTheEventLoop``: ``complete_async`` actually
  offloads the blocking call via ``asyncio.to_thread`` rather than merely
  being declared ``async def`` and blocking anyway -- the exact defect
  class this project has already been bitten by twice (see
  ``backend.observability.events.emit_decision_async``'s own docstring).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from anthropic.types import TextBlock
from anthropic.types.message import Message
from anthropic.types.usage import Usage

from backend.core.settings import Settings
from backend.database.models import AgentEvent
from backend.economics.budget import BudgetExceededError, BudgetGuard
from backend.tools.llm_client import AnthropicLLMClient, LLMCallFailedError


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "github_webhook_secret": "unused-in-this-test",
        "llm_timeout_seconds": 5.0,
        "llm_retry_max_attempts": 3,
        "llm_retry_base_delay_seconds": 0.01,
        "llm_retry_max_delay_seconds": 0.02,
        "llm_circuit_breaker_failure_threshold": 2,
        "llm_circuit_breaker_reset_timeout_seconds": 0.2,
        "budget_daily_cap_usd": Decimal("20"),
        "anthropic_model": "claude-haiku-4-5",
    }
    values.update(overrides)
    return Settings(**values)


def _fake_message(text: str = "ok", *, tokens_in: int = 100, tokens_out: int = 50) -> Message:
    """A real ``anthropic.types.Message`` (not a duck-typed stand-in) for a fake response."""
    return Message(
        id="msg_fake",
        content=[TextBlock(type="text", text=text)],
        model="claude-haiku-4-5",
        role="assistant",
        stop_reason="end_turn",
        stop_sequence=None,
        type="message",
        usage=Usage(input_tokens=tokens_in, output_tokens=tokens_out),
    )


class _FakeMessagesEndpoint:
    """Stands in for ``anthropic.Anthropic().messages`` -- just the one method used."""

    def __init__(self, responder: Any) -> None:
        self._responder = responder
        self.call_count = 0

    def create(self, **kwargs: Any) -> Message:
        self.call_count += 1
        return self._responder(**kwargs)


class _FakeAnthropicClient:
    """Stands in for ``anthropic.Anthropic`` -- injected via ``AnthropicLLMClient(anthropic_client=...)``."""

    def __init__(self, responder: Any) -> None:
        self.messages = _FakeMessagesEndpoint(responder)

    @property
    def call_count(self) -> int:
        return self.messages.call_count


@dataclass
class _FakeEventRepository:
    """Duck-typed stand-in for ``EventRepository`` -- satisfies both methods
    ``AnthropicLLMClient`` needs (``sum_llm_cost_for_day`` via
    ``BudgetGuard``, ``insert_event`` via ``emit_llm_call``) without
    touching any real Postgres connection at all.
    """

    spend: Decimal = Decimal("0")
    inserted: list[AgentEvent] = field(default_factory=list)

    def sum_llm_cost_for_day(self, day_start: datetime) -> Decimal:
        return self.spend

    def insert_event(self, event: AgentEvent) -> None:
        self.inserted.append(event)


class TestBudgetGuardBlocksBeforeTheClient:
    """DONE.html: 'BudgetGuard hard-blocks the next LLM call once the daily cap is exceeded'."""

    def test_over_cap_blocks_before_the_fake_client_is_ever_invoked(self) -> None:
        repository = _FakeEventRepository(spend=Decimal("25"))  # over the $20 cap below
        fake_client = _FakeAnthropicClient(responder=lambda **_: _fake_message())
        client = AnthropicLLMClient(
            settings=_settings(),
            anthropic_client=fake_client,
            budget_guard=BudgetGuard(repository, daily_cap_usd=Decimal("20")),
            event_repository=repository,
        )

        with pytest.raises(BudgetExceededError):
            client.complete(system="sys", user="diff", agent="security")

        # The real proof: the underlying client was NEVER called, not merely
        # that an exception happened to be raised somewhere.
        assert fake_client.call_count == 0

    def test_exactly_at_cap_also_blocks(self) -> None:
        """The boundary is inclusive: spend == cap already blocks the next call."""
        repository = _FakeEventRepository(spend=Decimal("20"))
        fake_client = _FakeAnthropicClient(responder=lambda **_: _fake_message())
        client = AnthropicLLMClient(
            settings=_settings(),
            anthropic_client=fake_client,
            budget_guard=BudgetGuard(repository, daily_cap_usd=Decimal("20")),
            event_repository=repository,
        )

        with pytest.raises(BudgetExceededError):
            client.complete(system="sys", user="diff", agent="security")
        assert fake_client.call_count == 0

    def test_under_cap_allows_the_call_through(self) -> None:
        repository = _FakeEventRepository(spend=Decimal("5"))
        fake_client = _FakeAnthropicClient(responder=lambda **_: _fake_message())
        client = AnthropicLLMClient(
            settings=_settings(),
            anthropic_client=fake_client,
            budget_guard=BudgetGuard(repository, daily_cap_usd=Decimal("20")),
            event_repository=repository,
        )

        response = client.complete(system="sys", user="diff", agent="security")
        assert response.text == "ok"
        assert fake_client.call_count == 1


class TestReliabilityComposition:
    """M6's retry/circuit-breaker layer, reused around this new outbound call, proven by fault injection."""

    def test_a_transient_failure_is_retried_to_success(self) -> None:
        attempts = {"count": 0}

        def flaky(**_: Any) -> Message:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("transient upstream failure")
            return _fake_message("recovered")

        fake_client = _FakeAnthropicClient(responder=flaky)
        repository = _FakeEventRepository()
        client = AnthropicLLMClient(
            settings=_settings(llm_retry_max_attempts=3, llm_circuit_breaker_failure_threshold=5),
            anthropic_client=fake_client,
            budget_guard=BudgetGuard(repository, daily_cap_usd=Decimal("20")),
            event_repository=repository,
        )

        response = client.complete(system="sys", user="diff", agent="security")
        assert response.text == "recovered"
        assert attempts["count"] == 3  # 2 failures + 1 success, all counted

    def test_persistent_failure_trips_the_breaker_then_fails_fast(self) -> None:
        """A 100%-failing client trips the breaker within N attempts; then it fails fast (no further calls)."""

        def always_fails(**_: Any) -> Message:
            raise RuntimeError("upstream is down")

        fake_client = _FakeAnthropicClient(responder=always_fails)
        repository = _FakeEventRepository()
        client = AnthropicLLMClient(
            # 1 attempt per complete() call, so each complete() maps to
            # exactly one breaker.call() -- makes the "N attempts to trip"
            # arithmetic exact and easy to assert on.
            settings=_settings(llm_retry_max_attempts=1, llm_circuit_breaker_failure_threshold=2),
            anthropic_client=fake_client,
            budget_guard=BudgetGuard(repository, daily_cap_usd=Decimal("20")),
            event_repository=repository,
        )

        # First 2 calls: real attempts against the fake client, both fail,
        # tripping the breaker to OPEN on the 2nd (failure_threshold=2).
        for _ in range(2):
            with pytest.raises(LLMCallFailedError):
                client.complete(system="sys", user="diff", agent="security")
        assert fake_client.call_count == 2

        # 3rd call: the breaker is OPEN -- fails fast, the fake client is
        # NOT invoked a 3rd time. This is the actual proof the breaker (not
        # just the retry loop) is wired in, not merely imported.
        with pytest.raises(LLMCallFailedError):
            client.complete(system="sys", user="diff", agent="security")
        assert fake_client.call_count == 2, "breaker should fail fast without calling the client again"


class TestCostAndEventAccounting:
    """A successful call computes real cost and emits M7's llm.call event -- its first live call site."""

    def test_successful_call_computes_cost_and_emits_one_llm_call_event(self) -> None:
        repository = _FakeEventRepository()
        fake_client = _FakeAnthropicClient(
            responder=lambda **_: _fake_message("findings here", tokens_in=1_000_000, tokens_out=500_000)
        )
        client = AnthropicLLMClient(
            settings=_settings(),
            anthropic_client=fake_client,
            budget_guard=BudgetGuard(repository, daily_cap_usd=Decimal("20")),
            event_repository=repository,
        )

        response = client.complete(
            system="sys", user="diff", agent="security", review_id="review-123"
        )

        # claude-haiku-4-5: $1.00/1M input, $5.00/1M output (see
        # backend.economics.pricing.MODEL_PRICES) -- 1M in + 0.5M out =
        # $1.00 + $2.50 = $3.50.
        assert response.cost_usd == Decimal("3.500000")
        assert response.tokens_in == 1_000_000
        assert response.tokens_out == 500_000

        assert len(repository.inserted) == 1
        event = repository.inserted[0]
        assert event.event_type.value == "llm.call"
        assert event.review_id == "review-123"
        assert event.agent == "security"
        assert event.model == "claude-haiku-4-5"
        assert event.tokens_in == 1_000_000
        assert event.tokens_out == 500_000
        assert event.cost_usd == Decimal("3.500000")
        assert event.latency_ms is not None and event.latency_ms >= 0

    def test_no_review_id_means_no_event_is_emitted(self) -> None:
        """An ad hoc call with nothing to correlate against emits no event -- see module docstring."""
        repository = _FakeEventRepository()
        fake_client = _FakeAnthropicClient(responder=lambda **_: _fake_message())
        client = AnthropicLLMClient(
            settings=_settings(),
            anthropic_client=fake_client,
            budget_guard=BudgetGuard(repository, daily_cap_usd=Decimal("20")),
            event_repository=repository,
        )

        client.complete(system="sys", user="diff", agent="security", review_id=None)
        assert repository.inserted == []


class TestCompleteAsyncDoesNotBlockTheEventLoop:
    """complete_async must offload, not just declare itself async and block anyway."""

    async def test_a_slow_call_does_not_stall_a_concurrent_coroutine(self) -> None:
        slow_seconds = 0.3

        def slow_responder(**_: Any) -> Message:
            time.sleep(slow_seconds)  # a genuinely blocking, synchronous call
            return _fake_message("slow but done")

        repository = _FakeEventRepository()
        fake_client = _FakeAnthropicClient(responder=slow_responder)
        client = AnthropicLLMClient(
            settings=_settings(),
            anthropic_client=fake_client,
            budget_guard=BudgetGuard(repository, daily_cap_usd=Decimal("20")),
            event_repository=repository,
        )

        heartbeat_ticks = 0

        async def heartbeat() -> None:
            nonlocal heartbeat_ticks
            deadline = asyncio.get_event_loop().time() + slow_seconds
            while asyncio.get_event_loop().time() < deadline:
                heartbeat_ticks += 1
                await asyncio.sleep(0.01)

        response, _ = await asyncio.gather(
            client.complete_async(system="sys", user="diff", agent="security"),
            heartbeat(),
        )

        assert response.text == "slow but done"
        # If complete_async blocked the event loop (e.g. by calling
        # self.complete directly instead of via asyncio.to_thread), the
        # heartbeat coroutine would never get scheduled while the slow call
        # ran, and this would be 0 or close to it instead of the ~30 ticks
        # 0.3s / 0.01s implies.
        assert heartbeat_ticks > 5, (
            f"only {heartbeat_ticks} heartbeat ticks during the slow call -- "
            "looks like complete_async blocked the event loop"
        )

    async def test_complete_async_still_raises_circuit_open_error_paths_correctly(self) -> None:
        """The offload must not swallow or alter the exception raised by complete()."""

        def always_fails(**_: Any) -> Message:
            raise RuntimeError("down")

        repository = _FakeEventRepository()
        fake_client = _FakeAnthropicClient(responder=always_fails)
        client = AnthropicLLMClient(
            settings=_settings(llm_retry_max_attempts=1),
            anthropic_client=fake_client,
            budget_guard=BudgetGuard(repository, daily_cap_usd=Decimal("20")),
            event_repository=repository,
        )

        with pytest.raises(LLMCallFailedError):
            await client.complete_async(system="sys", user="diff", agent="security")


def test_circuit_open_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check that CircuitOpenError itself is treated as non-retryable end-to-end."""
    repository = _FakeEventRepository()

    def always_fails(**_: Any) -> Message:
        raise RuntimeError("down")

    fake_client = _FakeAnthropicClient(responder=always_fails)
    client = AnthropicLLMClient(
        settings=_settings(llm_retry_max_attempts=5, llm_circuit_breaker_failure_threshold=1),
        anthropic_client=fake_client,
        budget_guard=BudgetGuard(repository, daily_cap_usd=Decimal("20")),
        event_repository=repository,
    )

    with pytest.raises(LLMCallFailedError):
        client.complete(system="sys", user="diff", agent="security")
    assert fake_client.call_count == 1  # breaker tripped on the first failure

    calls_before = fake_client.call_count
    with pytest.raises(LLMCallFailedError):
        client.complete(system="sys", user="diff", agent="security")
    # Even with retry_max_attempts=5, a CircuitOpenError must not be
    # retried -- the client is not invoked again at all.
    assert fake_client.call_count == calls_before
