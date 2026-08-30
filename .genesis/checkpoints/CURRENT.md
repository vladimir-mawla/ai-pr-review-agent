# CURRENT
- active_loop: none (between milestones)
- target: M4
- iteration: 0
- last_gate: L1 BUILD complete on M4 (all four gates + demo command green; not yet L4 VERIFYed)
- last_action: L1 BUILD session built the M4 LangGraph orchestrator skeleton: backend/core/workflow_engine.py (ADR-001 abstract WorkflowEngine Protocol), backend/orchestrator/{state,nodes,graph,langgraph_engine}.py, and tests/integration/test_orchestrator_fanout.py. Smoke-tested langgraph-checkpoint-redis directly against this project's real docker-compose Redis before writing any production code; it failed (RediSearch/FT.INFO not supported by plain redis:7-alpine), so used langgraph-checkpoint-sqlite instead (a real, first-party LangGraph checkpointer, file-backed, not in-memory) and documented why in langgraph_engine.py's module docstring and the pyproject.toml commit. Validated LangGraph's Send-API fan-out is genuinely parallel (measured: ~0.31s wall time vs 1.2s sum of four 0.3s sleeps, all four execution windows pairwise overlapping) and that its pending-writes mechanism actually skips already-completed parallel branches on resume (prototyped standalone before writing production code, then proven again in the real test suite via a brand-new engine instance against the same on-disk checkpoint file). All four gates (ruff, mypy --strict, pytest -v [62 passed], lint-imports) plus PLAN.md's exact M4 demo command ran green. Context graph refreshed (53->68 nodes, 108->146 edges); all 4 hand-written invariants backed up before graphizer ran and restored/verified after. Redis (M3's, port 6380) brought up only for the full pytest -v gate run (queue-roundtrip tests need it), then torn down; ampliphi-redis-1/ampliphi-postgres-1 confirmed untouched throughout.
- next_action: L4 VERIFY on M4 (separate session)
- model: claude-sonnet-5
- tokens_used: 0
- tokens_budget: 50000
- skills_loaded: []

## Deferred

New from M4 BUILD, recorded for the verifier:
- PLAN.md's M4 outcome text says the graph "checkpoints state to Redis"; this build uses langgraph-checkpoint-sqlite instead, not Redis. This is a deliberate, documented deviation (langgraph-checkpoint-redis needs RediSearch, which this project's plain redis:7-alpine does not have -- confirmed by an actual ResponseError, not assumed), not a silent substitution. See backend/orchestrator/langgraph_engine.py's module docstring for the full reasoning. A verifier should confirm this reading is acceptable, or that a future milestone should add Redis Stack to docker-compose.yml if Redis-backed checkpointing specifically (not just "a durable checkpointer") is actually required.
- The orchestrator built at M4 is a standalone skeleton: nothing in backend/webhook_receiver or backend/job_queue calls into LangGraphWorkflowEngine yet. That wiring (webhook -> queue -> orchestrator run) is not part of M4's freeze boundary and is expected to land in a later milestone (M5 aggregator or M10 full dry-run), not a gap in this build.
- LangGraphWorkflowEngine's default checkpoint DB path (var/orchestrator_checkpoints.sqlite3) is never exercised by the test suite (every test uses a tmp_path-scoped file) or created by this session's gate runs -- there is no evidence one way or the other that the default path itself works end-to-end outside of tests; only the mechanism (SqliteSaver against a real file, and the resume-across-a-new-engine-instance behavior) was verified.
- aggregate_node is an intentional no-op (M5's aggregation logic is out of scope), so GraphState.node_errors and GraphState.findings pass through it unchanged; this is expected, not an oversight.

Still open from M1:
- `Review.overall_confidence` has no cross-field consistency check (not cross-checked against the mean of `findings[].confidence`)
- `Finding`, `Review`, `WebhookEvent` are not frozen and do not set `validate_assignment`, so instances are mutable post-construction

Still open from M2:
- No max request body size is configured, so a large POST is fully buffered and hashed -- address before M11 internet exposure
- `backend/core/settings.py` placement is an accepted ADR-002 taxonomy nit, not a layering violation

New from M3 BUILD, recorded for the verifier (still true, not superseded):
- `RedisJobQueue.enqueue()` bridges into ARQ's async client via a dedicated background thread + `asyncio.run_coroutine_threadsafe`, which works but means every enqueue call blocks the calling (request) thread on a cross-thread round trip; acceptable at M3's scale, worth revisiting if webhook volume ever makes that latency matter.
- The docker-compose Redis is published on host port 6380, not Redis's usual 6379, because 6379 was already bound by an unrelated project's container on the build machine. `REDIS_URL`/`docker-compose.yml` are internally consistent with each other, but anyone reusing 6379 elsewhere should update both.

New from M3's L4 VERIFY, still open (all non-blocking -- did not block APPROVE):
- No FastAPI lifespan hook calls `RedisJobQueue.close()`, so the background event-loop thread is simply abandoned on process shutdown. Harmless in practice (it's a daemon thread and the process is exiting anyway), but untidy -- a lifespan hook should call `close()` for a clean shutdown.
- A Redis-down `enqueue()` call currently surfaces as an unhandled 500 rather than a graceful 503. Acceptable for M3's local-dev scope; revisit before this endpoint carries real traffic.
- Idempotency state (and the queue) is lost whenever the Redis container is recreated (`docker compose down && up`, no volume) -- confirmed empirically (DBSIZE drops to 0). This satisfies M3's own success criterion (no orphaned jobs) but the idempotency-reset corollary is now documented in `docker-compose.yml` and `README.md`.

Still open, carried forward (unchanged by this housekeeping pass):
- `Review.overall_confidence` cross-field consistency check (M1)
- Models not frozen / no `validate_assignment` (M1)
- No max request body size configured -- address before M11 internet exposure (M2)
- `backend/core/settings.py` placement taxonomy nit (M2, accepted, not a layering violation)

Resolved (previously deferred from M2, now closed):
- ~~`_is_hex` uses `int(value, 16)` which accepts underscore separators and a leading sign~~ -- fixed: replaced with a strict `[0-9a-fA-F]+` charset regex; regression test added (`test_underscore_in_digest_is_rejected_as_malformed_not_invalid`)
- ~~The demo command needs an activated venv and a hand-created `.env` and neither is documented (no README exists)~~ -- fixed: README.md now documents venv creation/activation, `pip install -e ".[dev]"`, and copying `.env.example` to `.env`
- ~~`InMemoryJobQueue` and its `_seen_delivery_ids` grow unboundedly with no eviction~~ -- fixed: M3's `RedisJobQueue` stores the idempotency key with an expiring TTL (`Settings.idempotency_ttl_seconds`, default one week) instead of an ever-growing in-process set; proven by a test that reads the TTL back from Redis directly.

## M4 Build Summary (L1 BUILD complete, needs L4 VERIFY)

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
