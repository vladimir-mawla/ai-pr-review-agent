# CURRENT
- active_loop: L2 DEBUG
- target: M5
- iteration: 1
- last_gate: L4 VERIFY REJECTED M5 (separate session, real safety bug) -- this L2 DEBUG session applied the approved fix; awaiting a re-run of L4 VERIFY
- last_action: L4 VERIFY REJECTED M5's original L1 BUILD for a real safety bug: `backend.agents.contracts.dedupe_findings` keyed on `(file_path, line_start)` and kept the highest-CONFIDENCE finding with severity playing no role at all, while `backend.hitl.queue.has_critical_finding` only ever inspects the POST-dedupe list. Verified repro: a SECURITY/CRITICAL finding (confidence 0.751) and a DOCS/INFO finding (confidence 0.752) colliding on the same file+line caused dedupe to keep the INFO finding and drop the CRITICAL one; `route_review` then saw no CRITICAL finding and returned POSTED, auto-posting a review whose real CRITICAL finding had been silently discarded -- violating the project's invariant that a CRITICAL finding always requires human review. This L2 DEBUG session applied the user-approved fix: `_is_better` in `backend/agents/contracts.py` now orders SEVERITY FIRST (via a new explicit `SEVERITY_RANK` map added to `backend/models/enums.py` -- deliberately not relying on `Severity`'s enum declaration order or member name, per the user's instruction), and only falls back to confidence, then the pre-existing `AGENT_PRECEDENCE`/category/rationale tie-breaks, within the same severity. `backend.models.findings.Finding.__lt__` was also refactored to use the same `SEVERITY_RANK` map instead of its own locally-duplicated severity-order dict, removing a second place the ranking could have drifted from the fix. Every docstring/comment claiming dedupe "keeps highest confidence" was rewritten (module docstring and `_is_better`'s docstring in `backend/agents/contracts.py`, `aggregate_node`'s docstring in `backend/orchestrator/nodes.py`, `tests/unit/test_aggregator.py`'s module docstring, `.genesis/DONE.html` section 2's M5 gate, `.genesis/PLAN.md`'s M5 outcome/success-criteria) to state the real severity-first rule. Added a true end-to-end regression test (`tests/unit/test_aggregator.py::TestDedupeAndRoutingInteraction::test_end_to_end_critical_survives_dedupe_and_forces_hitl`) that builds the exact reported CRITICAL/0.751-vs-INFO/0.752 collision, runs it through `dedupe_findings` then `route_review`, and asserts `QUEUED_FOR_HITL` with the CRITICAL finding surviving dedupe -- proven to FAIL against the old confidence-only ordering (temporarily reverted, ran, captured the failure, restored the fix, reran, captured the pass; both outputs pasted in this session's final report) so the test is proven to actually catch the bug it exists to catch, not just pass vacuously. Also closed the test-design gap that let the original bug through: added `TestSeverityBeforeConfidenceDedupe` (parametrized over every severity pair where the higher-severity finding has lower confidence, plus a permutation-based property test asserting a CRITICAL finding is never dropped across every ordering of a 5-finding collision) and `TestDedupeAndRoutingInteraction` (dedupe's actual output piped into `route_review`, both orderings, plus a contrast case confirming non-CRITICAL severity wins still follow the ordinary confidence-threshold rule rather than an accidental blanket "severity wins therefore always HITL"). Did NOT change: `route_review`'s empty-findings-list behavior (still correctly routes to HITL at confidence 0.000, per the user's explicit decision that this is a deliberate conservative choice, not a defect -- a clarifying comment was considered but `route_review`'s existing threshold-comparison logic already handles it via the ordinary `overall_confidence < threshold` path with no special-casing, so no code change was needed there); and did NOT widen the dedup key to include `line_end` (also an explicit user decision -- see Deferred, below). All four gates plus PLAN.md's exact M5 demo command were re-run after the fix; see this session's full report for pasted output and exit codes.
- next_action: re-run L4 VERIFY on M5 (separate session)
- model: claude-sonnet-5
- tokens_used: 0
- tokens_budget: 50000
- skills_loaded: []

## Deferred

New from M5 L4 VERIFY REJECT + L2 DEBUG fix (this session), non-blocking:
- **Known deferred gap, explicitly not fixed by user decision:** `dedupe_findings`'s key is still exactly `(file_path, line_start)`, not `(file_path, line_start, line_end)`. L4 VERIFY separately flagged that a wide-span finding (e.g. `line_start=42, line_end=80`) can collapse into an unrelated, narrower finding that merely shares `line_start=42`. The user explicitly decided NOT to widen the key for this fix -- adding `line_end` does not solve the general "do these two findings' spans actually overlap" problem (two spans can overlap without sharing either endpoint), so it would be a partial fix wearing a full fix's clothes. Left as a known, tracked gap for a future milestone to address properly (e.g. real interval-overlap detection), not silently dropped.
- **Deliberate, non-defect behavior (documented so a future reader does not "fix" it):** an empty findings list still routes to HITL (`route_review(Decimal("0.000"), [], threshold=...)` -> `QUEUED_FOR_HITL`, since 0.000 is below any sane threshold). This is a deliberate conservative choice per the user's explicit instruction, not an oversight -- "no findings" should never look indistinguishable from "confidently nothing to report" and silently auto-post. Already covered by `tests/unit/test_hitl_gate.py::test_empty_findings_list_is_handled_sanely`.

New from M5 (L1 BUILD), non-blocking:
- Three files outside PLAN.md's literal M5 freeze-boundary list were touched: `backend/models/review.py` (+ `backend/models/__init__.py`, `tests/unit/test_models.py`), `backend/orchestrator/state.py`, and `tests/integration/test_orchestrator_fanout.py`. Each was a minimal, targeted change the L1 BUILD driver's own instructions explicitly called for (closing the M1 `overall_confidence` gap; giving the real aggregator's join node somewhere in `GraphState` to write its result; proving that wiring actually works end-to-end) rather than scope creep -- flagging for L4 VERIFY to confirm this reading is acceptable, same as M2/M4's own documented freeze-boundary/architecture notes.
- `mypy --strict backend/` required one call site (`backend/orchestrator/nodes.py`'s new `Review(...)` construction) to pass `error_message=None` explicitly, because mypy's `dataclass_transform`-based reading of pydantic v2 models does not recognize `Field(None, ...)` (a positional default, as opposed to a plain `= None` class-body default) as making a field optional in the synthesized constructor signature. This is a latent M1-era pattern (`Review.error_message` and `Finding` fields use `Field(None, ...)` throughout) that only surfaced now because M5 is the first *backend* production code to construct a `Review` -- every prior `Review(...)` call was in `tests/`, which `mypy --strict` does not scope. Not fixed at the model level (would touch M1's field style repo-wide, well outside M5's freeze boundary); worth revisiting if `mypy --strict` is ever extended to `tests/`.
- `InMemoryHitlQueue` (`backend/hitl/queue.py`) is a real, tested class, but nothing in `backend.orchestrator.nodes.aggregate_node` actually calls `.enqueue()` on an instance of it -- the join node computes the correct `Review.status` (POSTED vs QUEUED_FOR_HITL) and `routing_reason`, but stops there. Wiring "a QUEUED_FOR_HITL review is actually pushed into a live queue something else can read" is left for a later milestone (M10's full local dry run, or wherever a durable/dashboard-visible queue is built), matching M5's explicit exclusion of GitHub posting (M11) and consistent with M4's own precedent of not wiring the orchestrator into the webhook/queue path.
- The dedup tie-break's final fallback level (lexicographic `rationale` comparison, after confidence -> AGENT_PRECEDENCE -> category) is untested in isolation (only the category-level tie-break is exercised in `tests/unit/test_aggregator.py`) since triggering it requires two findings identical in every field except `rationale`, a case that cannot arise from M4/M5's one-finding-per-agent-per-run stubs. The logic is straightforward (one more lexicographic comparison in the same chain) but this specific level has no direct regression test.

Resolved (previously deferred from M1, now closed):
- ~~`Review.overall_confidence` has no cross-field consistency check (not cross-checked against the mean of `findings[].confidence`)~~ -- fixed at M5: `compute_overall_confidence(findings)` is the one formula (mean, ROUND_HALF_UP to 3 decimal places, 0.000 for an empty list), and `Review` has a `model_validator(mode="after")` that raises `ValidationError` if `overall_confidence` disagrees with it. A validator (reject on mismatch) was chosen over silently recomputing the field, so a caller's bug surfaces loudly at construction time instead of being silently discarded.

Still open from M1:
- `Finding`, `Review`, `WebhookEvent` are not frozen and do not set `validate_assignment`, so instances are mutable post-construction

Still open from M2:
- No max request body size is configured, so a large POST is fully buffered and hashed -- address before M11 internet exposure
- `backend/core/settings.py` placement is an accepted ADR-002 taxonomy nit, not a layering violation

Still open from M3 (and M3's L4 VERIFY), all non-blocking:
- No FastAPI lifespan hook calls `RedisJobQueue.close()`, so the background event-loop thread is simply abandoned on process shutdown. Harmless in practice (it's a daemon thread and the process is exiting anyway), but untidy -- a lifespan hook should call `close()` for a clean shutdown.
- A Redis-down `enqueue()` call currently surfaces as an unhandled 500 rather than a graceful 503. Acceptable for M3's local-dev scope; revisit before this endpoint carries real traffic.
- Idempotency state (and the queue) is lost whenever the Redis container is recreated (`docker compose down && up`, no volume) -- confirmed empirically (DBSIZE drops to 0). This satisfies M3's own success criterion (no orphaned jobs) but the idempotency-reset corollary is documented in `docker-compose.yml` and `README.md`.

New from M4 (L1 BUILD + L4 VERIFY), still open:
- LangGraph logs a deserialization warning on checkpoint resume: "Deserializing unregistered type backend.models.enums.AgentType / Severity / backend.models.findings.Finding from checkpoint. This will be blocked in a future version." Our custom Pydantic/enum types are not registered with LangGraph's (de)serializer, so a future LangGraph major could break checkpoint resume for them entirely unless `allowed_msgpack_modules` (or an equivalent registration mechanism) is configured. Observed and recorded by L4 VERIFY; noted in `backend/orchestrator/langgraph_engine.py` near the checkpointer setup. Not blocking today -- resume works -- but should be addressed before relying on a newer LangGraph release.
- `LangGraphWorkflowEngine`'s default checkpoint DB path (`var/orchestrator_checkpoints.sqlite3`) is never exercised outside `tmp_path`-scoped tests -- there is no evidence one way or the other that the default path itself works end-to-end outside of tests; only the mechanism (SqliteSaver against a real file, and resume-across-a-new-engine-instance) was verified.
- The orchestrator built at M4 is not yet wired into the webhook/queue path: nothing in `backend.webhook_receiver` or `backend.job_queue` calls `LangGraphWorkflowEngine`. That wiring is expected to land in a later milestone, not a gap in M4's own scope.

Resolved (previously deferred from M2, now closed):
- ~~`_is_hex` uses `int(value, 16)` which accepts underscore separators and a leading sign~~ -- fixed: replaced with a strict `[0-9a-fA-F]+` charset regex; regression test added (`test_underscore_in_digest_is_rejected_as_malformed_not_invalid`)
- ~~The demo command needs an activated venv and a hand-created `.env` and neither is documented (no README exists)~~ -- fixed: README.md now documents venv creation/activation, `pip install -e ".[dev]"`, and copying `.env.example` to `.env`
- ~~`InMemoryJobQueue` and its `_seen_delivery_ids` grow unboundedly with no eviction~~ -- fixed: M3's `RedisJobQueue` stores the idempotency key with an expiring TTL (`Settings.idempotency_ttl_seconds`, default one week) instead of an ever-growing in-process set; proven by a test that reads the TTL back from Redis directly.

## M5 Build Summary (awaiting L4 VERIFY)

### Outcome Achieved
- `backend.agents.contracts.dedupe_findings` merges the four specialists'
  findings, collapsing exact `(file_path, line_start)` duplicates and
  keeping the highest-confidence one, with a fully deterministic tie-break
  (confidence -> `AGENT_PRECEDENCE` -> category -> rationale) that does not
  depend on the input list's order -- proven by running the same two
  findings through in both orders and asserting identical output.
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
M5-relevant gate ("Findings dedupe by file and line, keeping highest
confidence; aggregator threshold in code matches the threshold in its
user-facing message"), rule on the three freeze-boundary exceptions above,
and confirm the M1 gap closure's chosen approach (a rejecting
`model_validator`, not a silent recompute) is acceptable before marking M5
DONE.

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
