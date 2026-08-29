# PLAN — pr-review-agent

The machine-parseable implementation plan. Mirrors the milestone table in `DONE.html` (DONE.html is the
human/visual view; this is the one loops read). Sliced so each milestone ships in one L1 BUILD pass.

> Slicing rule: a milestone must have (a) a single clear outcome, (b) an exact **demo command** that
> proves it, and (c) a freeze boundary of files it may touch. If you can't write the demo command,
> the milestone is too vague — split it.

---

## Brainstorm (G0.5 — fill before slicing milestones)

> Three fundamentally different approaches to the cognitive job. Pick one. Record the rationale.
> This is the cheapest design decision — you haven't written a line of code yet.

### Approach A — Thin Vertical Slice
Build the shortest possible path from a real GitHub webhook to a real posted comment first —
one hardcoded "specialist" (or even a regex check) standing in for all four agents — then widen
the slice into the full fan-out, retrieval, and HITL design once the end-to-end path is proven.
- Strengths: proves the riskiest integration (GitHub App + webhook delivery + posting) on day one; gives a demoable artifact almost immediately.
- Weaknesses: requires GitHub App credentials before milestone 1 is even done, which violates the "M1 needs no external credentials" constraint; early architecture choices get made under real-integration pressure rather than deliberately.

### Approach B — Infrastructure-First
Build the full data spine, module skeleton, reliability layer, and observability plumbing
first — schemas, ADR-002 dependency enforcement, events table, circuit breakers — before any
agent logic exists, on the theory that a system this failure-mode-conscious should have its
skeleton and nervous system correct before it has a brain.
- Strengths: the DoD gates (dependency direction, timeouts, append-only events) are enforceable from milestone 1 onward, not bolted on later; hard-to-retrofit decisions (schema shape, module boundaries) get made early and deliberately.
- Weaknesses: nothing observable or demoable to a non-technical stakeholder for several milestones; risks over-building infrastructure for agent behavior that hasn't been validated yet ("infrastructure astronaut" failure mode).

### Approach C — Local-Simulation-First
Build the entire cognitive pipeline — ingress, queue, orchestrator, four agents, aggregator,
HITL gate, retrieval — against local fixtures and mocks (a hand-signed fake webhook payload, a
Dockerized Redis and Postgres+pgvector, a real LLM call once a key exists, a mocked GitHub
client) so every milestone through the full local dry-run needs zero cloud accounts. Real
GitHub, Tiger Cloud, and deployment are separate, later milestones that swap one adapter at a
time behind the interfaces the spec already designs for (workflow_engine, memory client).
- Strengths: satisfies "M1 needs no external credentials" not just for M1 but for roughly the first two-thirds of the plan; the abstract-interface discipline the spec already commits to (ADR-001's workflow_engine, ADR-003's staged Tiger migration) makes the later swap-in low-risk, not a rewrite.
- Weaknesses: a local Postgres+pgvector container is not Tiger Cloud — DiskANN, hypertables, and continuous aggregates are Tiger-specific and can't be fully simulated, so two migration milestones are unavoidable later; risk of "works locally, breaks on the first real webhook" if the mocked GitHub client drifts from the real API's edge cases.

### Chosen: Approach C — Local-Simulation-First
It is the only approach that satisfies the hard constraint (M1, and several milestones after
it, need no GitHub App / LLM key / Tiger Cloud account) while still building the real
architecture rather than a throwaway prototype — because the spec already designed clean swap
points (the workflow_engine interface, the staged four-phase Tiger integration plan in 4.3),
simulation-first defers cost and credential setup without deferring real design decisions.

---

## Milestones

