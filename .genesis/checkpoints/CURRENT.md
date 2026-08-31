# CURRENT
- active_loop: L1 BUILD complete on M10, awaiting L4 VERIFY (separate session)
- target: M10
- iteration: 1
- last_gate: L1 BUILD on M10 (2026-08-31, this session) -- all four gates
  green (`ruff check .`, `mypy --strict backend/`, `pytest -v` run twice
  with identical 317-passed results, `lint-imports`); PLAN.md's exact demo
  command run for real (`ANTHROPIC_API_KEY` configured, real 4-call cost
  $0.026943) -- see "M10 L1 BUILD" below for the full honest summary. NOT
  independently verified yet -- do not treat as APPROVEd.
- next_action: L4 VERIFY on M10 (separate session). Read "M10 L1 BUILD"
  below first, then `.genesis/PLAN.md`'s M10 section for the freeze
  boundary/demo command/success criteria, then judge the demo output's
  actual finding quality honestly (this session's own final report flags
  one real, disclosed remit-bleed imperfection to check).
- model: claude-sonnet-5
- tokens_used: not tracked this session
- tokens_budget: 50000
- skills_loaded: []

## M10 L1 BUILD (this session, 2026-08-31) -- summary for a cold L4 VERIFY session

**Built:** all four specialists real (`backend/agents/{security_agent,
quality_agent,test_agent,docs_agent}.py`, sharing orchestration logic via
new `backend.agents.base_agent.run_specialist_analysis` -- prompt load,
retrieval grounding, LLM call, parse, forced-HITL fallback on parse
failure); retrieval wired into all four prompts (query = changed-symbol
names, falling back to truncated diff text; top_k=5, ≤6000 chars injected,
a retrieval failure degrades to diff-only rather than blocking); M8's
SECURITY-only infrastructure-failure-forces-HITL fix generalized to all
four nodes (`backend/orchestrator/nodes.py`'s `_specialist_node`, one
shared body); `backend/integrations/github_client.py` (mock-backed,
`post_or_queue` guarantees exactly one of post/queue never both);
`backend/cli/review_local.py` (PLAN.md's named demo command);
`backend/job_queue/arq_worker.py` wired into the orchestrator, closing the
gap deferred since M4 (offloaded via `asyncio.to_thread`, same fix applied
three times before in this project for the identical event-loop-blocking
defect class); `tests/fixtures/sample_pr_diff.patch` (3 files, genuinely
targets all four remits).

**BudgetGuard across four agents:** each of the 4 calls independently
calls `BudgetGuard.check_and_raise()` (unchanged M8 mechanism, no
review-scoped fast path) -- proven per-call, and a mid-review exhaustion
(2 of 4 blocked) forces `QUEUED_FOR_HITL` via the same CRITICAL-fallback
mechanism, never a review that silently looks complete
(`tests/integration/test_budget_guard_across_four_agents.py`).

**Two real, disclosed regressions found and fixed in pre-existing,
out-of-M10-freeze-boundary test files** (both caused by M10 legitimately
making QUALITY/TESTS/DOCS real by default): (1)
`tests/integration/test_events_spine.py`'s M7 orchestrator-spans test was
about to silently quadruple its real, unbudgeted Anthropic spend on every
ordinary `pytest` run with a key configured -- confirmed via `agent_events`
before the fix, fixed by installing fake LLM clients for all four
specialists. (2) `tests/integration/test_hybrid_retrieval.py`'s held-out
vector-alone recall@5 baseline shifted by one query (id 16,
`reconstruct_review_trace`) because M10's own new source files grew the
real-OpenAI-embedded corpus from 382 to 471/473 chunks -- the same
single-occurrence-identifier dilution-at-scale limitation M9 already
measured, re-confirmed by a second corpus-growth event; re-baselined with
full dated reasoning in place, per that test class's own stated
amendment discipline. See `git log` (both fixes are their own commits)
for the full reasoning; neither is a silent loosening.

**Demo output, judged honestly:** 14 real, schema-valid findings
(SECURITY 5, QUALITY 2, TESTS 4, DOCS 3), `status=QUEUED_FOR_HITL`,
`overall_confidence=0.896` (forced HITL by two CRITICAL findings despite
high average confidence -- gate working correctly). All 14 land on real,
correctly-identified defects in the fixture diff (SQL injection, hardcoded
secret, missing authz, bare-except swallowing an `UnboundLocalError`,
three genuine test-coverage gaps, the `is_member` signature/docstring
drift, plus two reasonable unplanted DOCS observations) -- not generic
filler. One real imperfection worth L4 VERIFY's attention: TESTS flagged
the hardcoded-credential issue too (a mild remit bleed into SECURITY's
territory, framed through a "this should be validated by a test" angle);
and SECURITY/QUALITY both separately flagged the same bare-except/
uninitialized-variable bug from their own angles (defensible -- it is
genuinely both a security-relevant and a quality-relevant defect -- but
real overlap, not zero). Full per-finding list and reasoning in this
session's final report.

**API spend (real, this session):** demo command four calls, measured
exactly via `agent_events`: $0.026943 (haiku-4-5, 12,843 in / 2,820 out
tokens). `tests/integration/test_all_agents_live.py`'s 4 calls ran twice
(once per full-suite `pytest` run) since `ANTHROPIC_API_KEY` was
configured -- not measured exactly (no `review_id` passed, matching
`test_security_agent_live.py`'s own pre-existing pattern), estimated small
(haiku-tier, ~1-2K tokens each). `test_hybrid_retrieval.py`'s real-OpenAI
corpus re-embed (473 chunks) ran once per full traversal of that file's
two test classes in the same process (~5 times across this session's
debugging) at an estimated ~$0.024 each -- unavoidable by that fixture's
own documented design (the fixture-backend class always runs first within
one process and contaminates the marker), not something this session
could have prevented without touching M9's own file more invasively. See
final report's API_SPEND section for the full breakdown.

## Housekeeping note (2026-08-31 POST-APPROVE CLOSEOUT)

This file had grown to ~2450 lines, most of it superseded per-session
narrative: four "kept for context; superseded by the header above/below"
duplicate history blocks (M5/M7/M8 x2) that fully restated information
already carried in `.genesis/PLAN.md`'s Progress log, and seven
milestone-by-milestone "Build Summary" sections (M1 through M8) that
likewise duplicated PLAN.md's Progress entries and
`.genesis/implementation-notes.html`'s live-capability table almost
line for line. Both categories were pruned in this closeout pass --
roughly 1900 lines removed, none of it a Deferred item or a record of
a past REJECT/fix, all of which are kept below in full. If you are a
cold session and need the detailed history of any milestone M1-M9, it
lives in `.genesis/PLAN.md`'s "## Progress" section (one dated entry
per milestone, written for exactly this purpose) and in
`.genesis/explanations/*.html` (one rich walkthrough per milestone);
this file is deliberately no longer the place for that level of detail.

## M9 final state (Hybrid Retrieval / Local Vector Memory) -- DONE

Condensed from the full investigation history (previously ~1300 lines in
this file's M9 sections; see `.genesis/PLAN.md`'s M9 entry in "##
Progress" for the equally-complete narrative account, and
`.genesis/explanations/2026-08-31-explanation-m9.html` for the full
teaching walkthrough).

**Arc:** An interrupted L1 BUILD was resumed and completed
(`backend/memory/{embedder,context_retriever,tiger_client}.py`,
the pgvector service, the AST-based `scripts/seed_code_chunks.py`
seeder). A first, independent L4 VERIFY **REJECTED** it: PLAN.md's own
success criteria named a specific "recall@5 on a 10-query fixture set is
100%" bar with no such fixture actually built, and the demo command's own
test fixture truncated `code_chunks` before ever querying what the demo's
own seed step had just inserted (not actually end-to-end). An L2 DEBUG
loop built the named, git-committed 10-query fixture
(`tests/fixtures/retrieval_queries.json`) and a non-truncating,
self-seeding test class, and reported the honest result: **4/10 (40%)**
on the fixture embedder, not 100% -- root-caused (the
`DeterministicFixtureEmbedder`'s summed-then-normalized bag-of-tokens
design dilutes a single-occurrence identifier below the corpus noise
floor at real scale; `route_review`'s own definition chunk's true vector
rank was 117th of 378, cosine 0.032, below the ~0.0625 magnitude two
unrelated random 256-dim vectors correlate at by chance). A second,
independent L4 VERIFY session re-ran everything and **APPROVEd** M9
against this honestly-reported number.

A follow-up session obtained a funded OpenAI credential and re-measured
the same 10 queries on real `text-embedding-3-large` embeddings: **7/10
(70%)** -- real embeddings rescued 3 of the fixture embedder's 6 misses,
lost none of its hits, still short of the literal 100% bar. This tied
fusion and vector-alone at 7/10 each on this small set, leaving open
whether RRF fusion earns its complexity over vector-alone at all.

**The fusion-vs-vector-alone investigation:** a held-out 15-query set
(`tests/fixtures/retrieval_queries_holdout.json`) was written and
git-committed *before* it was ever queried. 13 fusion variants (weighted
RRF at several vector:fts ratios, larger candidate pools, alternate `k`
values, score-based fusion) were tuned using ONLY the original 10, and a
single winner was chosen by a rule pre-registered before the held-out set
was touched. The winner scored 8/10 on the tuning set -- and then
**failed to generalize**: on the held-out 15 it only tied the untuned
default at recall@5 (11/15 each) and was worse at recall@10 (12/15 vs
13/15), confirming it had fit noise in the 10-query tuning set. It was
rejected and never shipped; `HybridRetriever`/`reciprocal_rank_fusion`
are unchanged from the first APPROVE. Not shipping the tuned variant is
recorded here as the correct engineering outcome, not a missed
opportunity.

**Statistical honesty (the headline finding of this closeout session):**
the held-out measurement showed the current, untuned RRF beating
vector-alone (73% vs 60% recall@5, 87% vs 67% recall@10) -- but a formal
McNemar test on those same discordant pairs found neither gap
statistically significant: recall@5 has 2 discordant pairs, both
favoring RRF, p = 0.5; recall@10 has 3 discordant pairs, all favoring
RRF, p = 0.25. The direction is one-sided everywhere it was formally
tested (0 losses for RRF at either k on the held-out set), but n=15 is
too small for that one-sidedness to be significant. A nuance
`PLAN.md`'s earlier AMENDED line omitted: on the ORIGINAL 10-query set,
RRF actually underperforms vector-alone at recall@3 (5/10 vs 6/10) --
the one case where the direction runs against fusion outright, not
merely short of significance. `PLAN.md`'s M9 success-criteria section
carries the full, dated correction on top of (not replacing) the
AMENDED line it corrects.

A second, independent L4 VERIFY session **APPROVEd** the investigation,
having independently re-seeded the corpus itself from scratch (not
trusting the maker's own seeded state) and reproduced every recall
number and every individual miss-id reported above. `pytest -v` exits 0
with 262 passed, run twice with identical results, Redis (6380),
Postgres (5433), and pgvector (5434) all up.

**Real cost incurred across the M9 investigation, measured/estimated
directly (see `.genesis/PLAN.md`'s M9 Progress entry and this session's
own GATE_RESULTS for exact per-session figures):** on the order of
$0.10-0.15 total in OpenAI embedding spend across all M9 sessions
combined (re-seeds of the ~382-389-chunk corpus at ~$0.02 each, plus
query embeds at negligible additional cost).
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
- ~~Live demo BLOCKED on credential~~ -- now CLOSED: `ANTHROPIC_API_KEY` in
  `.env` was rotated to a valid one and PLAN.md's exact M8 demo command
  (`ANTHROPIC_API_KEY=... python -m backend.agents.security_agent --diff
  tests/fixtures/sqli_diff.patch`, run via `.env`, no literal key on the
  command line) was run for real: exit 0, 2 schema-valid CRITICAL
  `sql_injection` findings (confidence 0.999 each), both real SQL-injection
  sinks in the fixture diff correctly identified, no hallucinations. A
  second real call with `review_id="m8-closeout-demo2"` produced a real
  `llm.call` `agent_events` row (797 in / 273 out tokens, cost $0.002162,
  hand-checked exactly against claude-haiku-4-5 pricing, latency 2735ms),
  and `BudgetGuard.current_spend_usd()` returned a real $0.002408, correctly
  excluding the future-dated 2030 fixture rows. See the header block above
  and `.genesis/explanations/2026-08-30-explanation-m8.html` for the full
  account.

## Deferred

New from M9 POST-APPROVE CLOSEOUT (2026-08-31), non-blocking:
- **A larger, blind query set (30+ queries) would be needed to establish
  fusion's recall benefit over vector-alone with statistical confidence.**
  The held-out 15-query set's fusion-vs-vector-alone gap (73% vs 60%
  recall@5, 87% vs 67% recall@10) is directionally one-sided (0 losses for
  RRF at either k, per a formal McNemar test) but NOT statistically
  significant at n=15 (recall@5 p=0.5, recall@10 p=0.25 -- see PLAN.md's
  M9 statistical-correction line and this file's "M9 final state" section
  above for the full numbers). A future session revisiting retrieval
  quality should build a third, larger, blindly-chosen query set
  (30+ queries, committed before being run, same discipline as the
  existing 10/15-query sets) specifically to get enough discordant pairs
  for McNemar (or an equivalent test) to actually reach significance,
  rather than tuning further on the existing small sets.
- **`OpenAIEmbedder`'s real API path has since been exercised against the
  live OpenAI endpoint** (see the now-resolved item under "New from M9 L2
  DEBUG" below) -- noted here so a future reader does not re-flag it as
  outstanding.

New from M9 L2 DEBUG (this session, 2026-08-30), non-blocking except where noted:
- **Recall@5 on the real 10-query fixture set is 40% (4/10), not the 100%
  PLAN.md's success criteria literally asks for.** This is now a directly
  measured, honestly-reported fact (`tests/fixtures/retrieval_queries.json`
  + `TestRecallOnRealSeededCorpus::
  test_recall_at_five_across_the_ten_query_fixture_set`'s own docstring
  has the full per-query root-cause breakdown), not a gap in test
  coverage -- the previous "PLAN.md names a 10-query fixture set... a
  future session would need to build one" Deferred item is REMOVED as of
  this session; the fixture now exists and has been run for real. The
  root cause is `DeterministicFixtureEmbedder`'s summed-then-L2-normalized
  bag-of-tokens design: a single occurrence of even an exact, corpus-
  unique identifier gets diluted below the corpus's own incidental noise
  floor once the corpus has hundreds of chunks (confirmed directly:
  `route_review`'s own definition chunk's true vector rank is 117th of
  378, cosine similarity 0.032 -- BELOW the ~0.0625 magnitude two
  unrelated random 256-dim vectors correlate at by pure chance), while a
  chunk that CALLS the target function several times (mostly test files)
  repeats the exact compound identifier token repeatedly and so outranks
  the single-occurrence definition site in both full-text cover-density
  and vector token-sum weight. `HybridRetriever`'s SQL and
  `reciprocal_rank_fusion`'s arithmetic were verified directly to do
  exactly what they are specified to do -- this is a fixture-embedder
  scaling limitation, not a retriever bug, and this session did not
  change `backend/memory/context_retriever.py`. A larger candidate pool
  was tried experimentally (up to 100 candidates, over a quarter of the
  corpus) and only recovers 2 of the 6 misses (plateaus at 6/10) while
  defeating the pool's own documented purpose -- tried and rejected, not
  left untried. This is the single most concrete, load-bearing piece of
  evidence yet for the next Deferred item below (previously a plausible
  but unverified concern; now directly measured). Re-validate once a real
  `OpenAIEmbedder` credential exists -- a trained model should not exhibit
  this same single-occurrence dilution. **[RESOLVED 2026-08-31: re-validated
  against real OpenAI embeddings -- real recall@5 on the same 10 queries is
  7/10 (70%), rescuing 3 of these 6 fixture-embedder misses (ids 1, 5, 8)
  and losing none of its hits; see "M9 final state" above and
  `.genesis/PLAN.md`'s M9 section for the full per-query breakdown.]**
- **The fixture embedder's demonstrated properties (synonym
  canonicalization, short-token filtering, AND -- newly measured this
  session -- single-occurrence-identifier dilution at real corpus scale)
  are engineered/measured artifacts of a hashed bag-of-tokens design, not
  learned semantics** -- see this checkpoint's M9 L2 DEBUG entry above for
  the full fixture-vs-real distinction and the concrete numbers.
  `TestKeywordSearchFindsWhatVectorMisses`'s assertion that a real
  embedding model would also rank a short token like "s3" last remains a
  plausible but unverified claim about real subword tokenization
  behavior, not something this session could prove without a key.
- **`OpenAIEmbedder`'s real API path has never been exercised against the
  live OpenAI endpoint** -- there is no OpenAI credential available for
  this build. Its retry/circuit-breaker/timeout composition is proven by
  fault injection against an injected fake client
  (`tests/unit/test_embedder.py::TestReliabilityComposition`), the same
  pattern `AnthropicLLMClient` used before M8 obtained a real credential,
  but the actual `text-embedding-3-large` call, its real latency/error
  shapes, and whether `dimensions=256` truncation behaves as documented
  have not been proven end-to-end. Re-validate once a key is available.
  **[RESOLVED 2026-08-31: a funded OpenAI key was obtained; the real
  `text-embedding-3-large` endpoint was called for real (confirmed via a
  real, billable `POST /v1/embeddings` returning HTTP 200, a 256-dim
  non-zero vector, real tokens billed), the corpus was re-seeded with real
  embeddings multiple times across sessions, and `dimensions=256`
  truncation was confirmed directly against stored vectors -- see "M9
  final state" above.]**
- **`_CANDIDATE_POOL_MULTIPLIER=4`/`_MIN_CANDIDATE_POOL=20`
  (`backend/memory/context_retriever.py`) are judgment calls**, sized for
  this milestone's few-hundred-row local corpus and, as of this session,
  DIRECTLY SHOWN to be too small even at this milestone's own ~378-chunk
  corpus for several real single-occurrence-identifier queries (see the
  40% recall entry above) -- and increasing the pool substantially
  (tested up to 100) does not fully close the gap either. Not changed
  this session (see rationale above); a future session should not assume
  a bigger pool is the fix.
- **`backend/memory/tiger_client.py`'s `apply_migrations`/`connect` have
  no retry/circuit-breaker wrapping of their own** -- consistent with
  `backend.database.postgres`'s equivalent M7 pattern for local Postgres
  (a short-lived, low-frequency connection, not a per-request hot path),
  but worth re-confirming this stays the right scope boundary once M12
  replaces this file's internals with the real Tiger Cloud path.
- **`tests/integration/test_budget_guard_events.py` (M8's test, unrelated
  to M9) has a real cross-run test-isolation bug**, discovered
  incidentally while running this session's full `pytest -v` gate: its
  fixture rows are pinned to one hardcoded calendar day and never cleaned
  up across separate `pytest` process invocations against this project's
  long-lived local Postgres container, so accumulated rows from many past
  sessions (153 `budget-guard-%` rows, spanning 2020-2030, confirmed by
  direct inspection) now make
  `test_events_from_a_previous_day_are_not_counted` fail on an unrelated
  threshold it checks. NOT fixed in this session (out of scope -- it's
  M8's test file, and the responsible cleanup needs a privileged DB
  connection this session's safety controls correctly declined to use
  ad hoc). Flagged as a separate task; see this session's final report's
  ANOMALIES section.

New from M8 (L1 BUILD), non-blocking except where noted:
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

