# CURRENT
- active_loop: none (between milestones)
- target: M3
- iteration: 0
- last_gate: L1 BUILD complete on M3 (all four gates + demo command green in-session; L4 VERIFY not yet run)
- last_action: Built M3 -- RedisJobQueue (backend/job_queue/redis_arq.py) swapped in behind the unchanged JobQueue Protocol from M2, backed by an atomic Redis `SET NX EX` idempotency key with a configurable TTL (default 7 days, comfortably outliving GitHub's ~24h redelivery window) that fixes the M2-deferred unbounded-growth finding. Added the ARQ worker (backend/job_queue/arq_worker.py, a minimal stub handler), docker-compose.yml (redis:7-alpine, published on host port 6380 -- 6379 was occupied by an unrelated project's own container on this machine), and settings-driven backend selection (JOB_QUEUE_BACKEND) in backend/api/main.py. Added tests/integration/test_queue_roundtrip.py (9 tests: enqueue-then-consume via a real ARQ Worker in burst mode, idempotency, an actual TTL readback from Redis, and a JobQueue contract test parameterized over both implementations) -- all passed against a real dockerized Redis in-session. All four gates (ruff, mypy --strict backend/, pytest, lint-imports) green; PLAN.md's M3 demo command run verbatim, combined exit 0. Context graph refreshed (53 nodes/108 edges); graphizer wiped the 4 hand-written invariants again (as it did at M2) and they were restored from git history.
- next_action: L4 VERIFY on M3 (separate session)
- model: claude-sonnet-5
- tokens_used: 0
- tokens_budget: 50000
- skills_loaded: []

## Deferred

Still open from M1:
- `Review.overall_confidence` has no cross-field consistency check (not cross-checked against the mean of `findings[].confidence`)
- `Finding`, `Review`, `WebhookEvent` are not frozen and do not set `validate_assignment`, so instances are mutable post-construction

Still open from M2:
- No max request body size is configured, so a large POST is fully buffered and hashed -- address before M11 internet exposure
- `backend/core/settings.py` placement is an accepted ADR-002 taxonomy nit, not a layering violation

New from M3, still open:
- `RedisJobQueue.enqueue()` bridges into ARQ's async client via a dedicated background thread + `asyncio.run_coroutine_threadsafe`, which works but means every enqueue call blocks the calling (request) thread on a cross-thread round trip; acceptable at M3's scale, worth revisiting if webhook volume ever makes that latency matter.
- The docker-compose Redis is published on host port 6380, not Redis's usual 6379, because 6379 was already bound by an unrelated project's container on the build machine. `REDIS_URL`/`docker-compose.yml` are internally consistent with each other, but anyone reusing 6379 elsewhere should update both.

Resolved (previously deferred from M2, now closed):
- ~~`_is_hex` uses `int(value, 16)` which accepts underscore separators and a leading sign~~ -- fixed: replaced with a strict `[0-9a-fA-F]+` charset regex; regression test added (`test_underscore_in_digest_is_rejected_as_malformed_not_invalid`)
- ~~The demo command needs an activated venv and a hand-created `.env` and neither is documented (no README exists)~~ -- fixed: README.md now documents venv creation/activation, `pip install -e ".[dev]"`, and copying `.env.example` to `.env`
- ~~`InMemoryJobQueue` and its `_seen_delivery_ids` grow unboundedly with no eviction~~ -- fixed: M3's `RedisJobQueue` stores the idempotency key with an expiring TTL (`Settings.idempotency_ttl_seconds`, default one week) instead of an ever-growing in-process set; proven by a test that reads the TTL back from Redis directly.

## M3 Build Summary (L1 BUILD complete, L4 VERIFY pending)

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
