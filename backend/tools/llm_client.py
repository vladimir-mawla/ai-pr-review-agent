"""LLM client: wraps the Anthropic SDK with this project's reliability + cost layers.

Owns the one real outbound network call this milestone introduces -- a
completion request to Anthropic's Messages API -- and everything that must
surround it before it is safe to call from the request path:

1. RELIABILITY (reused, not hand-rolled). Every real Anthropic API call
   goes through the exact same composition ``backend.job_queue.redis_arq.
   RedisJobQueue`` and ``backend.database.repository.EventRepository`` use
   for their own outbound calls:

       retry (backend.reliability.retry.call_with_retry)
         -> circuit breaker (backend.reliability.circuit_breaker.CircuitBreaker)
           -> timeout (backend.reliability.timeout.run_with_timeout)
             -> the actual anthropic.Anthropic().messages.create(...) call

   A brand-new unprotected outbound call here would violate the exact
   DONE.html gate ("every outbound call has a timeout / circuit-breaker")
   two prior milestones (M6's own reliability layer, M7's L4-REJECT-then-
   fix for the events write) were held to -- see ``backend/reliability/``'s
   module docstrings for the full reasoning this class deliberately reuses
   rather than reinventing. Non-retryable exceptions extend the default set
   with the Anthropic SDK's own "this request itself is malformed/
   unauthorized, retrying can never help" exception family (bad request,
   auth, permission, not-found, unprocessable, conflict, request-too-large)
   -- mirroring ``backend.job_queue.redis_arq``'s own extension with
   ``CircuitOpenError`` for the identical reason.

   M8 L2 DEBUG addition (post-L4-VERIFY): a non-retryable provider error is
   re-raised by ``call_with_retry`` immediately, uncaught -- it never
   becomes a ``RetryExhaustedError``. Left alone, that meant a raw
   ``anthropic.AuthenticationError`` (an invalid/revoked API key) escaped
   this class entirely and crashed the whole orchestrator run, while a
   *missing* key (``LLMConfigurationError``, raised before any SDK call is
   even attempted) was already handled correctly and forced human review --
   an inconsistency proven by a real credential turning out to be rejected
   (see ``tests/integration/test_events_spine.py``'s
   ``test_real_orchestrator_run_produces_spans_and_a_decision_event`` and
   ``tests/unit/test_llm_client.py``'s
   ``TestNonRetryableProviderErrorsAreWrapped``). ``complete`` now catches
   ``anthropic.AnthropicError`` (the SDK's common base class covering its
   entire exception family, not just ``AuthenticationError``) around the
   retry/breaker/timeout call and re-raises it as this project's own
   ``LLMCallFailedError`` -- the same signal ``BudgetExceededError`` and
   ``LLMConfigurationError`` already give ``backend.orchestrator.nodes.
   security_node``, which is what lets that node force HITL instead of
   crashing, without ever importing ``anthropic`` itself.

2. BUDGETGUARD (hard block, not a warning). ``complete`` calls
   ``BudgetGuard.check_and_raise()`` as the FIRST thing it does -- before
   this class's own underlying Anthropic client is even constructed, let
   alone called. See ``backend.economics.budget`` for the guard itself and
   why spend is read from the real ``agent_events`` table, not an
   in-memory counter.

3. NOT BLOCKING THE EVENT LOOP FROM ASYNC CONTEXT. ``complete`` is a plain
   synchronous, blocking method -- correct for this milestone's one live
   call site (``backend.agents.security_agent.SecurityAgent.analyze``,
   called from ``backend.orchestrator.nodes.security_node``, which runs on
   a LangGraph-managed worker thread, never on an asyncio event loop --
   exactly the same situation ``backend.observability.events.emit_decision``
   is in for the orchestrator's other call sites). ``complete_async`` exists
   for a future caller that DOES run on an asyncio event loop (e.g. a
   webhook-triggered async review path): it offloads the identical
   synchronous work via ``asyncio.to_thread`` (asyncio's own default
   executor -- a bounded thread pool, not one thread per call), the exact
   pattern ``backend.observability.events.emit_decision_async`` and
   ``backend.job_queue.interface.enqueue_async`` both already established
   in this codebase to fix the identical defect class (a blocking call made
   directly, unawaited, from inside a coroutine stalls the entire shared
   event loop -- proven twice already in this project's own history, see
   ``.genesis/checkpoints/CURRENT.md``'s M7 REJECT-then-fix and its
   HIGH-PRIORITY Redis-enqueue follow-up). ``complete_async`` has no live
   call site yet, the same forward-looking-infrastructure category as M7's
   own ``emit_tool_call`` -- but it is directly tested
   (``tests/unit/test_llm_client.py``) to prove the offload mechanism
   itself actually works, not merely declared and left unverified.

4. COST ACCOUNTING + THE EVENTS SPINE. On a successful call, this class
   computes the call's USD cost (``backend.economics.pricing.
   compute_cost_usd``) from the response's real token usage and emits one
   ``llm.call`` event (``backend.observability.emit_llm_call``) -- M7's own
   live-call-site gap this milestone was explicitly told to close. Skipped
   when the caller passes no ``review_id`` (e.g. an ad hoc CLI invocation
   with nothing to correlate the event against) -- there is no review to
   attribute the spend to in that case, and BudgetGuard's own accounting
   query is not scoped by review anyway (see that module).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from anthropic import (
    Anthropic,
    AnthropicError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RequestTooLargeError,
    UnprocessableEntityError,
)
from anthropic.types import Message, TextBlock

from backend.core.settings import Settings, get_settings
from backend.database.repository import EventRepository
from backend.economics.budget import BudgetGuard
from backend.economics.pricing import compute_cost_usd
from backend.observability import emit_llm_call, get_event_repository
from backend.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    register,
)
from backend.reliability.retry import RetryExhaustedError, RetryPolicy, call_with_retry
from backend.reliability.timeout import TimeoutPolicy, run_with_timeout

# Name this client's circuit breaker is registered under -- independent of
# "redis_job_queue" (M6) and "events_db" (M7), what a future /health
# endpoint would look it up by.
_CIRCUIT_BREAKER_NAME = "anthropic_llm_client"

# The Anthropic SDK's own "this request itself is malformed/unauthorized/
# forbidden/nonexistent/unprocessable/conflicting/too-large" exception
# family -- retrying any of these can never help, since the request body/
# credentials/target that caused them do not change between attempts.
# Extends retry's own default (TypeError/ValueError/KeyError/AttributeError)
# plus CircuitOpenError, mirroring backend.job_queue.redis_arq's own
# extension. Deliberately NOT included here (left to the default retryable
# path): RateLimitError, ServiceUnavailableError, OverloadedError,
# InternalServerError, APIConnectionError/APITimeoutError,
# DeadlineExceededError -- every one of those means "the request itself was
# fine, the provider/network had a transient problem", which retrying (and,
# on repeated failure, the circuit breaker) can plausibly fix.
_NON_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    CircuitOpenError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    NotFoundError,
    UnprocessableEntityError,
    ConflictError,
    RequestTooLargeError,
)

# Generous enough for a multi-finding JSON response with rationale text per
# finding; small enough to bound one call's worst-case cost/latency. Not
# yet configurable via Settings -- no milestone requirement has asked for
# per-call output-length tuning, and a single fixed cap keeps this client's
# configuration surface no larger than it needs to be today.
_MAX_OUTPUT_TOKENS = 4096


class LLMConfigurationError(Exception):
    """Raised when a real Anthropic call is attempted with no usable API key.

    Only ever raised from the real (non-injected) client path -- every unit
    test in this project injects a fake ``anthropic_client``, which never
    reaches this check. See ``AnthropicLLMClient._client``.
    """


class LLMCallFailedError(Exception):
    """Wraps ``RetryExhaustedError``/``CircuitOpenError`` from the reliability layer.

    Mirrors ``backend.job_queue.interface.QueueUnavailableError``'s role:
    one exception type a caller can catch regardless of which of the two
    underlying reliability failures actually happened.
    """


@dataclass(frozen=True)
class LLMResponse:
    """One completed LLM call's result, with everything BudgetGuard/events accounting needs.

    Attributes:
        text: The concatenated text content of the model's response.
        model: The model id that was actually called.
        tokens_in: Input (prompt) token count, from the API's own reported usage.
        tokens_out: Output (completion) token count, from the API's own reported usage.
        cost_usd: This call's computed USD cost (``backend.economics.pricing.compute_cost_usd``).
        latency_ms: Wall-clock duration of the call, including any retries.
    """

    text: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    latency_ms: int


class LLMClientProtocol(Protocol):
    """The shape ``backend.agents.security_agent.SecurityAgent`` depends on.

    A ``Protocol`` (structural typing), not an ABC -- so a test can hand
    ``SecurityAgent`` a bare fake object satisfying this one method without
    inheriting from ``AnthropicLLMClient`` or constructing any part of the
    real Anthropic SDK, which is exactly what lets every unit test in this
    project pass without ``ANTHROPIC_API_KEY`` set.
    """

    def complete(
        self,
        *,
        system: str,
        user: str,
        agent: str,
        review_id: str | None = None,
    ) -> LLMResponse: ...


def _extract_text(message: Message) -> str:
    """Concatenate every text content block of an Anthropic Messages API response.

    A response's ``content`` is a list of typed blocks (a discriminated
    union on ``.type``); only ``TextBlock``s are relevant here (a plain
    completion request with no tool use configured should only ever
    produce those, but concatenating rather than indexing ``content[0]``
    is a cheap safety margin against a response with more than one).
    The explicit ``isinstance`` check (rather than comparing ``.type`` to
    the string ``"text"``) is what lets mypy narrow each block to
    ``TextBlock`` before accessing ``.text``.
    """
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "".join(parts)


class AnthropicLLMClient:
    """Wraps ``anthropic.Anthropic`` with retry/circuit-breaker/timeout, BudgetGuard, and cost accounting.

    Construction never requires ``ANTHROPIC_API_KEY`` to be set -- the real
    SDK client is built lazily, only inside ``_client()``, and only reached
    when ``anthropic_client`` was not injected. Every unit test in this
    project injects a fake ``anthropic_client`` (satisfying
    ``anthropic_client.messages.create(...)``), so this class can be
    freely constructed and exercised in CI with no credential at all;
    ``LLMConfigurationError`` is raised only if the real, non-injected path
    is actually reached with no key configured.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        anthropic_client: Anthropic | None = None,
        budget_guard: BudgetGuard | None = None,
        event_repository: EventRepository | None = None,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._injected_client = anthropic_client
        self._cached_real_client: Anthropic | None = None
        self._event_repository = event_repository
        self._budget_guard = budget_guard

        # M8 reliability policies, built once from Settings -- see module
        # docstring for why this is a THIRD, independent set from M6/M7's.
        self._retry_policy = RetryPolicy(
            max_attempts=self._settings.llm_retry_max_attempts,
            base_delay_seconds=self._settings.llm_retry_base_delay_seconds,
            max_delay_seconds=self._settings.llm_retry_max_delay_seconds,
        )
        self._timeout_policy = TimeoutPolicy(seconds=self._settings.llm_timeout_seconds)
        self._breaker = register(
            CircuitBreaker(
                CircuitBreakerConfig(
                    failure_threshold=self._settings.llm_circuit_breaker_failure_threshold,
                    reset_timeout_seconds=self._settings.llm_circuit_breaker_reset_timeout_seconds,
                ),
                name=_CIRCUIT_BREAKER_NAME,
            )
        )

    def _repository(self) -> EventRepository:
        if self._event_repository is not None:
            return self._event_repository
        return get_event_repository()

    def _guard(self) -> BudgetGuard:
        if self._budget_guard is None:
            self._budget_guard = BudgetGuard(
                self._repository(), daily_cap_usd=self._settings.budget_daily_cap_usd
            )
        return self._budget_guard

    def _client(self) -> Anthropic:
        """The underlying Anthropic SDK client: injected, or lazily real.

        Lazy so a key-less ``AnthropicLLMClient()`` (e.g. the one
        ``backend.orchestrator.nodes._get_security_agent`` lazily
        constructs as the production default) never fails merely by
        existing -- only an actual attempt to make a real call, with no
        injected client and no configured key, raises.
        """
        if self._injected_client is not None:
            return self._injected_client
        if self._cached_real_client is None:
            if not self._settings.anthropic_api_key:
                raise LLMConfigurationError(
                    "ANTHROPIC_API_KEY is not configured -- cannot make a real "
                    "Anthropic API call. Set it in .env for the live demo, or "
                    "inject a fake anthropic_client for tests."
                )
            # max_retries=0: this project's own retry/breaker/timeout
            # composition (below) is the single source of retry behavior --
            # leaving the SDK's own default (2) enabled too would silently
            # double-retry underneath it, confusing both latency and the
            # attempt counts this milestone's fault-injection tests assert on.
            self._cached_real_client = Anthropic(
                api_key=self._settings.anthropic_api_key, max_retries=0
            )
        return self._cached_real_client

    def complete(
        self,
        *,
        system: str,
        user: str,
        agent: str,
        review_id: str | None = None,
    ) -> LLMResponse:
        """Make one budget-gated, reliability-wrapped, cost-tracked LLM completion call.

        Args:
            system: The specialist's system prompt (see
                ``backend.prompts.registry.load_prompt``).
            user: The user-turn content -- the diff to analyze.
            agent: Which specialist is calling (e.g. "security"), used only
                to attribute the emitted ``llm.call`` event.
            review_id: Correlates the emitted event with a review run. If
                ``None`` (e.g. an ad hoc CLI invocation with no review to
                attribute to), no event is emitted -- see module docstring.

        Raises:
            ``backend.economics.budget.BudgetExceededError``: today's spend
                already meets the configured cap -- raised BEFORE this
                class's own underlying Anthropic client is constructed or
                touched at all.
            ``LLMConfigurationError``: no API key configured and no fake
                client injected.
            ``LLMCallFailedError``: every retry attempt failed, the circuit
                breaker was already open, or the provider rejected the
                request outright with a non-retryable error (bad API key,
                insufficient permissions, malformed request, etc. -- see
                ``_NON_RETRYABLE_EXCEPTIONS`` above). This last case is
                exactly the defect this docstring note exists to prevent a
                regression of: an invalid ``ANTHROPIC_API_KEY`` raises
                ``anthropic.AuthenticationError`` from deep inside
                ``call_with_retry`` (re-raised immediately, uncaught, since
                it is in ``_NON_RETRYABLE_EXCEPTIONS`` -- see
                ``backend.reliability.retry.call_with_retry``'s docstring);
                that is a *vendor-specific* exception type this class must
                never let escape past this method, or every caller up to
                and including ``backend.orchestrator.nodes.security_node``
                would need to know about ``anthropic`` -- exactly the
                layering violation ``backend.tools.llm_client`` exists to
                prevent (see this module's own docstring). Catching
                ``anthropic.AnthropicError`` (the SDK's common base for its
                entire exception family) below and wrapping it into this
                project's own ``LLMCallFailedError`` is what keeps that
                promise regardless of which specific provider failure mode
                is responsible.
        """
        # HARD BLOCK -- see backend.economics.budget's module docstring for
        # why this happens before self._client() is even called.
        self._guard().check_and_raise()

        client = self._client()

        def _make_request() -> Message:
            return client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=_MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
            )

        def _bounded_request() -> Message:
            return run_with_timeout(_make_request, policy=self._timeout_policy)

        start = time.monotonic()
        try:
            message = call_with_retry(
                lambda: self._breaker.call(_bounded_request),
                policy=self._retry_policy,
                non_retryable_exceptions=_NON_RETRYABLE_EXCEPTIONS,
            )
        except (RetryExhaustedError, CircuitOpenError) as exc:
            raise LLMCallFailedError(
                f"LLM call failed (retries exhausted or circuit breaker open): {exc}"
            ) from exc
        except AnthropicError as exc:
            # A non-retryable provider error (see _NON_RETRYABLE_EXCEPTIONS)
            # is re-raised immediately by call_with_retry -- it never goes
            # through RetryExhaustedError, so it is NOT caught above. Left
            # unhandled here, a raw anthropic.AuthenticationError (etc.)
            # would propagate all the way out to
            # backend.orchestrator.nodes.security_node, which only catches
            # this project's own exception types -- crashing the whole
            # orchestrator run instead of forcing human review the same way
            # a BudgetExceededError or missing API key already does. Wrap it
            # here, at this class's boundary, so no caller -- including the
            # orchestrator -- ever needs to know the vendor SDK exists.
            raise LLMCallFailedError(
                f"LLM call failed (non-retryable provider error): "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        tokens_in = message.usage.input_tokens
        tokens_out = message.usage.output_tokens
        cost_usd = compute_cost_usd(
            self._settings.anthropic_model, tokens_in=tokens_in, tokens_out=tokens_out
        )

        if review_id is not None:
            # M8: emit_llm_call's first live call site -- see module
            # docstring point 4. Failure of the write itself is caught and
            # logged inside emit_llm_call/_emit (backend.observability.events'
            # own failure policy); it never raises past this point and never
            # prevents the LLMResponse below from being returned.
            emit_llm_call(
                self._repository(),
                review_id,
                agent,
                model=self._settings.anthropic_model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
            )

        return LLMResponse(
            text=_extract_text(message),
            model=self._settings.anthropic_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )

    async def complete_async(
        self,
        *,
        system: str,
        user: str,
        agent: str,
        review_id: str | None = None,
    ) -> LLMResponse:
        """``complete``, off the calling coroutine's own thread. See module docstring point 3.

        No live call site yet (forward-looking, same category as M7's
        ``emit_tool_call``) -- directly tested to prove the offload actually
        works, not merely declared.
        """
        return await asyncio.to_thread(
            self.complete, system=system, user=user, agent=agent, review_id=review_id
        )