Credential legend: **[NO CREDENTIALS]** runs entirely locally. **[CREDENTIALS REQUIRED: X]**
needs the orchestrator to warn the user and pause for X before this milestone can start.
Credential-free milestones (M1–M7, M9's container path) are ordered first; milestones needing
a paid/external credential (M8, M10–M13) come after, per the user-approved ordering rule.

### M1 — Project Skeleton & Core Contracts
- **Outcome:** The 19-subpackage layout exists with the inward-only dependency rule mechanically enforced, and the Finding/Review/WebhookEvent Pydantic contracts from the spec's L2 are defined and unit-tested.
- **Phase (spec roadmap):** Phase 01 — System Architecture
- **Files / freeze boundary:** `backend/{core,models,agents,orchestrator,...}/__init__.py` (stub package tree per 4.2's module map), `backend/models/{enums,findings,review,webhook}.py`, `pyproject.toml`, `.importlinter` (or equivalent), `tests/unit/test_models.py`, `docs/adr/ADR-002-modular-monolith.md`
- **Demo command:** `pytest tests/unit/test_models.py -v && lint-imports --config .importlinter`
- **Success criteria:** All model tests pass; `lint-imports` reports zero contract violations; `backend/core/` has zero imports from any sibling package (grep-verifiable).
- **Loops:** L1, L2, L4
- **Skills:** canon + tdd + modular-architecture
- **Token budget:** 50000
- **Credentials:** [NO CREDENTIALS]

### M2 — Webhook Ingress: HMAC + Idempotency
- **Outcome:** A FastAPI endpoint that verifies GitHub's HMAC-SHA256 signature and rejects a replayed `X-GitHub-Delivery` UUID, using a locally-crafted signed payload (no real GitHub App needed to test signature math).
- **Phase:** Phase 03 — Backend & API
- **Files / freeze boundary:** `backend/webhook_receiver/{validator,parser,router}.py`, `backend/api/main.py`, `tests/unit/test_webhook_validator.py`, `tests/fixtures/sample_pr_payload.json`
- **Demo command:** `uvicorn backend.api.main:app --port 8000 & sleep 1 && python scripts/send_signed_webhook.py --secret test-secret --url http://localhost:8000/webhook && pytest tests/unit/test_webhook_validator.py -v`
- **Success criteria:** A correctly-signed request returns 200; a tampered-signature request returns 401; a replayed delivery UUID is acknowledged but not reprocessed (verified by an idempotency-store hit count of 1).
- **Loops:** L1, L3, L4
- **Skills:** canon + tdd + security-engineering
- **Token budget:** 50000
- **Credentials:** [NO CREDENTIALS] (uses a locally-generated HMAC secret, not a real GitHub App)

### M3 — Queue + Worker (Dockerized Redis/ARQ)
- **Outcome:** A validated webhook enqueues a job to Redis via ARQ, and a separate worker process dequeues and logs it, proving the async hand-off the spec's ingress design depends on.
- **Phase:** Phase 04 — Workflow Orchestration (infra half) / Phase 13 — Infrastructure (local dev slice)
- **Files / freeze boundary:** `backend/job_queue/arq_worker.py`, `docker-compose.yml` (redis service), `tests/integration/test_queue_roundtrip.py`
- **Demo command:** `docker compose up -d redis && arq backend.job_queue.arq_worker.WorkerSettings & pytest tests/integration/test_queue_roundtrip.py -v`
- **Success criteria:** A job enqueued by the test appears in the worker's processed log within 5 seconds; `docker compose down` and re-`up` leaves no orphaned jobs (queue is empty after a clean run).
- **Loops:** L1, L4
- **Skills:** canon + tdd + distributed-systems
- **Token budget:** 50000
- **Credentials:** [NO CREDENTIALS] (local Docker only)

### M4 — Orchestrator Fan-Out (Stub Agents)
- **Outcome:** A LangGraph graph fans out to four parallel stub agent nodes (each returning a canned `Finding`) via the Send API, checkpoints state to Redis, and resumes correctly after a simulated mid-run crash.
- **Phase:** Phase 04 — Workflow Orchestration
- **Files / freeze boundary:** `backend/orchestrator/{graph,nodes,state,langgraph_engine}.py`, `backend/core/workflow_engine.py` (the ADR-001 abstract interface), `tests/integration/test_orchestrator_fanout.py`
- **Demo command:** `pytest tests/integration/test_orchestrator_fanout.py -v -k "fanout or checkpoint_resume"`
- **Success criteria:** All four stub nodes' outputs are present in the final state; killing the worker after 2 of 4 nodes complete and restarting resumes from the checkpoint rather than re-running completed nodes (asserted via a call counter).
- **Loops:** L1, L2, L3, L4
- **Skills:** canon + tdd + llmops-ai-agents
- **Token budget:** 50000
- **Credentials:** [NO CREDENTIALS] (stub nodes, no real LLM calls yet)

### M5 — Aggregator + Confidence-Weighted HITL Gate
- **Outcome:** Pure-Python aggregation logic merges four agents' Finding lists, deduplicates same-(file,line) findings by keeping the highest confidence, computes overall confidence, and routes to "post" or "human_approval_queue" per the spec's L7 gate — all testable without any LLM.
- **Phase:** Phase 08 — Multi-Agent Systems (aggregator half) / Phase 19 — Human-in-the-Loop (gate logic)
- **Files / freeze boundary:** `backend/agents/{base_agent,contracts}.py`, `backend/orchestrator/nodes.py` (aggregator node), `backend/hitl/queue.py`, `tests/unit/test_aggregator.py`, `tests/unit/test_hitl_gate.py`
- **Demo command:** `pytest tests/unit/test_aggregator.py tests/unit/test_hitl_gate.py -v`
- **Success criteria:** Given a fixture set of overlapping findings, dedup keeps only the higher-confidence one; a fixture containing one CRITICAL finding always routes to the HITL queue regardless of confidence; a fixture with confidence below the configured threshold (`HITL_CONFIDENCE_THRESHOLD` env var, default 0.75) routes to the HITL queue.
- **Loops:** L1, L2, L4
- **Skills:** canon + tdd + llmops-ai-agents
- **Token budget:** 50000
- **Credentials:** [NO CREDENTIALS]

### M6 — Reliability Layer (Retries, Circuit Breaker, Timeouts)
- **Outcome:** Every outbound call path (a fake flaky HTTP client standing in for GitHub/LLM providers) is wrapped in retry-with-backoff, a circuit breaker that opens after N failures, and a hard timeout — verified under deliberate fault injection.
- **Phase:** Phase 12 — Reliability
- **Files / freeze boundary:** `backend/reliability/{retry,circuit_breaker,idempotency,timeout}.py`, `tests/unit/test_reliability.py` (fault-injection harness using a fake unreliable client)
- **Demo command:** `pytest tests/unit/test_reliability.py -v --tb=short`
- **Success criteria:** A client that fails 100% of the time trips the circuit breaker within N attempts and subsequent calls fail fast (no network attempt) until the cooldown; a call exceeding the configured timeout raises within tolerance (±50ms) rather than hanging.
- **Loops:** L1, L4
- **Skills:** canon + tdd + release-it
- **Token budget:** 50000
- **Credentials:** [NO CREDENTIALS]

### M7 — Events Spine (Local Audit/Trace Log)
- **Outcome:** Every orchestrator span, decision, and (stub) LLM call emits one append-only row to a local events table, and a trace-viewer query reconstructs one review end-to-end from `review_id` alone.
- **Phase:** Phase 10 — Observability
- **Files / freeze boundary:** `backend/observability/{events,tracing,audit,workflow_context}.py`, `backend/database/{postgres,models,repository}.py` (local Postgres/SQLite dev schema mirroring `agent_events`), `tests/integration/test_events_spine.py`
- **Demo command:** `docker compose up -d postgres && python scripts/run_fixture_review.py --review-id demo-1 && psql "$DATABASE_URL" -c "SELECT event_type, agent, ts FROM agent_events WHERE review_id='demo-1' ORDER BY ts"`
- **Success criteria:** The query returns a non-empty, time-ordered sequence covering span.start through decision for the fixture run; no UPDATE/DELETE statement exists anywhere in the codebase against the events table (grep-verifiable, mirrors the append-only-events invariant).
- **Loops:** L1, L2, L4
- **Skills:** canon + tdd + designing-data-intensive-applications
- **Token budget:** 50000
- **Credentials:** [NO CREDENTIALS] (local Postgres via Docker, not Tiger Cloud yet)

### M8 — First Real Specialist Agent (LLM-Backed)
- **Outcome:** The security agent makes a real call to the driver model (claude-haiku-4-5) against a fixture diff, and its raw output is parsed through the `Finding` Pydantic schema before anything downstream sees it — the first point real model behavior enters the system.
- **Phase:** Phase 05 — LLM & Reasoning
- **Files / freeze boundary:** `backend/agents/security_agent.py`, `backend/tools/{llm_client,model_router}.py`, `backend/prompts/{registry,templates/security.md}`, `backend/economics/budget.py` (BudgetGuard stub, daily cap read from `BUDGET_DAILY_CAP_USD` env var, default 20), `tests/integration/test_security_agent_live.py` (marked to skip without a key), `tests/unit/test_security_agent_schema.py` (mocked LLM response)
- **Demo command:** `ANTHROPIC_API_KEY=... python -m backend.agents.security_agent --diff tests/fixtures/sqli_diff.patch`
- **Success criteria:** The command exits 0 and prints at least one `Finding` whose fields all validate against the schema; `pytest tests/unit/test_security_agent_schema.py -v` passes without any API key (mocked path); a forced BudgetGuard-over-cap fixture blocks the call before it reaches the LLM client.
- **Loops:** L1, L3, L4
- **Skills:** canon + tdd + llmops-ai-agents
- **Token budget:** 50000
- **Credentials:** **[CREDENTIALS REQUIRED: LLM API key (claude-haiku-4-5 driver access) for the live demo command; the unit-test path is credential-free]**

### M9 — Hybrid Retrieval / Local Vector Memory
- **Outcome:** A small seeded set of code chunks in a local Postgres+pgvector container is searchable by both ANN vector similarity and full-text search, merged by reciprocal rank fusion into a top-k list — the grounding layer from spec L4/3.5, running locally before any Tiger Cloud account exists. Embeddings use OpenAI text-embedding-3-large at 256 dims, per the spec's pinned config.
- **Phase:** Phase 06 — Memory Architecture
- **Files / freeze boundary:** `backend/memory/{tiger_client,embedder,context_retriever}.py`, `migrations/scripts/dev-pgvector-init.sql` (local analog of `2026-06-tiger-init.sql`), `tests/integration/test_hybrid_retrieval.py`
- **Demo command:** `docker compose up -d pgvector && python scripts/seed_code_chunks.py --repo . && pytest tests/integration/test_hybrid_retrieval.py -v`
- **Success criteria:** A query for a known function name returns it in the top-3 fused results via FTS even when the embedding model ranks it lower; recall@5 on a 10-query fixture set is 100% (every known-relevant chunk is retrieved).
- **Loops:** L1, L3, L4
- **Skills:** canon + tdd + designing-data-intensive-applications
- **Token budget:** 50000
- **Credentials:** [NO CREDENTIALS] for the container path; **[CREDENTIALS REQUIRED: OpenAI API key for text-embedding-3-large]** if the embedder is not run against a cached/fixture embedding set

### M10 — Full Local Dry-Run Review
- **Outcome:** A complete PR diff fixture flows through ingress (simulated), all four real specialist agents (claude-haiku-4-5), retrieval, aggregation, and the HITL gate, producing one structured review JSON on disk — with GitHub posting mocked out — proving the whole cognitive pipeline end-to-end before touching a real repository.
- **Phase:** Phase 08 — Multi-Agent Systems (full integration)
- **Files / freeze boundary:** `backend/agents/{quality_agent,test_agent,docs_agent}.py`, `backend/integrations/github_client.py` (mock-backed interface), `backend/cli/review_local.py`, `tests/e2e/test_full_local_review.py`, `tests/fixtures/sample_pr_diff.patch`
- **Demo command:** `python -m backend.cli.review_local --diff tests/fixtures/sample_pr_diff.patch --out out/review.json && jq '.findings | length' out/review.json`
- **Success criteria:** `out/review.json` validates against the `Review` schema; the mocked GitHub client records exactly one "post" or one "queue_for_hitl" call, never both; `pytest tests/e2e/test_full_local_review.py -v` passes.
- **Loops:** L1, L4
- **Skills:** canon + tdd + llmops-ai-agents
- **Token budget:** 50000
- **Credentials:** **[CREDENTIALS REQUIRED: LLM API key]** (four real agents now run; GitHub itself is still mocked)

### M11 — Real GitHub Integration
- **Outcome:** A real GitHub App receives a webhook from an actual pull request, and the system posts a real structured review comment back to that PR — the mocked `github_client` from M10 is swapped for the real REST wrapper behind the same interface. A contract test pins the mocked client's behavior to the real API's observed shape, to catch mock-drift.
- **Phase:** Phase 03 (real ingress) / Phase 08 (real posting) — closing the loop opened in M2/M10
- **Files / freeze boundary:** `backend/integrations/{github_client,github_models}.py` (real implementation), `backend/security/rbac.py`, `.env.example` (documents required GitHub App vars), `tests/contract/test_github_client_contract.py`
- **Demo command:** `ngrok http 8000 & python scripts/register_webhook.py --url "$NGROK_URL/webhook" && gh pr create --title "test" --body "test" && curl -s http://localhost:8000/health`
- **Success criteria:** Opening a real PR against the configured test repo results in a real comment posted by the bot within 60 seconds, visible via `gh pr view --comments`; a second identical webhook delivery (GitHub's own retry) does not produce a second comment.
- **Loops:** L1, L3, L4
- **Skills:** canon + tdd + security-engineering
- **Token budget:** 50000
- **Credentials:** **[CREDENTIALS REQUIRED: GitHub App registration + webhook secret + installation on a test repo]**

### M12 — Tiger Cloud Migration
- **Outcome:** The local Postgres+pgvector and events tables from M7/M9 are replaced by a provisioned Tiger Cloud instance with the real hypertable, DiskANN index, and continuous aggregates from the spec's 2.3/4.3, per ADR-003's four-stage plan (Infra → Events → Memory → Dashboard, stages A–C here).
- **Phase:** Phase 13 — Infrastructure / Phase 14 — Data Engineering / Phase 06 & 10 (re-verified against the real store)
- **Files / freeze boundary:** `migrations/scripts/2026-06-tiger-init.sql`, `backend/database/postgres.py` (Tiger pool + `init_tiger_schema`), `backend/memory/tiger_client.py` (real pgvectorscale/DiskANN path replaces the local dev version)
- **Demo command:** `psql "$TIGER_DATABASE_URL" -c "SELECT extname FROM pg_extension WHERE extname IN ('timescaledb','vector','vectorscale')" && pytest tests/integration/test_hybrid_retrieval.py tests/integration/test_events_spine.py --tiger-url "$TIGER_DATABASE_URL" -v`
- **Success criteria:** All three extensions are listed; the same M9/M7 test suites pass unmodified against the real Tiger Cloud connection string; `agent_health_1m` and `pr_cost_hourly` continuous aggregates exist and return rows after the fixture run.
- **Loops:** L1, L3, L4
- **Skills:** canon + tdd + designing-data-intensive-applications
- **Token budget:** 50000
- **Credentials:** **[CREDENTIALS REQUIRED: Tiger Cloud account + provisioned instance connection string]**

### M13 — Dashboard + Evaluation/CI Gate
- **Outcome:** The Next.js 15 (Node 20) dashboard renders the HITL queue and per-agent cost/latency from the continuous aggregates, and a golden-dataset LLM-as-judge (claude-sonnet-5, judge only) regression gate runs in CI and blocks a merge that degrades review quality.
- **Phase:** Phase 02 — Frontend / Phase 09 — Evaluation / Phase 18 — CI/CD for AI
- **Files / freeze boundary:** `frontend/src/app/**`, `frontend/components/**`, `backend/evaluation/{golden_dataset,judge,regression_gate}.py`, `.github/workflows/eval-gate.yml`
- **Demo command:** `npm --prefix frontend run build && npm --prefix frontend run dev & pytest tests/eval/test_regression_gate.py -v`
- **Success criteria:** The dashboard's economics page renders non-zero cost figures sourced from `pr_cost_hourly`; the regression gate fails CI when run against a deliberately-degraded fixture judge score, and passes against the baseline.
- **Loops:** L1, L2, L3, L4
- **Skills:** canon + tdd + **design-system skill (MANDATORY for frontend)** + llmops-ai-agents
- **Token budget:** 50000
- **Credentials:** **[CREDENTIALS REQUIRED: LLM API key for the judge (claude-sonnet-5); Railway deployment credentials only if the dashboard is deployed rather than run locally]**

---

## Credential summary for the orchestrator to surface up front

| Needs no credentials at all | M1, M2, M3, M4, M5, M6, M7, M9 (container path) |
|---|---|
| Needs an LLM API key | M8, M10, M13 (judge) |
| Needs an OpenAI embeddings key | M9 (only if not using cached/fixture embeddings) |
| Needs a GitHub App | M11 |
| Needs Tiger Cloud | M12 |
| Needs Railway (optional) | M13 (only if deploying, not for local dev) |

Eight of thirteen milestones — roughly the first two-thirds of the plan — are runnable with
nothing but Docker and Python/Node installed locally, per the chosen local-simulation-first
approach. Credential-free milestones (M1–M7, plus M9's container path) are ordered first;
milestones needing a paid/external credential (M8, M10, M11, M12, M13) are ordered after them.

---

## Progress (loops append here on milestone completion — newest last)

- _(none yet — first loop fills this)_
