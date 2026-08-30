# CURRENT
- active_loop: none (between milestones)
- target: M8
- iteration: 3
- last_gate: L2 DEBUG (2026-08-31) -- fixed a defect an invalid (rejected)
  `ANTHROPIC_API_KEY` exposed: `anthropic.AuthenticationError` was not in
  `backend.orchestrator.nodes._SECURITY_INFRASTRUCTURE_FAILURE_EXCEPTIONS`
  and was never wrapped into this project's own `LLMCallFailedError`, so it
  propagated raw out of `AnthropicLLMClient.complete` and crashed the whole
  orchestrator run instead of forcing human review -- while a *missing* key
  (`LLMConfigurationError`) was already handled correctly. Found when a real
  credential (present in `.env`, meant to unblock M8's demo) turned out to
  be rejected by Anthropic with a real 401 `authentication_error`, confirmed
  by raw curl. Fixed at `AnthropicLLMClient.complete`'s boundary (`backend/
  tools/llm_client.py`): catches `anthropic.AnthropicError` (the SDK's
  common base, covering the whole non-retryable family -- auth, permission,
  bad request, not-found, conflict, unprocessable, request-too-large -- not
  just `AuthenticationError`) around the retry/breaker/timeout call and
  re-raises it as `LLMCallFailedError`, so `backend.orchestrator.nodes`
  still never imports `anthropic` and `security_node`'s existing
  `_SECURITY_INFRASTRUCTURE_FAILURE_EXCEPTIONS` catch (unchanged) now
  reaches it. Non-retryable set also gained `RequestTooLargeError` (413,
  same "retrying the same body can never help" family, previously missed).
  Transient provider failures (`RateLimitError`, `ServiceUnavailableError`,
  `OverloadedError`, `InternalServerError`, `APIConnectionError`/
  `APITimeoutError`, `DeadlineExceededError`) are deliberately left retryable
  (M6's retry/breaker already handles them); proven that a bad key fails
  fast on attempt 1 (never retried 3 times), while a transient `RuntimeError`
  is still retried to the configured attempt count. Regression proof: the
  previously-failing `tests/integration/test_events_spine.py::
  test_real_orchestrator_run_produces_spans_and_a_decision_event` now
  passes; new `tests/unit/test_llm_client.py::
  TestNonRetryableProviderErrorsAreWrapped` (5 tests, real
  `anthropic.AuthenticationError`/`PermissionDeniedError`/`BadRequestError`
  constructed with no network call) and new `tests/integration/
  test_orchestrator_fanout.py::
  TestRealSecurityAgentAuthenticationFailureForcesHITL` (1 test, a real
  `SecurityAgent`+`AnthropicLLMClient` wired through the compiled graph)
  both proven to fail against the pre-fix code and pass after -- pasted
  output in this session's final report. All four gates green after the
  fix (`ruff check .`; `mypy --strict backend/` 60 source files; `pytest -v`
  253 passed + 1 failed (`test_hybrid_retrieval.py`, uncommitted M9 hybrid-
  retrieval WIP, unrelated to this fix and left untouched); `lint-imports
  --config .importlinter` 2 kept/0 broken), Redis (6380) and Postgres
  (5433) up. `test_security_agent_live.py` now PASSES for real -- the
  credential in `.env` was rotated to a valid one partway through this
  session (curl now returns 200, not 401), so M8's demo command is
  unblocked; this was not this session's fix and is noted only as a
  same-session environment change.
- next_action: re-run PLAN.md's M8 demo command for real now that
  `ANTHROPIC_API_KEY` is valid, then L4 VERIFY on M8 (separate session) --
  re-verify this auth-error-handling fix alongside the three from the prior
  L2 DEBUG pass below
- model: claude-sonnet-5
- tokens_used: 0
- tokens_budget: 50000
- skills_loaded: []

## M8 L2 DEBUG (2026-08-30) -- 3 findings from an independent L4 VERIFY session, all fixed

L4 VERIFY reviewed the M8 L1 BUILD above and raised three findings, none of
which required rejecting the milestone outright but all of which needed a
real fix before re-verification. This L2 DEBUG session fixed all three.

**Finding 1 (highest priority) -- `EventRepository.sum_llm_cost_since` had
no upper bound, so a future-dated row counted toward "today"'s spend.**
This had already caused two independent false `BudgetExceededError`
failures: the M8 builder session's own 2030-dated fixture rows ($2119.000446
of $20), and L4 VERIFY's own, INDEPENDENTLY reproduced failure from its own
2099-dated boundary-test fixture rows ($40.00 of $20) -- proving the earlier
"fix" (re-pinning `tests/integration/test_budget_guard_events.py`'s fixture
day to the past, see `## M8 history` below) had only relocated the symptom,
never touched the actual defect. Fixed for real this time: the query itself
now takes a genuine half-open `[day_start, day_start + 1 day)` window
(`WHERE event_type = %s AND ts >= %s AND ts < %s`), and the method was
renamed `sum_llm_cost_for_day` (from `sum_llm_cost_since`) since the old
name no longer described what it does -- every call site
(`BudgetGuard.current_spend_usd`, both fake-repository test doubles in
`tests/unit/test_budget_guard.py`/`tests/unit/test_llm_client.py`) updated
to match. DESIGN DECISION: a future-dated row is IGNORED for the day it
doesn't belong to (excluded by the upper bound), not counted and not
raised/logged as a separate anomaly signal inline -- `BudgetGuard.
check_and_raise()` runs on this milestone's hot path (once per LLM call),
so the query stays the single, cheap, already-necessary bounded scan it was
before rather than growing a second unbounded "scan for anomalies" query
alongside it; surfacing future-dated rows to an operator is left as
follow-up infrastructure (see Deferred), not built here since nothing has
asked for it yet. Proven, not just asserted: `tests/integration/
test_budget_guard_events.py` gained `TestFutureDatedRowsAreExcluded` (a row
one second into the next day, and a row dated 10 years ahead reproducing
the exact $2119.000446 repro number, both proven not to change
`current_spend_usd()` at all) and `TestExactBoundaryInstants` (a row at
exactly `day_start` counts in full; a row at exactly `day_start + 1 day`
does not count at all) -- and the fix is proven to actually catch the real
bug: the query's upper bound was temporarily reverted (keeping the new name
so the revert isolates only the SQL bound, not a naming mismatch), all 3 of
the new tests failed against the reverted query with real Postgres output
(`AssertionError: spend changed from 1059.500100 to 3178.500546 after
inserting a far-future-dated row`, etc.), then the fix was restored and all
8 tests in the file passed again (Postgres recycled via `docker compose
down && up` between runs, matching M8 L1 BUILD's own precedent, so no
polluted row from the temporary revert leaked into the passing run or the
later live demo attempt) -- full pasted output in this session's final
report.

**Finding 2 -- `backend/orchestrator/nodes.py`'s `security_node` caught
`BudgetExceededError` (and any other infrastructure failure) with a bare
`except Exception` and turned it into an EMPTY findings list --
indistinguishable from "the model ran and found nothing to flag".** Masked
today only because the three remaining stub specialists keep
`overall_confidence` below the HITL threshold regardless; at M10, once they
are real, a budget block would silently read as a clean security review.
Fixed by narrowing the catch to exactly
`_SECURITY_INFRASTRUCTURE_FAILURE_EXCEPTIONS` (`BudgetExceededError`,
`LLMConfigurationError`, `LLMCallFailedError` -- the same set `SecurityAgent
.analyze`'s own docstring names as "unable to even attempt an analysis")
and, on any of them, returning ONE synthetic CRITICAL/confidence-0.000
`Finding` (new `backend.agents.security_agent.
infrastructure_failure_fallback_finding`) instead of `[]` -- reusing the
EXACT mechanism `SecurityAgent`'s own total-parse-failure fallback already
uses to force human review (`backend.hitl.queue.has_critical_finding`'s
unconditional CRITICAL-forces-HITL routing), per this session's own
instruction to reuse rather than invent a second mechanism. The failure is
still recorded in `node_errors` either way. A genuine programming bug
(anything NOT in that narrow tuple -- a `TypeError`/`KeyError` standing in
for a real defect in our own code) is deliberately NOT caught and
propagates uncaught out of the node, consistent with how M7 narrowed the
events failure policy to stop swallowing `IntegrityError` alongside real
outages. Checked the other three stub specialists (`quality_node`,
`tests_node`, `docs_node`): none of them has a bare `except Exception` at
all -- only `SimulatedNodeCrashError` (re-raised) and `AgentExecutionError`
(isolated) are caught in any of the three -- so no equivalent fix was
needed there; this is a `security_node`-only pattern (from its own M8 L1
BUILD addition), not a repo-wide one. New `tests/unit/
test_security_node_infrastructure_failures.py` (9 tests) proves: a
`BudgetExceededError` (and `LLMConfigurationError`/`LLMCallFailedError`)
during the security node returns a forced-HITL CRITICAL finding, is
recorded in `node_errors`, and actually routes an otherwise-confident
review to `QUEUED_FOR_HITL` through the real `route_review` function; a
genuine empty-findings result from the agent (no exception) still returns
`{"findings": [], "node_errors": {}}` and does not by itself force HITL for
an otherwise-confident review; a `TypeError`/`KeyError` from the agent
propagates out of `security_node` uncaught.

**Finding 3 -- `.genesis/context-graph.json`'s `budget-guard-hard-blocks`
invariant described a per-node check that was never built.** The real,
built design centralizes the check once, inside `AnthropicLLMClient.
complete`/`complete_async`, guarding every caller uniformly -- L4 VERIFY
ruled this design better (no caller can forget it; covers both sync and
async paths) and recommended rewording rather than requiring the literal
per-node duplication. Reworded to L4 VERIFY's own suggested wording (the
real, verifiable rule): "AnthropicLLMClient.complete/complete_async call
BudgetGuard.check_and_raise() as the first statement, before the underlying
Anthropic client is constructed or invoked; verify by grepping every call
site of `Anthropic(` and `messages.create` and confirming each is reachable
only through complete." Verified checkable: `grep -rn "Anthropic("
backend/` and `grep -rn "messages\.create" backend/` each find exactly one
real (non-comment/non-docstring) call site, both inside
`AnthropicLLMClient.complete`, with `self._guard().check_and_raise()` as
that method's literal first statement. All 4 hand-written invariants
(`inward-only-dependencies`, `hmac-verified-before-any-work`,
`budget-guard-hard-blocks`, `events-table-append-only`) confirmed present
after the edit -- graphizer wipes this block on every run, so this was
edited by hand and verified, not regenerated. `.genesis/DONE.html` section
2's BudgetGuard gate text ("BudgetGuard hard-blocks the next LLM call once
the daily cap is exceeded") was checked and needs NO change -- it is
already implementation-agnostic (it never described the per-node
architecture the invariant wrongly did), so there was nothing there to
reconcile.

Gate results, regression-proof output, and the demo command's real output
are all in this session's final report.

## M8 L2 DEBUG (2026-08-31) -- invalid API key crashed the run instead of forcing HITL

A follow-up L2 DEBUG pass on the same milestone, found while trying to
unblock M8's demo command with a real credential: `ANTHROPIC_API_KEY` was
present in `.env` but Anthropic rejected it outright (a real 401
`authentication_error`, confirmed by raw curl, not a test artifact).

**The defect.** `backend.tools.llm_client.AnthropicLLMClient.complete`
composes retry -> circuit breaker -> timeout around the real
`anthropic.Anthropic().messages.create(...)` call, with
`anthropic.AuthenticationError` (and its sibling non-retryable family --
`BadRequestError`, `PermissionDeniedError`, `NotFoundError`,
`UnprocessableEntityError`, `ConflictError`) already correctly listed in
`_NON_RETRYABLE_EXCEPTIONS`, so `backend.reliability.retry.call_with_retry`
re-raises it immediately on the first attempt, with no sleep and no further
attempts -- exactly right, a bad key must never be retried 3 times. But
`call_with_retry` re-raises it *uncaught*, as the raw vendor exception --
it never becomes a `RetryExhaustedError`. `complete`'s own
`except (RetryExhaustedError, CircuitOpenError)` clause therefore never
caught it, so the raw `anthropic.AuthenticationError` escaped
`AnthropicLLMClient` entirely, propagated through `SecurityAgent.analyze`
(which deliberately does not catch infrastructure failures -- see that
class's own docstring), and crashed `backend.orchestrator.nodes.
security_node`, since that node's `_SECURITY_INFRASTRUCTURE_FAILURE_
EXCEPTIONS` catch only lists this project's own exception types
(`BudgetExceededError`, `LLMConfigurationError`, `LLMCallFailedError`) --
crashing the whole graph run instead of forcing human review. A *missing*
key (`LLMConfigurationError`, raised before any SDK call is even attempted)
was already handled correctly by the exact same node; an authentication
failure is precisely the same class of "the analysis never actually ran"
as a budget block or missing config, but was NOT treated the same. Confirmed
failing first: `tests/integration/test_events_spine.py::
test_real_orchestrator_run_produces_spans_and_a_decision_event` raised a
raw, uncaught `anthropic.AuthenticationError` (full traceback in this
session's final report).

**The fix -- Option A, at the client boundary, not the node.** Widening
`security_node`'s caught tuple (Option B) would have required either
importing `anthropic` into `backend/orchestrator/` (a real layering
violation this project has otherwise kept clean -- `nodes.py` has never
imported the vendor SDK) or duplicating the SDK's exception list a second
time outside `backend/tools/llm_client.py`, the one module whose whole job
is to be the whitelisted vendor-SDK boundary (see that module's own
docstring). Instead, `complete` now also catches `anthropic.AnthropicError`
-- the SDK's common base class, covering its *entire* exception family, not
merely `AuthenticationError` -- around the retry/breaker/timeout call, and
re-raises it as this project's own `LLMCallFailedError`. This costs nothing
extra at the non-retryable path (those exceptions were already correctly
failing fast) and means `security_node`'s existing, unchanged
`_SECURITY_INFRASTRUCTURE_FAILURE_EXCEPTIONS` catch now reaches it, giving
exactly the same forced-HITL synthetic CRITICAL/confidence-0.000 `Finding`
a `BudgetExceededError` already produces. `_NON_RETRYABLE_EXCEPTIONS` also
gained `RequestTooLargeError` (413 -- same "retrying the identical
oversized body can never help" family as the six already listed, simply
missed before). Transient provider failures (`RateLimitError`,
`ServiceUnavailableError`, `OverloadedError`, `InternalServerError`,
`APIConnectionError`/`APITimeoutError`, `DeadlineExceededError`) are
deliberately left OUT of the non-retryable set -- M6's retry/breaker
composition already handles those correctly (retry, then trip the breaker
on sustained failure), and they are not the "the request itself can never
succeed" family a bad key is.

**Proof.** `tests/unit/test_llm_client.py`'s new
`TestNonRetryableProviderErrorsAreWrapped` (5 tests) constructs REAL
`anthropic.AuthenticationError`/`PermissionDeniedError`/`BadRequestError`
instances with a real (but network-free) `httpx2.Request`/`Response` pair,
proving each comes out of `complete` as `LLMCallFailedError` (vendor
exception still reachable via `__cause__`), that an `AuthenticationError`
invokes the fake underlying client exactly once (not retried, even with
`max_attempts=5`), and -- the control case -- that a genuinely transient
`RuntimeError` IS retried to the full configured attempt count (3 of 3),
proving the fast-fail is specific to non-retryable provider errors, not a
global regression. `tests/integration/test_orchestrator_fanout.py`'s new
`TestRealSecurityAgentAuthenticationFailureForcesHITL` wires a real
`SecurityAgent` around a real `AnthropicLLMClient` (only the innermost
`anthropic_client` faked, to inject the same real, network-free
`AuthenticationError`) through the actual compiled LangGraph graph, and
proves: the orchestrator run completes (no exception escapes `engine.run`);
the synthetic CRITICAL/confidence-0.000 finding is present; `node_errors`
records the security failure; and `route_review` -- both via the real
`Review.status` the aggregator produced and recomputed directly -- returns
`QUEUED_FOR_HITL`, never able to average out into an auto-post even
alongside a confident, unrelated specialist finding. Both new test files'
relevant tests, and the pre-existing `test_events_spine.py` test, were
proven to FAIL against the pre-fix code (the `except AnthropicError`
clause temporarily removed, real tracebacks pasted in this session's final
report) and PASS after the fix was restored.

**Gates.** `ruff check .` clean; `mypy --strict backend/` clean on 60
source files; `pytest -v` 253 passed, 1 failed
(`test_hybrid_retrieval.py::TestVectorSearchFindsWhatKeywordMisses::
test_synonym_chunk_found_by_vector_even_though_fulltext_matches_nothing` --
uncommitted M9 hybrid-retrieval work-in-progress, unrelated to this fix,
left completely untouched per this session's explicit instructions);
`lint-imports --config .importlinter` 2 kept/0 broken. Both Redis (6380)
and Postgres (5433) were up for the whole run. Same-session environment
change, NOT this fix: `tests/integration/test_security_agent_live.py` now
PASSES for real -- the `.env` credential was rotated to a valid one
partway through this session (raw curl now returns 200, previously 401),
so M8's demo command is now unblocked for a follow-up run.

Files touched: `backend/tools/llm_client.py` (the fix),
`tests/unit/test_llm_client.py` (5 new tests),
`tests/integration/test_orchestrator_fanout.py` (1 new test class). No
change to `backend/orchestrator/nodes.py`, `backend/agents/security_agent.py`,
or any of the M9 hybrid-retrieval work-in-progress files (`.env.example`,
`backend/core/settings.py`, `docker-compose.yml`, `pyproject.toml`,
`backend/memory/{embedder,context_retriever,tiger_client}.py`,
`migrations/`, `scripts/seed_code_chunks.py`,
`tests/integration/test_hybrid_retrieval.py`, `tests/unit/test_embedder.py`
-- all confirmed still exactly as found via `git status --short` before
committing).

## M8 history (kept for context; superseded by the two lines above)

**L1 BUILD.** First real, LLM-backed specialist agent plus the cost
controls around it. `backend/tools/llm_client.py`'s `AnthropicLLMClient`
wraps `anthropic.Anthropic().messages.create(...)` (model configurable via
`Settings.anthropic_model`, default `claude-haiku-4-5` per this project's
approved driver-model decision -- never Opus) in the exact retry ->
circuit-breaker -> timeout composition `RedisJobQueue`/`EventRepository`
already established, reused rather than hand-rolled. `backend.economics.
budget.BudgetGuard.check_and_raise()` runs first inside `complete()`,
before the underlying Anthropic client is even constructed, and reads real
spend via a new `EventRepository.sum_llm_cost_since` query against
`agent_events`'s real `cost_usd` column -- never an in-memory counter. On
success, `emit_llm_call` gets its first live call site (M7 built it with no
caller and disclosed that as M8's job). A tolerant parser
(`backend.agents.response_parsing`) survives real-model drift (key drift,
markdown fences, prose-then-JSON, a bare list); total parse failure returns
one synthetic CRITICAL/confidence-0.000 `Finding` (forces
`has_critical_finding`'s unconditional HITL routing) rather than crashing
or silently dropping the specialist's contribution. `security_node`
(`backend.orchestrator.nodes`) now delegates to a real, lazily-constructed
`SecurityAgent` by default, with a test-only override hook mirroring this
module's own pre-existing `arm_crash`/`arm_agent_error` pattern -- the
other three specialists remain M4's canned stubs, per this milestone's
explicit scope.

**A real bug found and fixed during this same L1 BUILD session (not
deferred to L4 VERIFY):** the parser could not distinguish a genuinely
empty `findings: []` list (a clean diff -- valid) from "every item failed
validation" (untrustworthy output -- should raise) -- both collapsed to an
empty `findings` list, so a clean diff was being misrouted through the
forced-HITL CRITICAL fallback path every time. Fixed by only raising
`ResponseParseError` when the extracted items list was non-empty yet
nothing in it survived validation; proven by two tests that previously
failed against the bug (`test_empty_findings_list_is_valid_not_an_error`,
`test_empty_findings_list_is_a_real_empty_list_not_a_fallback`), now
passing.

**A second real bug found and fixed, this one with a genuine
data-hygiene consequence** (SEE THE `## Resolved` SECTION ABOVE -- this
paragraph is kept for historical context, but this "fix" turned out to be
incomplete: it re-pinned the symptom, not the underlying query defect,
which an independent L4 VERIFY session then re-triggered with its own
2099-dated fixture rows, and this session's L2 DEBUG loop actually fixed):
the integration test proving BudgetGuard reads
real spend from `agent_events` originally pinned its fixture rows to a
FAR-FUTURE day (2030-06-15) for test isolation. This was backwards --
`sum_llm_cost_since` correctly has NO upper bound (a real production
`llm.call` is never timestamped in the future), so a future-dated fixture
row is still `>= today's real midnight` and silently polluted the *live*
BudgetGuard's actual accounting: running the M8 demo CLI immediately
afterward raised `BudgetExceededError: spent $2119.000446 of $20 cap` --
the sum of every future-pinned fixture row this session's own test suite
had written, not a real defect in production code. Fixed (INCOMPLETELY --
see above) by re-pinning the
fixture day to 2020-06-15 (safely in the past, unconditionally excluded
from any real "since today's midnight" query for the life of this test
suite) and, since the append-only table cannot be cleaned up any other way
by application code, recycling `docker compose down && up` (no volume, so
this wipes the polluted local dev Postgres) before the final gate re-run.
Confirmed clean afterward: real "today" `llm.call` spend was $0.000123
(from the gate run itself), and the demo CLI's failure changed from a
spurious `BudgetExceededError` to the correct `LLMConfigurationError` for
the actually-missing credential. Flagged for L4 VERIFY as a
test-design-only finding (production code was never wrong) -- see
`tests/integration/test_budget_guard_events.py`'s own ISOLATION note.

**Live demo: BLOCKED -- awaiting credential**, not fabricated. See the
header fields above and this session's final report for the full,
real CLI traceback proving exactly where it stops (a clear
`LLMConfigurationError` at the point a real Anthropic API call would be
made, with the budget/parsing/wiring machinery upstream of it all
verified working via the fixed test suite).

All four gates plus the freeze-boundary disclosures are in this session's
full report; see `## M8 Build Summary` below for the complete account.

## M7 history (kept for context; superseded by the two lines above)

**The REJECT.** An independent L4 VERIFY session rejected M7's L1 BUILD for a
real reliability defect, not a nitpick: `EventRepository.insert_event`
(`backend/database/repository.py`) opened a synchronous `psycopg.connect(...,
connect_timeout=2)` and was called directly (never awaited, never offloaded)
from `async def receive_webhook` (`backend/webhook_receiver/router.py`) --
`connect_timeout` bounds only the TCP handshake, not query execution or
lock-wait time, and no `statement_timeout` or circuit breaker existed at all.
L4 VERIFY proved this empirically: with an admin session holding
`LOCK TABLE agent_events IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(6)`, three
concurrent, independent webhook POSTs against a live uvicorn each took ~4.4s
instead of the normal sub-10ms -- a stalled events write serialised every
other in-flight request, violating `.genesis/DONE.html` section 2's "every
outbound call has a timeout / circuit-breaker" gate (the exact gate M6 was
built to satisfy for the Redis path).

**The fix (blocking).** Three changes, composed:
1. `backend/observability/events.py` gained `emit_decision_async`, the ONE
   function `backend/webhook_receiver/router.py`'s `async def receive_webhook`
   now `await`s instead of calling the plain synchronous `emit_decision`
   directly. It runs the identical write via `asyncio.to_thread` (asyncio's
   own default executor -- a bounded thread pool, not one thread per call),
   so only the calling request's own coroutine waits, not the whole event
   loop every other in-flight request also depends on. The orchestrator's
   call sites (`backend/orchestrator/nodes.py`) did not need this change --
   they already run on a LangGraph-managed worker thread for a sync graph,
   never on an asyncio event loop.
2. `backend/database/repository.py`'s `EventRepository` now sets Postgres's
   own `statement_timeout` GUC (via libpq's `options` connection parameter,
   new `statement_timeout_ms` constructor arg, default 2000ms) on every
   connection it opens -- a real query-level bound that covers lock-wait
   time, which `connect_timeout` provably does not.
3. `EventRepository.insert_event` now runs through a per-instance
   `backend.reliability.circuit_breaker.CircuitBreaker` (new
   `circuit_breaker_failure_threshold`/`circuit_breaker_reset_timeout_seconds`
   constructor args, and matching `Settings.events_*` fields, independent of
   the pre-existing M6 knobs that guard Redis), mirroring
   `RedisJobQueue`'s own composition pattern instead of hand-rolling
   something new -- a persistently down/slow events store now fails fast
   instead of being retried at full connect-timeout cost on every request.
   `backend/observability/events.py`'s `_emit` was extended to also swallow
   `CircuitOpenError`, alongside the pre-existing `psycopg.Error`/`OSError`.

**Proof.** `tests/integration/test_events_spine.py::
TestConcurrentWebhookWritesAreNotSerializedByALockedEventsTable` reproduces
the exact mechanism (an ACCESS EXCLUSIVE lock held during concurrent webhook
POSTs, driven in-process via `httpx.ASGITransport` on one event loop -- the
same single-event-loop-thread concurrency model a real uvicorn worker uses)
and is proven to fail against the pre-fix router (temporarily reverted:
latencies `[1.22, 4.01, 2.44, 3.66]`s, clearly serialized) and pass against
the fix (`[1.22, 1.22, 1.22, 1.22]`s, genuinely concurrent). A live-uvicorn
manual repro against the SAME experiment L4 VERIFY used (real
`docker compose up -d postgres`, a real admin session holding the table
lock, three real concurrent `scripts/send_signed_webhook.py` POSTs) measured
~4.76s/request before the fix (matching L4's ~4.4s finding) and ~2.26s/request
after (bounded by `statement_timeout`, not serialized) -- full numbers in this
session's final report.

**Also fixed (non-blocking findings from the same L4 review):**
- TRUNCATE bypassed the append-only trigger: PostgreSQL never fires a
  row-level (`FOR EACH ROW`) trigger for TRUNCATE, so the pre-existing
  `agent_events_no_update`/`agent_events_no_delete` pair did nothing to stop
  it -- L4 VERIFY demonstrated a superuser `TRUNCATE agent_events;` silently
  wiping all 94 rows, no exception. `backend/database/migrations/
  0001_agent_events.sql` gained a statement-level (`FOR EACH STATEMENT`)
  `agent_events_no_truncate` `BEFORE TRUNCATE` trigger, applied in place
  (the migration is idempotent and this project's local Postgres has no
  volume, so there is no deployed state to preserve). Proven: `TRUNCATE
  agent_events;` as the "postgres" superuser now raises `agent_events is
  append-only: TRUNCATE is not permitted`, exit code 1, row count unchanged.
  The migration's misleading comment claiming the row-level trigger pair
  holds "regardless of which role issues it" (false for TRUNCATE) was
  corrected.
- Narrowed `backend/observability/events.py`'s exception swallow: `_emit`
  used to catch bare `(psycopg.Error, OSError)`, which also silently
  swallowed `psycopg.errors.IntegrityError` (e.g. a CHECK-constraint
  violation) -- our own bug, not an availability failure.
  `psycopg.errors.IntegrityError` is now re-raised before the broad except
  runs. New `tests/unit/test_events_failure_policy.py` proves both directions
  (availability failures still swallowed; IntegrityError/CheckViolation now
  propagate) and is proven to fail against the old broad except (temporarily
  reverted, ran, captured 3 real failures, restored, reran, captured all
  passing).
- The M7 demo command didn't work verbatim in a clean shell: `.env` is read
  by pydantic-settings but never exported, so a bare `psql "$DATABASE_URL"`
  after only `python scripts/run_fixture_review.py` sees an empty string.
  `.genesis/PLAN.md`'s M7 demo command now inlines
  `set -a && source .env && set +a` before the `psql` step -- verified in a
  clean shell via `env -i PATH=... HOME=... bash -c '...'`.
- `.genesis/PLAN.md`'s demo SQL used bare `ORDER BY ts` with no `id`
  tiebreak, so same-timestamp rows had undefined order; changed to
  `ORDER BY ts ASC, id ASC`, matching `EventRepository.
  fetch_events_for_review`'s own tiebreak.

All four gates (`ruff check .`, `mypy --strict backend/`, `pytest -v` -- 160
passed, both Redis and Postgres integration tests ran for real, none
skipped -- `lint-imports --config .importlinter`) plus PLAN.md's (fixed) M7
demo command were re-run after the fix; see this session's final report for
the full pasted output and exit codes.

**Outcome:** A second, independent L4 VERIFY session re-ran all four gates
plus PLAN.md's exact (fixed) M7 demo command against the fixed code,
reproduced the concurrency numbers itself rather than trusting this
session's report (~2.03s per request, ~2.09s total wall time for three
genuinely concurrent webhook requests -- not stacked/serialized),
independently proved the event loop never stalls even with its executor
saturated (40 event emissions completed in 3 FIFO batches against a
deliberately tiny thread pool while a concurrent heartbeat coroutine ticked
61/61 times with no gaps), and **APPROVEd** M7 with no blocking defects.
The full suite passed with 160 tests. M7 is now DONE; see
`.genesis/PLAN.md`'s Progress entry and
`.genesis/explanations/2026-08-30-explanation-m7.html` for the full account
of the REJECT-then-fix cycle.

## M5 history (kept for context; superseded by the two lines above)
- last_gate (superseded): L4 VERIFY REJECTED M5 (separate session, real safety bug) -- this L2 DEBUG session applied the approved fix; a second, independent L4 VERIFY session then re-ran everything against the fix and APPROVEd M5.
- last_action: L4 VERIFY REJECTED M5's original L1 BUILD for a real safety bug: `backend.agents.contracts.dedupe_findings` keyed on `(file_path, line_start)` and kept the highest-CONFIDENCE finding with severity playing no role at all, while `backend.hitl.queue.has_critical_finding` only ever inspects the POST-dedupe list. Verified repro: a SECURITY/CRITICAL finding (confidence 0.751) and a DOCS/INFO finding (confidence 0.752) colliding on the same file+line caused dedupe to keep the INFO finding and drop the CRITICAL one; `route_review` then saw no CRITICAL finding and returned POSTED, auto-posting a review whose real CRITICAL finding had been silently discarded -- violating the project's invariant that a CRITICAL finding always requires human review. This L2 DEBUG session applied the user-approved fix: `_is_better` in `backend/agents/contracts.py` now orders SEVERITY FIRST (via a new explicit `SEVERITY_RANK` map added to `backend/models/enums.py` -- deliberately not relying on `Severity`'s enum declaration order or member name, per the user's instruction), and only falls back to confidence, then the pre-existing `AGENT_PRECEDENCE`/category/rationale tie-breaks, within the same severity. `backend.models.findings.Finding.__lt__` was also refactored to use the same `SEVERITY_RANK` map instead of its own locally-duplicated severity-order dict, removing a second place the ranking could have drifted from the fix. Every docstring/comment claiming dedupe "keeps highest confidence" was rewritten (module docstring and `_is_better`'s docstring in `backend/agents/contracts.py`, `aggregate_node`'s docstring in `backend/orchestrator/nodes.py`, `tests/unit/test_aggregator.py`'s module docstring, `.genesis/DONE.html` section 2's M5 gate, `.genesis/PLAN.md`'s M5 outcome/success-criteria) to state the real severity-first rule. Added a true end-to-end regression test (`tests/unit/test_aggregator.py::TestDedupeAndRoutingInteraction::test_end_to_end_critical_survives_dedupe_and_forces_hitl`) that builds the exact reported CRITICAL/0.751-vs-INFO/0.752 collision, runs it through `dedupe_findings` then `route_review`, and asserts `QUEUED_FOR_HITL` with the CRITICAL finding surviving dedupe -- proven to FAIL against the old confidence-only ordering (temporarily reverted, ran, captured the failure, restored the fix, reran, captured the pass; both outputs pasted in this session's final report) so the test is proven to actually catch the bug it exists to catch, not just pass vacuously. Also closed the test-design gap that let the original bug through: added `TestSeverityBeforeConfidenceDedupe` (parametrized over every severity pair where the higher-severity finding has lower confidence, plus a permutation-based property test asserting a CRITICAL finding is never dropped across every ordering of a 5-finding collision) and `TestDedupeAndRoutingInteraction` (dedupe's actual output piped into `route_review`, both orderings, plus a contrast case confirming non-CRITICAL severity wins still follow the ordinary confidence-threshold rule rather than an accidental blanket "severity wins therefore always HITL"). Did NOT change: `route_review`'s empty-findings-list behavior (still correctly routes to HITL at confidence 0.000, per the user's explicit decision that this is a deliberate conservative choice, not a defect -- a clarifying comment was considered but `route_review`'s existing threshold-comparison logic already handles it via the ordinary `overall_confidence < threshold` path with no special-casing, so no code change was needed there); and did NOT widen the dedup key to include `line_end` (also an explicit user decision -- see Deferred, below). All four gates plus PLAN.md's exact M5 demo command were re-run after the fix; see this session's full report for pasted output and exit codes.

A second, independent L4 VERIFY session then re-ran all four gates plus PLAN.md's M5 demo command against the fixed code, re-verified the CRITICAL-survives-dedupe guarantee through the real compiled graph (not just the unit-level regression test), and APPROVEd M5. M5 is now DONE; see `.genesis/PLAN.md`'s Progress entry and `.genesis/explanations/2026-08-30-explanation-m5.html` for the full account of the REJECT-then-fix cycle.

## Resolved (raised by an independent L4 VERIFY session on M8, fixed in this L2 DEBUG loop on 2026-08-30, now closed)

- ~~`EventRepository.sum_llm_cost_since` had no upper bound, so a
  future-dated row counted toward "today"'s spend~~ -- fixed for real, not
  re-pinned again: renamed to `sum_llm_cost_for_day` and bounded to a
  genuine half-open `[day_start, day_start + 1 day)` window
  (`backend/database/repository.py`). This is the SAME defect class the M8
  builder session's own fixture re-pin (see `## M8 history` below) only
  masked, and L4 VERIFY independently re-triggered it with its own
  2099-dated boundary-test rows ($40.00 of $20 cap) -- proof the earlier
  "fix" never touched the query itself. Proven with a real
  failing-then-passing regression run against real Postgres (query
  temporarily reverted to unbounded, 3 new tests failed with real spend
  figures inflated by the future-dated row; fix restored, all 8 tests in
  `tests/integration/test_budget_guard_events.py` passed) -- see this
  session's final report for both pasted runs.
- ~~`backend/orchestrator/nodes.py`'s `security_node` caught
  `BudgetExceededError` behind a bare `except Exception` and returned an
  empty findings list, indistinguishable from a clean security review~~ --
  fixed: the catch is narrowed to exactly the infrastructure/availability
  exceptions `SecurityAgent.analyze` can raise (`BudgetExceededError`,
  `LLMConfigurationError`, `LLMCallFailedError`), and any of them now
  returns one synthetic CRITICAL/confidence-0.000 `Finding`
  (`backend.agents.security_agent.infrastructure_failure_fallback_finding`)
  that forces `route_review` to `QUEUED_FOR_HITL` via the same mechanism
  `SecurityAgent`'s own total-parse-failure fallback already uses, instead
  of an empty list. A genuine programming bug (anything outside that narrow
  tuple) is no longer caught here and propagates. Checked and confirmed the
  other three stub specialist nodes (`quality`, `tests`, `docs`) never had
  this pattern -- no equivalent fix was needed there. Proven by 9 new tests
  in `tests/unit/test_security_node_infrastructure_failures.py`.
- ~~`context-graph.json`'s `budget-guard-hard-blocks` invariant described a
  per-node check that was never built~~ -- fixed: reworded to describe the
  real, centralized design (`AnthropicLLMClient.complete`/`complete_async`
  call `BudgetGuard.check_and_raise()` as the first statement), using L4
  VERIFY's own suggested wording verbatim. Verified checkable by grep
  (`Anthropic(` and `messages.create` each have exactly one real call site,
  both inside `complete`, reached only after `check_and_raise()`), and all
  4 hand-written invariants confirmed present after the edit.
  `.genesis/DONE.html` section 2's BudgetGuard gate text was checked and
  needs no change -- it was already implementation-agnostic.

## Deferred

New from M8 (L1 BUILD), non-blocking except where noted:
- **Live demo BLOCKED on credential, not a defect:** `ANTHROPIC_API_KEY` is
  not present in `.env`. PLAN.md's exact M8 demo command cannot be run for
  real; see the header's `last_gate` and `## M8 history` above for the
  real, non-fabricated CLI traceback proving exactly where it stops
  (`LLMConfigurationError`, at the point a real API call would be made).
  L4 VERIFY should re-run the demo for real once the key is available;
  until then this is the expected, honest state, not a build gap.
- **`complete_async` (`backend.tools.llm_client.AnthropicLLMClient`) has no
  live call site yet** -- the one live call site this milestone built
  (`security_node` -> `SecurityAgent.analyze` -> `complete`) runs on a
  LangGraph worker thread, never an asyncio event loop, so the synchronous
  `complete` is the correct method there. `complete_async` exists and is
  directly tested (proving the `asyncio.to_thread` offload actually works,
  not merely declared) for a future async caller -- the same
  forward-looking-infrastructure category as M7's own `emit_tool_call`.
- **A daily budget with no per-model or per-agent breakdown:** `BudgetGuard`
  sums ALL `llm.call` cost across every model/agent/review for the day
  against one global cap -- there is no way today to ask "how much did
  QUALITY specifically spend" or "how much came from a different model" in
  isolation. Acceptable for M8's single real specialist; worth revisiting
  once M10 wires up the other three real agents and cost attribution
  across specialists starts to matter operationally.
- **No emitted event on a failed LLM call:** `AnthropicLLMClient.complete`
  only calls `emit_llm_call` on success -- a call that exhausts retries or
  hits an open circuit breaker records nothing in `agent_events` beyond
  whatever the caller's own error handling logs elsewhere. A future
  milestone building real alerting/dashboards off the events spine may
  want a failure-shaped event too; not built here since no requirement
  named it and `outcome`/token/cost fields don't have an obvious "this
  failed" shape to reuse without a schema change.
- **Freeze-boundary touches beyond PLAN.md's literal M8 file list,
  disclosed here per M2/M5/M6/M7's own precedent:** `backend/agents/
  base_agent.py` (`analyze` gained an optional, keyword-only `review_id` --
  a backward-compatible extension of the exact forward-looking contract M5
  wrote specifically for M8 to implement against), `backend/orchestrator/
  {nodes,state}.py` (replacing the security stub and adding `GraphState`'s
  optional `diff` field -- both explicitly called for by this session's own
  build instructions: "Replace the security stub node in the orchestrator
  ... The graph, fan-out, aggregator and HITL gate must all still work"),
  and `tests/integration/test_orchestrator_fanout.py` (an autouse fixture
  installing a stub-equivalent fake agent, needed so the pre-existing M4/M5
  tests keep passing once `security_node`'s production default changed).
  `backend/prompts/templates/security/v1.md` uses a directory-per-agent
  layout (PLAN.md's literal freeze-boundary text named a single
  `backend/prompts/templates/security.md` file) per this session's own
  instruction to borrow "the reference implementation's two-level approach"
  (a versioned file per agent, not one flat file per agent) -- flagging
  since it is a structural, not merely additive, deviation from the
  literal freeze-boundary listing.

New from M7 (L4 VERIFY, both rounds), non-blocking except where noted:
- The events circuit breaker opening means events can be dropped for up to
  `reset_timeout_seconds` (30s default) under a sustained events-store
  outage. Every drop is logged, and this is a judged, deliberate tradeoff
  (a bounded gap in the audit trail vs. an unbounded request stall) -- but
  an audit log that can silently drop entries has an integrity concern
  distinct from plain availability. Wants a `/health` surface so an open
  events breaker is visible operationally, not just discoverable by
  grepping logs after the fact.
- `statement_timeout=2000ms` (`EventRepository`'s default) is judged
  reasonable for the single-row inserts this milestone performs, but is a
  tunable worth revisiting once real load/latency data exists rather than
  the current best-guess default.

Resolved (HIGH PRIORITY item raised by M7's L4 VERIFY, fixed in a dedicated
post-M7 L2 DEBUG loop on 2026-08-30, now closed):
- ~~The M6-scope Redis enqueue call had the exact same defect class M7 just
  fixed~~ -- fixed: `backend/webhook_receiver/router.py`'s `async def
  receive_webhook` called `queue.enqueue(event)` as a plain, unawaited,
  synchronous call; `RedisJobQueue.enqueue` (`backend/job_queue/
  redis_arq.py`) blocks the calling thread via `backend.reliability.
  timeout.await_future`'s `future.result(timeout=policy.seconds)`, and that
  call ran on uvicorn's single event-loop thread -- so one webhook whose
  Redis call was merely slow (not down) would serialise every other
  concurrent, unrelated webhook request behind it, exactly as the pre-fix
  events write did (missed by M6's own L4 VERIFY, which tested the
  circuit breaker/retry/timeout primitives thoroughly but never asked
  whether the call that uses them blocked the event loop). Fix: the
  router now does `await enqueue_async(queue, event)`
  (`backend/job_queue/interface.py`'s new `enqueue_async`), a free
  function that runs the existing, unchanged, still-synchronous
  `queue.enqueue` on a worker thread via `asyncio.to_thread` (asyncio's
  own default executor -- a bounded thread pool, not one thread per call),
  mirroring M7's own `emit_decision_async` fix for the identical defect
  class on the events-write path. `JobQueue.enqueue` itself, `RedisJobQueue`
  internals, and every M2/M3/M6 guarantee (exactly-once idempotency per
  `delivery_id` via Redis's atomic `SET NX EX`, `QueueUnavailableError` ->
  503, the retry/circuit-breaker/timeout composition) are unchanged --
  proven, not just re-asserted: `tests/unit/test_reliability.py`'s existing
  503/idempotency/wiring tests and `tests/integration/test_events_spine.py`'s
  decision-event tests all still pass unmodified; a new manual check fired
  20 genuinely concurrent requests sharing one `delivery_id` and confirmed
  exactly 1 `accepted` + 19 `duplicate` (idempotency under real, not
  event-loop-serialized, concurrency). A new regression test,
  `tests/integration/test_queue_roundtrip.py::
  TestConcurrentWebhookEnqueuesAreNotSerializedBySlowRedis`, models Redis's
  own `DEBUG SLEEP` (blocks the whole, single-threaded Redis server for a
  fixed duration) the way M7's own `TestConcurrentWebhookWritesAreNot
  SerializedByALockedEventsTable` modeled a Postgres table lock, and fails
  against the old blocking call (proven: temporarily reverted, ran, caught
  the N-times-serialized failure, restored, reran, passed -- both outputs
  captured in this session's final report). `docker-compose.yml`'s `redis`
  service gained `command: ["redis-server", "--enable-debug-command",
  "yes"]` so that test's `DEBUG SLEEP` is actually permitted (Redis 7
  refuses `DEBUG` from a Docker-published TCP port by default, not just
  from "local" in the sense it checks) -- local dev/test infrastructure
  only, nothing in `backend/` itself ever issues a `DEBUG` command. A real
  live-uvicorn latency experiment (short `RELIABILITY_TIMEOUT_SECONDS=1.0`/
  `RETRY_MAX_ATTEMPTS=1` for a fast, deterministic demo; Redis put to sleep
  for 8s via `DEBUG SLEEP`, independently verified still-unresponsive via a
  timed-out probe PING immediately before firing) measured 4 concurrent
  signed webhook POSTs: before the fix, latencies staggered to
  `[1.10, 4.06, 4.06, 4.05]s` (overall batch 4.13s, ~4x one attempt's
  timeout); after the fix, `[1.10, 1.03, 1.03, 1.02]s` (overall batch
  1.10s, ~1x) -- all four gates (`ruff`, `mypy --strict`, `pytest` with
  both Docker services up so integration tests actually ran, `lint-imports`)
  re-passed after the fix.

Resolved (raised by M7's first L4 VERIFY REJECT, fixed in the same L2 DEBUG
pass, now closed):
- ~~TRUNCATE bypassed the append-only trigger~~ -- fixed: PostgreSQL never
  fires a row-level (`FOR EACH ROW`) trigger for TRUNCATE, so the
  pre-existing `agent_events_no_update`/`agent_events_no_delete` pair did
  nothing to stop a superuser `TRUNCATE agent_events;` from silently
  wiping every row. `backend/database/migrations/0001_agent_events.sql`
  gained a statement-level (`FOR EACH STATEMENT`) `agent_events_no_truncate`
  `BEFORE TRUNCATE` trigger; proven by a real `TRUNCATE agent_events;` as
  the "postgres" superuser now raising `agent_events is append-only:
  TRUNCATE is not permitted`, row count unchanged.
- ~~Over-broad exception swallow in the events failure policy~~ -- fixed:
  `backend/observability/events.py`'s `_emit` used to catch bare
  `(psycopg.Error, OSError)`, which also silently swallowed
  `psycopg.errors.IntegrityError` (e.g. a CHECK-constraint violation, our
  own bug, not an availability failure). `IntegrityError` is now re-raised
  before the broad except runs; proven by a test suite that fails against
  the old broad except and passes against the fix.
- ~~The M7 demo command didn't work verbatim in a clean shell~~ -- fixed:
  `.env` is read by pydantic-settings but never exported to the shell, so
  a bare `psql "$DATABASE_URL"` after only `python scripts/
  run_fixture_review.py` saw an empty string. `.genesis/PLAN.md`'s M7 demo
  command now inlines `set -a && source .env && set +a` before the `psql`
  step, and its `ORDER BY ts` was corrected to `ORDER BY ts ASC, id ASC`
  to match `EventRepository.fetch_events_for_review`'s own tiebreak; both
  fixes verified in a clean shell via `env -i`.

New from M7 (L1 BUILD), non-blocking:
- Four files outside M7's literal freeze-boundary list were touched:
  `backend/webhook_receiver/router.py` + `backend/api/main.py` (wiring the
  webhook-ingress decision event and giving `create_app` a per-app
  injectable `EventRepository`, explicitly called for by this milestone's
  own instructions -- "wire event emission into ... at minimum the webhook
  ingress"), and `backend/orchestrator/nodes.py` (wiring
  span.start/span.end per specialist and the aggregator's decision event,
  equally explicitly called for). `scripts/run_fixture_review.py` was also
  added though not separately named in the freeze-boundary list -- PLAN.md's
  own M7 demo command invokes it by this exact path, the same situation as
  M2's `scripts/send_signed_webhook.py`. Flagging all four for L4 VERIFY to
  confirm this reading is acceptable, same as M2/M5/M6's own documented
  freeze-boundary notes.
- `backend.observability.get_event_repository()` (the process-wide
  singleton in `workflow_context.py`) is used by
  `backend.orchestrator.nodes` (LangGraph nodes have no per-request
  dependency injection available) but deliberately NOT by
  `backend.webhook_receiver.router`, which instead reads a per-app
  `EventRepository` off `request.app.state` (mirroring the existing
  `Settings`/`JobQueue` pattern) -- required so a test can point one
  isolated app's events writes at an unreachable DSN without affecting the
  real, shared docker-compose Postgres any other test in the same run
  depends on. Two different access patterns for the same repository type,
  by design; flagging so a future reader does not "fix" the inconsistency.
- `emit_llm_call`/`emit_tool_call` (`backend/observability/events.py`) have
  no live call site at M7 -- no real LLM call or tool call exists yet
  (M8's job). Forward-looking infrastructure, the same category as M5's
  `InMemoryHitlQueue` or M6's `CircuitBreaker` registry.
- The webhook-ingress decision event for a rejected/duplicate/accepted
  delivery is correlated by a synthetic `webhook-<delivery_id>` run id
  (`backend.observability.workflow_context.run_id_for_delivery`), never the
  real `review_id` an eventual orchestrator run for the same PR would use
  -- there is no way to join a webhook-ingress event to its later
  orchestrator-run events from `agent_events` alone today, since the
  orchestrator is still not wired into the webhook/queue path (a gap M4-M6
  all already deferred, unchanged by M7).
- `EventRepository` opens and closes a short-lived connection per call
  (insert or select) rather than holding a pool -- deliberately simpler
  than `RedisJobQueue`'s retry/circuit-breaker/timeout composition, since
  events are supplementary telemetry, not a request the caller blocks
  waiting on the *result* of. Acceptable for M7's local-dev scope; revisit
  if event-write volume/latency ever becomes a concern (e.g. a connection
  pool, or batching).
- Two full sets of specialist/decision events accumulated under
  `review_id='demo-1'` in this session's own demo-command run, because the
  same review_id was invoked twice against the append-only table (once
  manually while testing, once as the final demo command) -- expected,
  harmless, and exactly what append-only means (a fresh
  `docker compose up` with no volume wipes this on next boot regardless).
  Not a defect; noted so a future reader isn't confused by 18 rows instead
  of 9 in that specific manual repro.

New from M5 (L4 VERIFY, both rounds) and its L2 DEBUG fix, non-blocking:
- **Known deferred gap, explicitly not fixed by user decision:** `dedupe_findings`'s key is still exactly `(file_path, line_start)`, not `(file_path, line_start, line_end)`. L4 VERIFY separately flagged that a wide-span finding (e.g. `line_start=42, line_end=80`) can collapse into an unrelated, narrower finding that merely shares `line_start=42`. The user explicitly decided NOT to widen the key for this fix -- adding `line_end` does not solve the general "do these two findings' spans actually overlap" problem (two spans can overlap without sharing either endpoint), so it would be a partial fix wearing a full fix's clothes. Left as a known, tracked gap for a future milestone to address properly (e.g. real interval-overlap detection), not silently dropped.
- **Deliberate, non-defect behavior (documented so a future reader does not "fix" it):** an empty findings list still routes to HITL (`route_review(Decimal("0.000"), [], threshold=...)` -> `QUEUED_FOR_HITL`, since 0.000 is below any sane threshold). This is a deliberate conservative choice per the user's explicit instruction, not an oversight -- "no findings" should never look indistinguishable from "confidently nothing to report" and silently auto-post. Already covered by `tests/unit/test_hitl_gate.py::test_empty_findings_list_is_handled_sanely`.
- **Dead/forward-looking code:** `backend.models.findings.Finding.__lt__` (severity-then-confidence ordering via `SEVERITY_RANK`) has no call site anywhere in `backend/` production code -- `dedupe_findings` compares findings via its own `_is_better`/`_tie_break_key` helpers, and its final `sorted(best)` sorts dict keys `(file_path, line_start)`, not `Finding` objects, so `__lt__` is never invoked in production. It is exercised only indirectly, by a pre-existing, general-purpose test (`tests/unit/test_models.py::TestFinding::test_finding_sorting`, calling `sorted(findings)`), not by anything specific to M5's dedupe path. Kept because it is the correct, drift-proof ordering for a future direct caller (e.g. a dashboard listing findings), not because anything needs it today.
- **Accepted fail-safe consequence of the severity-first fix:** severity-first dedupe means a mixed-severity collision can now produce a *lower* `overall_confidence` than the old confidence-only rule would have, and that lower confidence can itself push a review from POSTED to QUEUED_FOR_HITL independent of the CRITICAL-finding check (e.g. a HIGH/0.100 finding beating a colliding MEDIUM/0.950 finding pulls the mean down, where the old rule would have kept the 0.950 finding and stayed above threshold). This lowers the auto-post rate for mixed-severity collisions compared to the pre-fix behavior. Deliberate and accepted: fewer auto-posts is the safe direction to be wrong in, and it is a direct, expected consequence of fixing the REJECT -- not a new defect.

New from M5 (L1 BUILD), non-blocking:
- Three files outside PLAN.md's literal M5 freeze-boundary list were touched: `backend/models/review.py` (+ `backend/models/__init__.py`, `tests/unit/test_models.py`), `backend/orchestrator/state.py`, and `tests/integration/test_orchestrator_fanout.py`. Each was a minimal, targeted change the L1 BUILD driver's own instructions explicitly called for (closing the M1 `overall_confidence` gap; giving the real aggregator's join node somewhere in `GraphState` to write its result; proving that wiring actually works end-to-end) rather than scope creep -- flagging for L4 VERIFY to confirm this reading is acceptable, same as M2/M4's own documented freeze-boundary/architecture notes.
- `mypy --strict backend/` required one call site (`backend/orchestrator/nodes.py`'s new `Review(...)` construction) to pass `error_message=None` explicitly, because mypy's `dataclass_transform`-based reading of pydantic v2 models does not recognize `Field(None, ...)` (a positional default, as opposed to a plain `= None` class-body default) as making a field optional in the synthesized constructor signature. This is a latent M1-era pattern (`Review.error_message` and `Finding` fields use `Field(None, ...)` throughout) that only surfaced now because M5 is the first *backend* production code to construct a `Review` -- every prior `Review(...)` call was in `tests/`, which `mypy --strict` does not scope. Not fixed at the model level (would touch M1's field style repo-wide, well outside M5's freeze boundary); worth revisiting if `mypy --strict` is ever extended to `tests/`.
- `InMemoryHitlQueue` (`backend/hitl/queue.py`) is a real, tested class, but nothing in `backend.orchestrator.nodes.aggregate_node` actually calls `.enqueue()` on an instance of it -- the join node computes the correct `Review.status` (POSTED vs QUEUED_FOR_HITL) and `routing_reason`, but stops there. Wiring "a QUEUED_FOR_HITL review is actually pushed into a live queue something else can read" is left for a later milestone (M10's full local dry run, or wherever a durable/dashboard-visible queue is built), matching M5's explicit exclusion of GitHub posting (M11) and consistent with M4's own precedent of not wiring the orchestrator into the webhook/queue path.
- The dedup tie-break's final fallback level (lexicographic `rationale` comparison, after confidence -> AGENT_PRECEDENCE -> category) is untested in isolation (only the category-level tie-break is exercised in `tests/unit/test_aggregator.py`) since triggering it requires two findings identical in every field except `rationale`, a case that cannot arise from M4/M5's one-finding-per-agent-per-run stubs. The logic is straightforward (one more lexicographic comparison in the same chain) but this specific level has no direct regression test.

New from M6 (L4 VERIFY), non-blocking:
- **CircuitBreaker's registry has no reader yet:** `register()`/`all_breakers()` populate a real process-wide registry, but nothing in this codebase calls `all_breakers()` -- there is no `/health` route yet to consume it. L4 VERIFY confirmed this is forward-looking infrastructure, not a gap in M6's own scope (see the L1 BUILD note below), but flagged it as worth revisiting specifically when M8's LLM client and M11's GitHub client get wrapped in the same reliability layer: a non-idempotent call to either of those (an LLM completion with side effects, a GitHub comment POST) would be a riskier thing to leave silently timed-out-but-still-running in the background than the idempotent Redis `SET`/enqueue operations this milestone guards today.
- **A timed-out call keeps running in the background, because Python cannot kill a thread:** `backend.reliability.timeout.run_with_timeout` submits the wrapped callable to a shared background thread pool and gives up *waiting* on it after the configured bound, but the worker thread itself keeps executing the original call to completion (or its own eventual failure) -- Python has no portable way to forcibly terminate a running thread. This is documented in the module's own docstring as a known, accepted limitation (safe today because the wrapped calls -- the idempotency `SET` and the ARQ enqueue -- are idempotent or side-effect-tolerant), not silently hidden. L4 VERIFY re-confirmed the documentation matches the actual behavior rather than treating "it's in a docstring" as sufficient on its own.
- **`idempotency.py`'s absence is a legitimate scope decision, not a missed deliverable:** PLAN.md's own slicing rule (see `PLAN.md` line 7: "a freeze boundary of files it **may** touch") defines a milestone's freeze boundary as the set of files it is *permitted* to touch, not a mandatory creation checklist every named file must produce. `backend/reliability/idempotency.py` was named in M6's freeze-boundary listing but never built, because the project's real idempotency mechanism (the atomic Redis `SET NX EX` in `backend/job_queue/redis_arq.py`, since M3) already exists and is a queue-layer concern, not a generic cross-cutting reliability primitive the way retry/timeout/circuit-breaker are. L4 VERIFY explicitly ruled on this reading (rather than either silently accepting the gap or demanding a second, unused idempotency module purely to check a box) and found it acceptable under the freeze-boundary rule as written.

New from M6 (L1 BUILD), non-blocking:
- Two files outside PLAN.md's literal M6 freeze-boundary list (`backend/reliability/{retry,circuit_breaker,idempotency,timeout}.py`, `tests/unit/test_reliability.py`) were touched: `backend/webhook_receiver/router.py` (the 503-not-500 fix, explicitly called for by this session's own build instructions to close a tracked M3-deferred item -- see the Resolved entry under M3, above) and `backend/job_queue/{redis_arq,interface}.py` (wiring the reliability layer into RedisJobQueue's real Redis calls, which is the actual point of this milestone -- a reliability layer with no live call site fails DONE.html's own "provable by grep" gate). `backend/core/settings.py` and `.env.example` were also touched, per this session's own explicit instruction to expose the new retry/timeout/breaker knobs there. Flagging all four for L4 VERIFY to confirm this reading is acceptable, same as M2/M4/M5's own documented freeze-boundary notes.
- `idempotency.py` (named in PLAN.md's M6 freeze boundary) was deliberately not built as a standalone module: the project's real idempotency mechanism is the atomic Redis `SET NX EX` already implemented in `backend/job_queue/redis_arq.py` since M3, which is a queue-layer concern (guaranteeing exactly-once enqueue per `delivery_id`), not a generic cross-cutting reliability primitive the way retry/timeout/circuit-breaker are. Building a second, unused `backend/reliability/idempotency.py` purely to satisfy the literal freeze-boundary listing would itself have been the "nothing calls it" failure mode this milestone exists to forbid. Flagging for L4 VERIFY to confirm this reading, rather than silently dropping the file.
- `CircuitBreaker`'s process-wide registry (`backend.reliability.circuit_breaker.register`/`all_breakers`) has no consumer yet -- no `/health` endpoint exists in this codebase as of M6. It was built because PLAN.md's own M6 text calls for "a way to inspect current state (a future /health endpoint will need it)" and because DONE.html's live-call-site gate is specifically about the retry/breaker/timeout *wrapping*, which is wired (see WIRING_PROOF in this session's final report) -- the registry itself is forward-looking infrastructure for a later milestone's `/health` route, the same category as M4's `WorkflowEngine` Protocol or M5's `InMemoryHitlQueue`. Flagging for L4 VERIFY, not treating as a gap in M6's own scope.
- The M6 wiring/integration tests (`TestRedisJobQueueWiring`, `TestWebhookReturns503WhenQueueUnavailable` in `tests/unit/test_reliability.py`) simulate "Redis is down" by constructing `RedisJobQueue` against the real, reachable project Redis (so its eager ARQ pool-creation succeeds) and then reassigning its private `_redis_sync` attribute to a client pointed at an unreachable address, rather than stopping the shared `docker-compose.yml` Redis container mid-test-run. This was a deliberate choice (stopping shared infrastructure mid-session would break any other test in the same run that assumes Redis stays up, e.g. `tests/integration/test_queue_roundtrip.py`) but does mean these specific tests never exercise `RedisJobQueue.__init__`'s own construction-time failure path (its eager `create_pool(...).result(timeout=...)` call) against a genuinely-down Redis -- only the post-construction `enqueue()` path is proven. Flagging as a known test-design boundary, not a defect.

Resolved (previously deferred from M1, now closed):
- ~~`Review.overall_confidence` has no cross-field consistency check (not cross-checked against the mean of `findings[].confidence`)~~ -- fixed at M5: `compute_overall_confidence(findings)` is the one formula (mean, ROUND_HALF_UP to 3 decimal places, 0.000 for an empty list), and `Review` has a `model_validator(mode="after")` that raises `ValidationError` if `overall_confidence` disagrees with it. A validator (reject on mismatch) was chosen over silently recomputing the field, so a caller's bug surfaces loudly at construction time instead of being silently discarded.

Still open from M1:
- `Finding`, `Review`, `WebhookEvent` are not frozen and do not set `validate_assignment`, so instances are mutable post-construction

Still open from M2:
- No max request body size is configured, so a large POST is fully buffered and hashed -- address before M11 internet exposure
- `backend/core/settings.py` placement is an accepted ADR-002 taxonomy nit, not a layering violation

Still open from M3 (and M3's L4 VERIFY), all non-blocking:
- No FastAPI lifespan hook calls `RedisJobQueue.close()`, so the background event-loop thread is simply abandoned on process shutdown. Harmless in practice (it's a daemon thread and the process is exiting anyway), but untidy -- a lifespan hook should call `close()` for a clean shutdown.
- Idempotency state (and the queue) is lost whenever the Redis container is recreated (`docker compose down && up`, no volume) -- confirmed empirically (DBSIZE drops to 0). This satisfies M3's own success criterion (no orphaned jobs) but the idempotency-reset corollary is documented in `docker-compose.yml` and `README.md`.

Resolved (previously deferred from M3, now closed):
- ~~A Redis-down `enqueue()` call currently surfaces as an unhandled 500 rather than a graceful 503~~ -- fixed at M6: `RedisJobQueue.enqueue()` now raises a specific `QueueUnavailableError` (`backend/job_queue/interface.py`) once its retry budget is exhausted or its circuit breaker is open, and `backend.webhook_receiver.router` catches exactly that exception and answers 503. Proven, not just asserted: `tests/unit/test_reliability.py::TestWebhookReturns503WhenQueueUnavailable` drives the real FastAPI route through `TestClient` against a `RedisJobQueue` whose synchronous client is pointed at an unreachable address (constructed against the real, reachable project Redis first, then redirected -- simulating a live dependency going down rather than stopping the shared docker-compose Redis other tests depend on), and asserts a 503, not a 500.

New from M4 (L1 BUILD + L4 VERIFY), still open:
- LangGraph logs a deserialization warning on checkpoint resume: "Deserializing unregistered type backend.models.enums.AgentType / Severity / backend.models.findings.Finding from checkpoint. This will be blocked in a future version." Our custom Pydantic/enum types are not registered with LangGraph's (de)serializer, so a future LangGraph major could break checkpoint resume for them entirely unless `allowed_msgpack_modules` (or an equivalent registration mechanism) is configured. Observed and recorded by L4 VERIFY; noted in `backend/orchestrator/langgraph_engine.py` near the checkpointer setup. Not blocking today -- resume works -- but should be addressed before relying on a newer LangGraph release.
- `LangGraphWorkflowEngine`'s default checkpoint DB path (`var/orchestrator_checkpoints.sqlite3`) is never exercised outside `tmp_path`-scoped tests -- there is no evidence one way or the other that the default path itself works end-to-end outside of tests; only the mechanism (SqliteSaver against a real file, and resume-across-a-new-engine-instance) was verified.
- The orchestrator built at M4 is not yet wired into the webhook/queue path: nothing in `backend.webhook_receiver` or `backend.job_queue` calls `LangGraphWorkflowEngine`. That wiring is expected to land in a later milestone, not a gap in M4's own scope.

Resolved (previously deferred from M2, now closed):
- ~~`_is_hex` uses `int(value, 16)` which accepts underscore separators and a leading sign~~ -- fixed: replaced with a strict `[0-9a-fA-F]+` charset regex; regression test added (`test_underscore_in_digest_is_rejected_as_malformed_not_invalid`)
- ~~The demo command needs an activated venv and a hand-created `.env` and neither is documented (no README exists)~~ -- fixed: README.md now documents venv creation/activation, `pip install -e ".[dev]"`, and copying `.env.example` to `.env`
- ~~`InMemoryJobQueue` and its `_seen_delivery_ids` grow unboundedly with no eviction~~ -- fixed: M3's `RedisJobQueue` stores the idempotency key with an expiring TTL (`Settings.idempotency_ttl_seconds`, default one week) instead of an ever-growing in-process set; proven by a test that reads the TTL back from Redis directly.

## M8 Build Summary (L1 BUILD, awaiting L4 VERIFY)

### G0 Pre-Flight Verdict
UNBUILT. `backend/tools/__init__.py`, `backend/prompts/__init__.py`, and
`backend/economics/__init__.py` were all module docstrings only -- no
`llm_client.py`, `registry.py`/`templates/`, or `budget.py`/`pricing.py`
existed. `backend/agents/security_agent.py` and `backend/agents/
response_parsing.py` did not exist. `backend.observability.events.
emit_llm_call` existed (M7) but had no live call site, exactly as M7's own
report disclosed. `backend.orchestrator.nodes.security_node` was still
M4's canned stub.

### Outcome Achieved
- `backend.tools.llm_client.AnthropicLLMClient`: wraps `anthropic.
  Anthropic().messages.create(...)` (model from `Settings.anthropic_model`,
  default `claude-haiku-4-5`) in retry -> circuit breaker -> timeout,
  composed identically to `RedisJobQueue`/`EventRepository`'s own pattern.
  `BudgetGuard.check_and_raise()` runs first, before the underlying
  Anthropic client is even constructed -- proven, not just asserted:
  `tests/unit/test_llm_client.py::TestBudgetGuardBlocksBeforeTheClient`
  asserts the injected fake client's call count stays 0. On success,
  computes real USD cost (`backend.economics.pricing.compute_cost_usd`)
  and emits `emit_llm_call`'s first live call site. `complete_async`
  offloads via `asyncio.to_thread`, proven by a heartbeat-coroutine test
  that a slow call does not stall a concurrent task.
- `backend.economics.budget.BudgetGuard`: hard-blocks (raises, never
  warns) once today's real spend -- summed from `agent_events` via a new
  `EventRepository.sum_llm_cost_since` query -- meets the configured daily
  cap (default $20). Proven against a real Postgres, not just a fake
  repository (`tests/integration/test_budget_guard_events.py`).
- `backend.prompts.registry.load_prompt`: versioned file on disk
  (`backend/prompts/templates/security/v1.md`) with an inline fallback,
  per this session's own instruction to borrow the reference
  implementation's two-level approach.
- `backend.agents.response_parsing.parse_findings_from_llm_response`: a
  tolerant parser surviving every named drift case (key drift, markdown
  fences, prose-then-JSON, a bare list, stacked combinations), with a
  correctness fix made during this same session (see `## M8 history`
  above) so a genuinely empty findings list is never confused with a
  total parse failure.
- `backend.agents.security_agent.SecurityAgent`: the first non-stub
  `BaseAgent`. On total parse failure, returns one synthetic CRITICAL/
  confidence-0.000 `Finding` (forces `has_critical_finding`'s
  unconditional HITL routing) rather than crashing or silently dropping
  the specialist's contribution. Also PLAN.md's named CLI demo entry
  point.
- `backend.orchestrator.nodes.security_node`: delegates to a real, lazily-
  constructed `SecurityAgent` by default, with a test-only override hook
  mirroring this module's pre-existing `arm_crash`/`arm_agent_error`
  pattern. `quality`/`tests`/`docs` are UNCHANGED canned stubs. Proven
  through the real compiled graph, not just in isolation: `tests/
  integration/test_orchestrator_fanout.py::
  TestRealSecurityAgentSlotsIntoTheGraph` installs a real `SecurityAgent`
  (fake LLM client, still no API key) and asserts its finding flows
  through alongside the three stubs' canned ones -- the M5-lesson
  composition test this session's own instructions called out by name.
- Every pre-existing M4/M5 fan-out/crash-resume/aggregation test in
  `test_orchestrator_fanout.py` keeps passing unchanged in *meaning* (not
  merely in exit code) via an autouse fixture that installs an
  M4-stub-equivalent fake security agent, reproducing the exact canned
  `Finding`/timing the real stub used to return.

### Gate Results (this session, full output in the L1 BUILD transcript)
- `ruff check .`: All checks passed, exit 0
- `mypy --strict backend/`: Success: no issues found in 57 source files, exit 0
- `pytest -v`: 205 passed, 1 skipped (the live-LLM-call integration test,
  correctly skipped for the missing `ANTHROPIC_API_KEY`), exit 0. Both
  Redis (6380) and Postgres (5433) were up for this run -- every
  DB-dependent test genuinely executed, including
  `test_budget_guard_events.py`'s real-Postgres proof.
- `lint-imports --config .importlinter`: 2 contracts kept, 0 broken, exit 0
- Cleaned up after: `docker compose down` run, confirmed no stray listeners
  on :5433/:6380/:8000, `docker compose ps` empty, and the unrelated
  `ampliphi-redis-1`/`ampliphi-postgres-1` containers left untouched and
  running throughout.

### Demo Command: BLOCKED -- awaiting credential
`ANTHROPIC_API_KEY` is not present in `.env` (`grep -q
'^ANTHROPIC_API_KEY=' .env` fails). PLAN.md's exact demo command was
actually attempted (with `.env` sourced into the shell), not merely
skipped: it fails with a real, non-fabricated `LLMConfigurationError`
("ANTHROPIC_API_KEY is not configured -- cannot make a real Anthropic API
call"), raised from `AnthropicLLMClient._client()` at exactly the point a
real call would be made -- i.e. every upstream step (CLI arg parsing, diff
reading, prompt loading, BudgetGuard's real-Postgres-backed check, which
passed since today's real spend was $0.000123) worked correctly, and the
ONLY blocker is the missing credential. See `## M8 history` above for the
full traceback and the test-isolation bug this exact repro surfaced and
fixed (a since-corrected future-dated test fixture had been polluting this
same real Postgres's daily spend figure).

### Files Written
- `backend/tools/llm_client.py`: `AnthropicLLMClient`, `LLMResponse`, `LLMClientProtocol`
- `backend/prompts/registry.py`, `backend/prompts/templates/security/v1.md`
- `backend/agents/response_parsing.py`: `parse_findings_from_llm_response`, `ResponseParseError`
- `backend/agents/security_agent.py`: `SecurityAgent` + CLI demo entrypoint
- `backend/agents/base_agent.py`: `analyze` gains optional `review_id` (freeze-boundary exception, disclosed)
- `backend/economics/budget.py`, `backend/economics/pricing.py`: `BudgetGuard`, `compute_cost_usd`
- `backend/database/repository.py`: `EventRepository.sum_llm_cost_since`
- `backend/core/settings.py`, `.env.example`: `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`/`BUDGET_DAILY_CAP_USD` + six `LLM_*` reliability knobs
- `backend/orchestrator/{nodes,state}.py`: real `security_node` + `GraphState.diff` (freeze-boundary exception, disclosed)
- `pyproject.toml`: `anthropic` runtime dependency; a `T20` per-file ruff ignore for `security_agent.py`'s CLI entrypoint
- `tests/unit/test_llm_client.py`, `tests/unit/test_security_agent_schema.py`, `tests/unit/test_budget_guard.py`: new unit suites
- `tests/integration/test_budget_guard_events.py`, `tests/integration/test_security_agent_live.py`: new integration suites (PLAN.md's named live-test file, plus the instructions' explicit real-Postgres BudgetGuard proof)
- `tests/integration/test_orchestrator_fanout.py`: autouse stub-agent fixture + new `TestRealSecurityAgentSlotsIntoTheGraph` (freeze-boundary exception, disclosed)
- `tests/fixtures/sqli_diff.patch`: PLAN.md's named M8 demo fixture
- `.genesis/context-graph.json`: refreshed (91->106 nodes, 295->421 edges); hand-written invariants backed up and restored byte-for-byte

### Architecture notes for the verifier
- The freeze-boundary exceptions above (`base_agent.py`, `orchestrator/
  {nodes,state}.py`, `test_orchestrator_fanout.py`, the
  `templates/security/v1.md` directory-per-agent layout vs. PLAN.md's
  literal single-file naming) need explicit sign-off, same as
  M2/M5/M6/M7's own documented notes.
- The `budget-guard-hard-blocks` context-graph invariant's wording mismatch
  (see `## Deferred` above) needs an explicit ruling: accept the
  centralized-in-the-LLM-client design and update the invariant's text, or
  require literal per-orchestrator-node duplication instead.
- The live demo is BLOCKED on credential, not broken -- everything upstream
  of the actual Anthropic API call (CLI, prompt loading, parsing, budget
  check against real Postgres) is proven working by the passing test suite
  and by the demo command's own real, observed failure point.

### Deferred / not built at M8 (explicitly out of scope, do not treat as gaps)
- No real QUALITY/TESTS/DOCS agents (M10) -- those three specialists
  remain M4's canned stubs, per this milestone's explicit scope
- No GitHub client (M11); no retrieval/memory (M9) -- neither was touched
- `complete_async` has no live call site yet -- see `## Deferred` above,
  same category as M7's own `emit_tool_call`

### Next Phase (M8 -> L4 VERIFY)
A separate agent/model session should run L4 VERIFY against this build:
re-run all four gates independently; check DONE.html's four M8-relevant
gates ("All LLM/structured outputs validated against a schema",
"BudgetGuard hard-blocks the next LLM call once the daily cap is
exceeded", "Every outbound call has a timeout / circuit-breaker", "Every
security and reliability module has a live call site in the request path,
provable by grep") by re-deriving the grep/dynamic evidence independently
rather than trusting this session's report; rule on the freeze-boundary
exceptions and the `budget-guard-hard-blocks` invariant wording mismatch
above; and, if `ANTHROPIC_API_KEY` has appeared in `.env` by then, run
PLAN.md's exact live demo command for real rather than continuing to treat
it as BLOCKED.

## M7 Build Summary (L4 VERIFY APPROVED, after an earlier REJECT + L2 DEBUG fix)

### G0 Pre-Flight Verdict
UNBUILT. `backend/observability/__init__.py` and `backend/database/__init__.py`
were both module docstrings only -- no `events.py`/`tracing.py`/`audit.py`/
`workflow_context.py` or `postgres.py`/`models.py`/`repository.py` existed.
No `agent_events` table, no migration, no postgres service in
`docker-compose.yml`, no `DATABASE_URL` in `Settings`.

### Outcome Achieved
- `docker-compose.yml` postgres service: `postgres:16-alpine` (deliberately
  not TimescaleDB -- see that file's comment and this session's report for
  the full justification), host port 5433 (5432 occupied by the unrelated
  `ampliphi-postgres-1` container; verified free via `lsof -i :5433` before
  choosing it).
- `backend/database/migrations/0001_agent_events.sql`: the `agent_events`
  table (timestamp, review_id, event_type, agent, model, tokens_in,
  tokens_out, `cost_usd NUMERIC(10,6)`, latency_ms, outcome,
  `confidence NUMERIC(4,3)`), indexed on `(review_id, ts)`, plus append-only
  enforcement -- a BEFORE UPDATE/DELETE trigger (fires for any role,
  including a superuser) and a dedicated `agent_events_writer` role granted
  only SELECT+INSERT with UPDATE/DELETE/TRUNCATE explicitly revoked.
- Enforcement was proven against a real, running Postgres, not merely
  written: a real UPDATE and a real DELETE against a real row were both
  rejected, with the actual database error text, as both the admin
  superuser (rejected by the trigger) and the restricted role (rejected by
  the permission check, before the trigger even runs). Full output pasted
  in this session's final report.
- `backend.database.repository.EventRepository`: INSERT/SELECT only, no
  UPDATE/DELETE statement anywhere in the file -- what makes the
  events-table-append-only invariant grep-verifiable, backed by an actual
  test (`TestNoApplicationCodeMutatesEvents`), not merely asserted in prose.
- `backend.observability`: `events.py` (one `emit_*` function per
  EventType, log-and-continue failure policy -- an events-DB outage never
  raises past `emit_*`, but a real construction-time bug, e.g. a bad type,
  still propagates), `tracing.py` (`traced_span`, measured latency +
  ok/error outcome), `audit.py` (`reconstruct_review_trace` -- the
  trace-viewer query PLAN.md's outcome text names), `workflow_context.py`
  (run-id correlation + a process-wide repository singleton for the
  orchestrator's LangGraph nodes, which have no per-request DI).
- Both live call sites wired and proven, by grep and dynamically: the
  webhook router now emits one `decision` event per verified, parsed
  pull_request outcome (accepted/duplicate/rejected), never before HMAC
  verification runs; each of the four orchestrator specialist nodes now
  runs inside `traced_span` (span.start/span.end, real measured latency),
  and `aggregate_node` emits one `decision` event with the real
  `Review.status`/`overall_confidence`.
- `Settings.database_url` (restricted role, what the app writes through)
  and `Settings.database_admin_url` (superuser, migrations only), both
  documented in `.env.example`.
- PLAN.md's demo command run verbatim end-to-end: `docker compose up -d
  postgres && python scripts/run_fixture_review.py --review-id demo-1 &&
  psql "$DATABASE_URL" -c "SELECT event_type, agent, ts FROM agent_events
  WHERE review_id='demo-1' ORDER BY ts"` -- combined exit 0, a non-empty,
  time-ordered sequence covering span.start through decision.

### Gate Results (this session, full output in the L1 BUILD transcript)
- `ruff check .`: All checks passed, exit 0
- `mypy --strict backend/`: Success: no issues found in 51 source files, exit 0
- `pytest -v`: 151 passed (137 carried over from M1-M6 + 14 new in
  `test_events_spine.py`), exit 0. Both Redis (6380) and Postgres (5433)
  were up for this run -- every DB-dependent test genuinely executed, none
  skipped.
- `lint-imports --config .importlinter`: 2 contracts kept, 0 broken, exit 0
- Cleaned up after: `docker compose down` run, confirmed no stray listeners
  on :5433/:6380/:8000, `docker compose ps` empty, and the unrelated
  `ampliphi-redis-1`/`ampliphi-postgres-1` containers left untouched and
  running throughout.

### Files Written
- `docker-compose.yml`: postgres service (5433, `postgres:16-alpine`, healthcheck)
- `backend/database/migrations/0001_agent_events.sql`: schema + trigger + restricted role
- `backend/database/{models,postgres,repository}.py`, `__init__.py` (re-exports)
- `backend/observability/{events,tracing,audit,workflow_context}.py`, `__init__.py` (re-exports)
- `backend/core/settings.py`, `.env.example`: `DATABASE_URL`/`DATABASE_ADMIN_URL`
- `backend/webhook_receiver/router.py`, `backend/api/main.py`: webhook-ingress decision event + per-app `EventRepository` injection (freeze-boundary exception, disclosed)
- `backend/orchestrator/nodes.py`: span.start/span.end + aggregator decision event (freeze-boundary exception, disclosed)
- `scripts/run_fixture_review.py`: PLAN.md's named M7 demo target (not separately listed in the freeze boundary; disclosed, same as M2's `send_signed_webhook.py`)
- `tests/integration/test_events_spine.py`: PLAN.md's named M7 demo test file, 14 tests
- `pyproject.toml`: `psycopg[binary]` runtime dependency
- `.genesis/context-graph.json`: refreshed (80->91 nodes, 221->295 edges); hand-written invariants backed up and restored byte-for-byte

### Architecture notes for the verifier
- The freeze-boundary exceptions above (`router.py`/`main.py`, `nodes.py`,
  `scripts/run_fixture_review.py`) need explicit sign-off, same as
  M2/M5/M6's own documented notes.
- The webhook router reads a per-app-injected `EventRepository` (off
  `request.app.state`, mirroring `Settings`/`JobQueue`), while the
  orchestrator's LangGraph nodes read the process-wide
  `get_event_repository()` singleton instead -- two different access
  patterns for the same repository type, by design (see Deferred).
- `emit_llm_call`/`emit_tool_call` have no live call site yet (M8's job) --
  confirm this is understood as intentional forward-looking scope, not a
  gap in M7's own.

### Deferred / not built at M7 (explicitly out of scope, do not treat as gaps)
- No real LLM agents (M8) -- `emit_llm_call` has no live caller yet
- No GitHub posting (M11); no dashboard/trace-viewer UI (M13) -- only the
  `reconstruct_review_trace` query it would call
- The orchestrator is still not wired into the webhook/queue path (M4-M6's
  own already-deferred item, unchanged by M7) -- so a webhook-ingress event
  and its eventual orchestrator-run events for the same PR cannot yet be
  joined from `agent_events` alone
- No Tiger Cloud / TimescaleDB hypertable, DiskANN, or continuous
  aggregates (M12's explicit scope) -- `agent_events` is a plain table

### Next Phase (M7 -> L4 VERIFY)
A separate agent/model session should run L4 VERIFY against this build:
re-run all four gates plus PLAN.md's exact M7 demo command independently,
re-derive the append-only enforcement proof independently (attempt a real
UPDATE/DELETE, both as the admin role and the restricted
`agent_events_writer` role, and confirm the actual rejection text) rather
than trusting this session's report, check DONE.html's "Every security and
reliability module has a live call site in the request path, provable by
grep" gate against `backend/observability`'s two wired call sites, and rule
on the freeze-boundary exceptions above before marking M7 DONE.

**Outcome:** a first independent L4 VERIFY session did exactly this and
REJECTED the build -- not for the append-only/wiring gates quoted above
(both held up), but for a real reliability defect squarely inside
DONE.html section 2's "every outbound call has a timeout / circuit-breaker"
gate: `EventRepository.insert_event` was a synchronous, un-offloaded call
made directly from inside `async def receive_webhook`, and a stalled
events write measurably serialised concurrent webhook requests (~4.4s each
under a locked events table, reproduced against a live uvicorn, not just
theorized). An L2 DEBUG session applied the approved fix described at the
top of this checkpoint (offload via `asyncio.to_thread`, a real
`statement_timeout`, and M6's own `CircuitBreaker` class reused for the
events path), plus closed the TRUNCATE gap and narrowed the exception
swallow the same review raised. A second, independent L4 VERIFY session
re-ran everything against the fix, reproduced the concurrency numbers
itself (~2.03s/request, ~2.09s total wall time for three genuinely
concurrent requests), independently proved the event loop stays
responsive even with its executor saturated, and APPROVEd M7. M7 is DONE.

## M6 Build Summary (L4 VERIFY APPROVED)

### G0 Pre-Flight Verdict
UNBUILT. `backend/reliability/__init__.py` was a module docstring only --
no `retry.py`/`circuit_breaker.py`/`timeout.py`/`idempotency.py` existed.
`backend/job_queue/redis_arq.py` had two hardcoded `future.result(timeout=10)`-
style magic numbers (`_POOL_CREATE_TIMEOUT_SECONDS`, `_ENQUEUE_TIMEOUT_SECONDS`)
and no retry loop or circuit breaker anywhere. `tests/unit/test_reliability.py`
did not exist.

### Outcome Achieved
- `backend.reliability.retry.call_with_retry`: bounded exponential backoff
  with full jitter, an explicit `non_retryable_exceptions` set (default:
  `TypeError`/`ValueError`/`KeyError`/`AttributeError` -- programmer
  errors, never retried) so a malformed-argument bug cannot burn the whole
  retry budget.
- `backend.reliability.timeout.{await_future,run_with_timeout}`: a shared,
  configurable bounded-wait wrapper for both an in-flight
  `concurrent.futures.Future` and a plain synchronous callable (via a
  shared background thread pool), replacing the old hardcoded
  `future.result(timeout=10)`.
- `backend.reliability.circuit_breaker.CircuitBreaker`: closed/open/half-open,
  a single `threading.Lock` guarding every state read and mutation (the
  wrapped call itself runs with the lock released), a process-wide registry
  for a future `/health` endpoint.
- All three are wired into `backend.job_queue.redis_arq.RedisJobQueue`'s two
  real Redis operations (the idempotency `SET NX EX` and the cross-thread
  ARQ enqueue) -- composed as retry(circuit-breaker(timeout(call))), so once
  the breaker opens, the retry loop's own non-retryable-exception check
  stops it from sleeping and trying again. See this session's full report
  for the grep-verified live call sites.
- `backend.job_queue.interface.QueueUnavailableError`: the shared exception
  `RedisJobQueue` now raises when retries are exhausted or the breaker is
  open; `backend.webhook_receiver.router` catches it and returns 503,
  closing the M3-deferred "Redis-down enqueue returns 500 not 503" item.
- Six new `Settings` fields (`retry_max_attempts`, `retry_base_delay_seconds`,
  `retry_max_delay_seconds`, `reliability_timeout_seconds`,
  `circuit_breaker_failure_threshold`, `circuit_breaker_reset_timeout_seconds`),
  documented in `.env.example`.

### Gate Results (this session, full output in the L1 BUILD transcript)
- `ruff check .`: All checks passed, exit 0
- `mypy --strict backend/`: Success: no issues found in 44 source files, exit 0
- `pytest -v`: 137 passed (114 carried over from M1-M5 + 23 new in
  `test_reliability.py`), exit 0. Redis (project's own, port 6380) was up
  for this run -- the `redis`-parametrized/gated cases in both
  `test_queue_roundtrip.py` and the new `test_reliability.py` genuinely
  executed, not skipped.
- `lint-imports --config .importlinter`: 2 contracts kept, 0 broken, exit 0
- PLAN.md's M6 demo command run verbatim
  (`pytest tests/unit/test_reliability.py -v --tb=short`): 23 passed, exit 0.
- Cleaned up after: `docker compose down` run, confirmed no stray listeners
  on :6380/:8000, `docker compose ps` empty, and the unrelated
  `ampliphi-redis-1`/`ampliphi-postgres-1` containers left untouched and
  running throughout.

### Files Written
- `backend/reliability/retry.py`, `circuit_breaker.py`, `timeout.py`, `__init__.py` (re-exports)
- `backend/job_queue/redis_arq.py`: wired the reliability layer around both real Redis calls
- `backend/job_queue/interface.py`: `QueueUnavailableError`
- `backend/webhook_receiver/router.py`: catches `QueueUnavailableError` -> 503 (freeze-boundary exception, disclosed)
- `backend/core/settings.py`, `.env.example`: six new reliability knobs
- `tests/unit/test_reliability.py`: PLAN.md's named M6 demo test file, 23 tests
- `.genesis/context-graph.json`: refreshed (73->80 nodes, 174->221 edges); hand-written invariants backed up and restored byte-for-byte

### Architecture notes for the verifier
- The freeze-boundary exceptions above (`router.py`, `redis_arq.py`/`interface.py`, `settings.py`/`.env.example`) need explicit sign-off, same as M2/M4/M5's own documented notes.
- `idempotency.py` was deliberately not built as a standalone module -- see Deferred, above, for the reasoning.
- The "Redis down" wiring tests simulate failure by redirecting `RedisJobQueue._redis_sync` post-construction rather than stopping the shared docker-compose Redis mid-session -- see Deferred, above.

### Deferred / not built at M6 (explicitly out of scope, do not treat as gaps)
- No real LLM agents (M8); no GitHub client (M11) -- this milestone's reliability layer wraps only the one real outbound I/O that exists today (Redis), per this session's own explicit scope instruction
- No `/health` endpoint yet to surface `CircuitBreaker`'s registry (a later milestone's job)

### Next Phase (M6 -> L4 VERIFY)
A separate agent/model session should run L4 VERIFY against this build:
re-run all four gates plus the demo command independently, check
DONE.html's two M6-relevant gates ("Every outbound call has a
timeout / circuit-breaker" and "Every security and reliability module has a
live call site in the request path, provable by grep -- not merely a
passing unit test") by re-deriving the grep evidence independently rather
than trusting this session's report, and rule on the freeze-boundary
exceptions above before marking M6 DONE.

**Outcome:** A separate, independent L4 VERIFY session (Sonnet) did exactly
this and **APPROVEd** M6 with no blocking defects. Critically, it did not
settle for "the module exists and is imported" as evidence for the
grep-provable live-call-site gate -- a reference implementation that wrote
these same three modules, unit-tested them, and never imported them from
the live path would pass an imports-only check too. Instead it verified the
wiring two ways: dynamically, by monkeypatching the retry and circuit
breaker primitives and observing exactly 2 circuit-breaker calls and 2
retry-loop attempts occur during one real webhook request driven through
`TestClient` against a `RedisJobQueue` whose Redis connection was redirected
to an unreachable address; and by falsification, temporarily neutering
`CircuitBreaker.call` (bypassing its own state machine and calling straight
through) and confirming this makes 8 of the 23 `test_reliability.py` tests
fail, rather than all 23 continuing to pass vacuously. It also confirmed the
503-not-500 fix does not weaken the HMAC trust boundary: a bad-signature
request against a down-Redis queue still returns 401, because signature
verification runs before the queue is ever touched. See
`.genesis/explanations/2026-08-30-explanation-m6.html` for the full account.
M6 is DONE.

## M5 Build Summary (L4 VERIFY APPROVED, after an earlier REJECT + L2 DEBUG fix)

### Outcome Achieved
- `backend.agents.contracts.dedupe_findings` merges the four specialists'
  findings, collapsing exact `(file_path, line_start)` duplicates and
  keeping the higher-severity finding, with confidence used only to break a
  tie within the same severity (a CRITICAL finding must never lose a dedup
  collision to a lower-severity one, regardless of confidence) -- and a
  fully deterministic tie-break below that (confidence -> `AGENT_PRECEDENCE`
  -> category -> rationale) that does not depend on the input list's order --
  proven by running the same two findings through in both orders and
  asserting identical output.
- `backend.models.review.compute_overall_confidence` is the single formula
  for `Review.overall_confidence` (mean of surviving findings' confidence,
  ROUND_HALF_UP to 3 decimal places, 0.000 for an empty list), and `Review`
  now enforces it via a `model_validator` -- closing the M1-deferred gap
  (see Resolved, above).
- `backend.hitl.queue.route_review` implements the HITL gate: auto-post iff
  `overall_confidence >= threshold` and no CRITICAL finding; otherwise
  queued for human review. Exactly-at-threshold auto-posts (tested on both
  sides of the boundary plus exactly at it). A CRITICAL finding forces
  human review unconditionally, even at confidence 1.000 or threshold
  0.000 (tested).
- Every reason string `route_review` returns is built from the same
  `threshold` value the comparison uses -- verified empirically, not just
  by code inspection: temporarily hardcoded a stale number into the
  message (reproducing the reference implementation's exact bug --
  `critical_block_count >= 2` in code vs. "3+ agents required" in the
  message), confirmed `test_reason_message_uses_the_configured_threshold_value`
  fails against the tampered file, then restored the file and confirmed
  the test passes again.
- `backend.orchestrator.nodes.aggregate_node` (M4's no-op join-node stub)
  now runs this whole pipeline for real as the graph's fan-in node:
  dedupe -> compute confidence -> route -> construct `Review` -> write it
  into `GraphState`. Proven reachable through the actual compiled graph
  (not just correct in isolation) by
  `test_aggregate_node_wires_into_the_fan_in_and_produces_a_review`.
- `HITL_CONFIDENCE_THRESHOLD` (default 0.750) is a `Settings` field,
  documented in `.env.example`.

### Gate Results (this session, full output in the L1 BUILD transcript)
- `ruff check .`: All checks passed, exit 0
- `mypy --strict backend/`: Success: no issues found in 41 source files, exit 0
- `pytest -v`: 91 passed (62 carried over from M1-M4 + 29 new: 13 in
  `test_aggregator.py`, 13 in `test_hitl_gate.py`, 2 new regression tests
  in `test_models.py` for the closed M1 gap, 1 new graph-wiring
  integration test in `test_orchestrator_fanout.py`), exit 0. Redis
  (project's own, port 6380) was up for this run -- the
  `[redis]`-parametrized `test_queue_roundtrip.py` cases genuinely
  executed (visible in the -v output), not skipped.
- `lint-imports --config .importlinter`: 2 contracts kept, 0 broken, exit 0
- PLAN.md's M5 demo command run verbatim
  (`pytest tests/unit/test_aggregator.py tests/unit/test_hitl_gate.py -v`):
  26 passed, exit 0.
- Cleaned up after: `docker compose down` run, confirmed no stray listeners
  on :6380/:8000, `docker compose ps` empty, and the unrelated
  `ampliphi-redis-1`/`ampliphi-postgres-1` containers left untouched and
  running throughout.

### Files Written
- `backend/agents/base_agent.py`: `BaseAgent` (M8 forward-looking contract) + `AGENT_PRECEDENCE`
- `backend/agents/contracts.py`: `dedupe_findings` (the aggregator's dedup core)
- `backend/hitl/queue.py`: `route_review`, `has_critical_finding`, `InMemoryHitlQueue`
- `backend/models/review.py`: `compute_overall_confidence` + the consistency `model_validator` (outside M5's literal freeze boundary -- see Deferred)
- `backend/models/__init__.py`: exports `compute_overall_confidence`
- `backend/core/settings.py`: `hitl_confidence_threshold` field
- `.env.example`: documents `HITL_CONFIDENCE_THRESHOLD`
- `backend/orchestrator/nodes.py`: real `aggregate_node` implementation
- `backend/orchestrator/state.py`: `review`/`routing_reason` `NotRequired` fields (outside M5's literal freeze boundary -- see Deferred)
- `tests/unit/test_aggregator.py`, `tests/unit/test_hitl_gate.py`: PLAN.md's named M5 demo test files
- `tests/unit/test_models.py`: updated pre-existing tests that relied on the now-closed M1 gap, plus 2 new regression tests
- `tests/integration/test_orchestrator_fanout.py`: 1 new test proving the real aggregator is wired into the graph's fan-in edge

### Architecture notes for the verifier
- The three freeze-boundary exceptions above (`backend/models/review.py` +
  its `__init__.py`/test file, `backend/orchestrator/state.py`,
  `tests/integration/test_orchestrator_fanout.py`) need explicit sign-off,
  same as M2/M4's own documented placement/scope notes.
- `InMemoryHitlQueue` exists and is tested but is not yet called by
  `aggregate_node` or anything else -- confirm this is understood as
  intentional scope (a later milestone's job), not a gap in M5's own.
- The dedup tie-break's `rationale`-level fallback (after confidence ->
  `AGENT_PRECEDENCE` -> category) has no direct test, since triggering it
  needs two findings identical in every field but `rationale`, which
  cannot arise from the current one-finding-per-agent stubs.

### Deferred / not built at M5 (explicitly out of scope, do not treat as gaps)
- No real LLM agents (M8); no GitHub posting (M11)
- No durable/shared HITL queue (only the in-memory stand-in); no dashboard to view it
- The orchestrator is still not wired into the webhook/queue path (M4's own deferred item, unchanged by M5)

### Next Phase (M5 -> L4 VERIFY)
A separate agent/model session should run L4 VERIFY against this build:
re-run all four gates plus the demo command independently, check DONE.html's
M5-relevant gate ("Findings dedupe by file and line, keeping the
higher-severity finding first and using confidence only to break a tie
within the same severity -- a CRITICAL finding must never lose to a
lower-severity one; aggregator threshold in code matches the threshold in
its user-facing message"), rule on the three freeze-boundary exceptions
above, and confirm the M1 gap closure's chosen approach (a rejecting
`model_validator`, not a silent recompute) is acceptable before marking M5
DONE.

**Outcome:** a first independent L4 VERIFY session did exactly this and
REJECTED the build -- the quoted gate above is what it found violated (the
build as originally written kept the highest-*confidence* finding with no
severity check at all). An L2 DEBUG session applied the fix described at
the top of this checkpoint, and a second, independent L4 VERIFY session
re-ran everything against the fix and APPROVEd M5. M5 is DONE.

## M4 Build Summary (L4 VERIFY APPROVED)

### Outcome Achieved
- A LangGraph StateGraph fans out from START to four parallel stub
  specialist nodes (security, quality, tests, docs) via the Send API, and
  fans back in through a shared aggregate join node to END.
- Each specialist is a stub: one canned, deterministic Finding per agent
  type, no LLM call, no API key read (backend/orchestrator/nodes.py).
- Parallelism is real, not a linear chain: measured wall time for all four
  nodes was ~0.3s (each node sleeps 0.2s) vs. the ~0.8-1.2s a sequential
  chain would take, and every pair of nodes' recorded execution windows
  pairwise overlaps -- both asserted in
  test_fanout_runs_nodes_in_parallel.
- State merges correctly across parallel branches: GraphState.findings uses
  an operator.add reducer and node_errors uses a dict-merge reducer, so all
  four specialists' writes combine instead of the last one to finish
  overwriting the rest -- proven by asserting 4 distinct findings survive
  (a reducer-less design would collapse this to 1).
- The graph is compiled with a checkpointer, never without: build_graph()
  takes checkpointer as a required parameter with no default.
- Checkpoint resume actually skips completed work: arming a simulated crash
  in one node, running (which raises), then building a brand-new
  LangGraphWorkflowEngine against the same on-disk SQLite checkpoint file to
  resume shows the three already-completed nodes' call counts stay at 1
  (never re-executed) while the crashed node's count becomes 2 (genuinely
  retried) -- test_checkpoint_resume_skips_completed_nodes.
- A specialist's own failure is isolated: an AgentExecutionError from one
  node is caught and recorded in node_errors rather than losing the other
  three specialists' findings -- test_fanout_isolates_single_node_failure.
- backend/core/workflow_engine.py adds the ADR-001 abstract WorkflowEngine
  interface (run/resume/get_state), generic over an opaque state type so it
  never has to import backend.orchestrator or backend.models (forbidden by
  .importlinter's core-independence contract).

### Checkpointer choice: SQLite, not Redis
langgraph-checkpoint-redis was installed and smoke-tested directly against
this project's own docker-compose Redis (plain redis:7-alpine, from M3)
before any production code was written. It failed on .setup() with
`redis.exceptions.ResponseError: unknown command 'FT.INFO'` -- it needs
RediSearch to build its checkpoint index, which plain Redis does not
provide. Switching the Redis image to Redis Stack is outside M4's freeze
boundary (docker-compose.yml is not listed), so this build uses
langgraph-checkpoint-sqlite instead: a real, first-party LangGraph
checkpointer, file-backed (not :memory:) so checkpoints survive the writing
process exiting. Full reasoning lives in
backend/orchestrator/langgraph_engine.py's module docstring. This is a
documented, justified deviation from PLAN.md's M4 outcome text ("checkpoints
state to Redis"), in the same spirit as M3's Redis-port-6380 deviation --
recorded, not hidden. See the Deferred section above.

### Gate Results (this session, full output in the L1 BUILD transcript)
- `ruff check .`: All checks passed, exit 0
- `mypy --strict backend/`: Success: no issues found in 38 source files, exit 0
- `pytest -v`: 62 passed (57 carried over from M1-M3 + 5 new orchestrator
  tests), exit 0
- `lint-imports --config .importlinter`: 2 contracts kept, 0 broken, exit 0
- PLAN.md's M4 demo command run verbatim
  (`pytest tests/integration/test_orchestrator_fanout.py -v -k "fanout or
  checkpoint_resume"`): 5 passed (all 5 test names match the -k filter),
  exit 0.
- Cleaned up after: `docker compose down` run, confirmed no stray listeners
  on :6380/:8000, and the unrelated ampliphi-redis-1/ampliphi-postgres-1
  containers left untouched and running throughout.

### Files Written
- `backend/core/workflow_engine.py`: ADR-001 abstract WorkflowEngine Protocol
- `backend/orchestrator/state.py`: GraphState TypedDict with parallel-merge reducers
- `backend/orchestrator/nodes.py`: four stub specialist nodes + aggregate join node + test instrumentation (call_count/execution_windows/arm_crash/arm_agent_error)
- `backend/orchestrator/graph.py`: Send-API fan-out/fan-in StateGraph builder
- `backend/orchestrator/langgraph_engine.py`: LangGraphWorkflowEngine (SQLite-backed)
- `pyproject.toml`: `langgraph`, `langgraph-checkpoint-sqlite`, `langchain-core` runtime deps
- `.gitignore`: ignores `var/` (default local checkpoint DB directory)
- `tests/integration/test_orchestrator_fanout.py`: 5 integration tests

### Architecture notes for the verifier
- The Redis-vs-SQLite checkpointer deviation above needs explicit sign-off.
- WorkflowEngine is satisfied structurally (Protocol), not via explicit
  inheritance -- LangGraphWorkflowEngine does not subclass it. Confirm this
  reading of "abstract interface... so LangGraph could be swapped later" is
  acceptable, or that explicit inheritance was intended.
- The orchestrator is not yet wired into the webhook/worker path (not part
  of M4's freeze boundary) -- confirm this is understood as intentional
  scope, not a gap.

### Deferred / not built at M4 (explicitly out of scope, do not treat as gaps)
- No real LLM agents (M8); no aggregator confidence/dedup/HITL-routing logic (M5)
- No wiring from the webhook/queue path into the orchestrator (later milestone)

### Next Phase (M4 -> L4 VERIFY)
A separate agent/model session should run L4 VERIFY against this build:
re-run all four gates plus the demo command independently, check the
DONE.html gate for M4 ("LangGraph checkpoints actually resume after a
simulated worker crash -- compiled with a checkpointer, not without"), and
rule on the Redis-vs-SQLite checkpointer deviation before marking M4 DONE.

## M3 Build Summary (L4 VERIFY APPROVED)

### Outcome Achieved
- A validated webhook enqueues a job to Redis via ARQ
  (`backend/job_queue/redis_arq.py`), and a separate worker process
  (`arq backend.job_queue.arq_worker.WorkerSettings`) dequeues and records
  it -- the async hand-off M3 exists to prove.
- `backend/webhook_receiver/router.py` required zero changes: both
  `InMemoryJobQueue` and `RedisJobQueue` satisfy the same `JobQueue`
  Protocol from M2, confirmed by a parameterized contract test.
- Idempotency is an atomic `SET key value NX EX ttl` (not a
  check-then-act `EXISTS`+`SET`), with a configurable TTL
  (`IDEMPOTENCY_TTL_SECONDS`, default one week) that fixes the
  M2-deferred unbounded-growth finding.
- `docker-compose.yml` runs Redis on host port 6380 (not 6379 -- occupied
  by an unrelated project's container on this machine).

### Gate Results (this session, full output in the L1 BUILD transcript)
- `ruff check .`: All checks passed, exit 0
- `mypy --strict backend/`: Success: no issues found in 33 source files, exit 0
- `pytest -v`: 57 passed (48 carried over from M1/M2, including the
  post-M2 `_is_hex` regression test, + 9 new integration tests), exit 0.
  All 9 new tests ran for real against a real dockerized Redis, not
  skipped.
- `lint-imports --config .importlinter`: 2 contracts kept, 0 broken, exit 0
- PLAN.md's M3 demo command run verbatim (`docker compose up -d redis &&
  arq backend.job_queue.arq_worker.WorkerSettings & pytest
  tests/integration/test_queue_roundtrip.py -v`): combined exit 0 (all 9
  tests passed); separately confirmed via `ps aux` that the backgrounded
  ARQ worker process actually started and held live connections to Redis.
- Cleaned up after: worker process killed, `docker compose down` run,
  confirmed no stray listeners on :6380/:8000 and the unrelated
  `ampliphi-redis-1`/`ampliphi-postgres-1` containers left untouched.

### Files Written
- `docker-compose.yml`: single `redis` service, pinned `redis:7-alpine`, healthcheck
- `backend/job_queue/redis_arq.py`: `RedisJobQueue` (TTL'd idempotency + ARQ hand-off)
- `backend/job_queue/arq_worker.py`: `WorkerSettings` + stub `process_webhook_event` handler
- `backend/core/settings.py`: added `redis_url`, `idempotency_ttl_seconds`, `job_queue_backend`
- `backend/api/main.py`: `_default_job_queue()` selects the implementation via settings
- `.env.example`: documents `JOB_QUEUE_BACKEND`, `REDIS_URL`, `IDEMPOTENCY_TTL_SECONDS`
- `pyproject.toml`: `redis`, `arq` runtime deps; pytest-asyncio auto mode + `redis` marker
- `tests/integration/test_queue_roundtrip.py`: 9 tests against real Redis

### Architecture notes for the verifier
- `RedisJobQueue.enqueue()` stays synchronous (the Protocol's contract)
  by bridging into ARQ's async client via a dedicated background thread +
  `asyncio.run_coroutine_threadsafe`, rather than nesting an event loop
  inside a request already running on uvicorn's. Confirm this is an
  acceptable pattern, or that a future milestone should revisit it if
  request-path latency becomes a concern.
- Host port 6380 (not 6379) for Redis is a deliberate, documented
  workaround for a real port conflict discovered on the build machine
  (an unrelated project's own container), not an arbitrary choice --
  see `docker-compose.yml`'s comment.

### Deferred / not built at M3 (explicitly out of scope, do not treat as gaps)
- No LangGraph orchestrator, no real agents (M4+)
- The ARQ worker's job handler is a stub (logs + records a marker key);
  no review workflow logic

### Next Phase (M3 -> L4 VERIFY)
A separate agent/model session should run L4 VERIFY against this build:
re-run all four gates plus the demo command independently, check the DoD
gates in DONE.html section 2 relevant to M3, and confirm the
port-6380 deviation and the background-thread event-loop bridge in
`RedisJobQueue` are both acceptable before marking M3 DONE.

## M2 Build Summary (L4 VERIFY APPROVED)

### Outcome Achieved
- FastAPI POST /webhook verifies GitHub's HMAC-SHA256 signature over the RAW
  request body using `hmac.compare_digest` (backend/webhook_receiver/validator.py)
- Missing signature -> 401; malformed header shape -> 400; wrong signature -> 401;
  tampered body with an otherwise well-formed signature -> 401
- X-GitHub-Delivery idempotency: JobQueue.enqueue() is a no-op on a repeated
  delivery_id (backend/job_queue/{interface,in_memory}.py) -- verified by an
  actual queue-size assertion in tests, not just a second 200 response
- Only pull_request events with action in {opened, synchronize, reopened} are
  parsed and enqueued; everything else is acknowledged (200) and ignored
- No real queue yet (that's M3): enqueue sits behind a JobQueue Protocol so
  M3 can swap in Redis/ARQ without touching the router or its tests

### Gate Results (this session, full output in the L1 BUILD transcript)
- `ruff check .`: All checks passed, exit 0
- `mypy --strict backend/`: Success: no issues found in 31 source files, exit 0
- `pytest -v`: 47 passed (24 from M1 + 23 new webhook tests), exit 0
- `lint-imports --config .importlinter`: 2 contracts kept, 0 broken, exit 0
- PLAN.md's M2 demo command run verbatim (uvicorn backgrounded, signed POST,
  pytest): combined exit 0. Note: the literal `sleep 1` in that command was
  observed to be marginal on a cold start once during this session (uvicorn
  took slightly longer than 1s to bind, causing a connection-refused retry);
  a rerun succeeded within ~0.3-0.5s startup. Not modified since PLAN.md's
  exact command is what must be preserved, but the L4 verifier should be
  aware this specific demo command has a small flake risk on a slow machine.
- Verified no stray process left listening on :8000 after the demo run.

### Files Written
- `backend/core/settings.py`: pydantic-settings Settings (GITHUB_WEBHOOK_SECRET, no default)
- `backend/webhook_receiver/validator.py`: HMAC verification (3 distinct exception types)
- `backend/webhook_receiver/parser.py`: raw payload -> WebhookEvent, SUPPORTED_ACTIONS
- `backend/job_queue/{interface,in_memory}.py`: JobQueue Protocol + InMemoryJobQueue
- `backend/webhook_receiver/router.py`: POST /webhook, dependency-injected via app.state
- `backend/api/main.py`: create_app() factory + module-level app for uvicorn
- `scripts/send_signed_webhook.py`: signs+POSTs the fixture payload as raw bytes
- `tests/unit/test_webhook_validator.py`: 23 tests (validator unit + route-level via TestClient)
- `tests/conftest.py`, `tests/fixtures/sample_pr_payload.json`, `.env.example`

### Architecture notes for the verifier
- `backend/core/settings.py` was placed in `backend.core` rather than a new
  top-level `backend.config` package, since `config` is not one of the 19
  subpackages in ADR-002's frozen module map and `core` is explicitly the
  layer for base abstractions all others depend on. Confirm this reading is
  acceptable, or that a dedicated config package should be added to the map.
- A local `.env` (GITHUB_WEBHOOK_SECRET=test-secret, gitignored, not pushed)
  is required for `uvicorn backend.api.main:app` to start, matching
  PLAN.md's demo command which does not export the env var inline. This is
  intentional -- documented in .env.example -- not a hardcoded secret.
- `tests/conftest.py` sets a placeholder `GITHUB_WEBHOOK_SECRET` env var
  purely so importing `backend.api.main` (which builds its module-level
  `app` eagerly by design) doesn't break test collection with no `.env`
  present. No test uses that placeholder's value.

### Deferred / not built at M2 (explicitly out of scope, do not treat as gaps)
- No real Redis/ARQ queue (M3)
- No LangGraph orchestrator, no real agents (M4+)
- No real GitHub App integration (M11) -- signatures are verified against a
  locally-chosen test secret, per PLAN.md's M2 credential note

### Next Phase (M2 -> L4 VERIFY)
A separate agent/model session should run L4 VERIFY against this build:
re-run all four gates plus the demo command independently, check the DoD
gates in DONE.html section 2 relevant to M2 (HMAC verified + idempotency
before any work is enqueued; mypy --strict clean and pytest green; no
context-graph invariant violated), and confirm the architecture-placement
note above (backend.core.settings) is acceptable before marking M2 DONE.

## M1 Build Summary (L4 VERIFY APPROVED)

### Outcome Achieved
✓ 19-subpackage package layout exists with ADR-002 inward-only rule enforced
✓ Finding/Review/WebhookEvent Pydantic v2 contracts defined and unit-tested
✓ 24 comprehensive tests pass (100% pass rate)
✓ Model contracts reject invalid states at construction time
✓ Confidence bounds [0.000, 1.000] with 3-decimal precision enforced

### Gate Results
- `pytest tests/unit/test_models.py -v`: 24 PASSED in 0.06s ✓
- `lint-imports --config .importlinter`: 2 contracts kept, 0 broken ✓
- Code inspection: backend.core and backend.models both verified independent of every sibling package

### Files Written (34 files)
- `pyproject.toml`: Project metadata, Python 3.12 toolchain, dev dependencies
- `backend/models/{enums,findings,review,webhook}.py`: Core contracts (4 files)
- `backend/{core,agents,api,cli,database,economics,evaluation,hitl,integrations,job_queue,memory,models,observability,orchestrator,prompts,reliability,security,tools,webhook_receiver}/__init__.py`: 19-subpackage skeleton
- `tests/{__init__,unit/__init__,integration/__init__,e2e/__init__,contract/__init__,eval/__init__}.py`: Test directory structure
- `tests/unit/test_models.py`: 24 comprehensive test cases
- `.importlinter`: ADR-002 contract configuration (single source of truth; `setup.cfg` and the dead `pyproject.toml` copy were deleted)

### Commits (11 logical)
1. `chore(build): add pyproject.toml with Python 3.11 toolchain`
2. `feat(models): add Severity, AgentType, ReviewStatus enums`
3. `feat(models): add Finding contract with bounded confidence`
4. `feat(models): add Review and WebhookEvent contracts`
5. `feat(arch): establish 22-module monolith with ADR-002 inward-only dependencies` (historical commit subject — actual layout is 19 subpackages, see below)
6. `test(models): comprehensive unit tests for domain contracts`
7. `chore(genesis): checkpoint M1 build complete`
8. `fix(build): repair import-linter config so lint-imports actually runs`
9. `fix(models): clear ruff and mypy --strict findings in domain contracts`
10. `chore(build): move project to python 3.12`
11. `chore(build): pin import-linter to the 1.8 line matching our contract syntax`

### Deferred from M1 (pending user decision)
The L4 verifier confirmed the demo command passes and APPROVEd M1, but flagged the
following model-validation gaps as deliberately deferred rather than blocking:
- `Finding.line_end >= line_start` is not enforced (no cross-field validator)
- `Review.overall_confidence` is not cross-checked against the mean of `findings[].confidence`
- `WebhookEvent.delivery_id` is validated only by length (36 chars), not real UUID format
- Models are not frozen and do not set `validate_assignment`, so instances are mutable post-construction
- `WebhookPullRequest` and `WebhookRepository` in `backend/models/webhook.py` are unused dead code (only `WebhookEvent` is referenced)

### Next Phase (M2)
M2 begins with webhook ingress: HMAC validation and idempotency checking.
Precondition: M1 contracts are stable (they are, 24 tests validate all invariants).
