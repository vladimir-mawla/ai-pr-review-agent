# pr-review-agent

An AI pull-request review agent. Four specialist LLM reasoners — security,
quality, tests, and docs — fan out over a PR diff, grounded in retrieved
codebase context, merged by an aggregator, and gated by a confidence-weighted
human-in-the-loop check before anything is posted back to GitHub.

## Status: EARLY

This is under active, milestone-by-milestone construction using a
[genesis-kit](.genesis/) loop (plan, build, verify, repeat). It is **not**
close to reviewing a real pull request yet.

**Done:**

- **M1 — Project Skeleton & Core Contracts.** The package layout exists with
  an inward-only dependency rule enforced by `import-linter`, and the
  `Finding` / `Review` / `WebhookEvent` Pydantic contracts are defined and
  unit-tested.
- **M2 — Webhook Ingress: HMAC + Idempotency.** A FastAPI endpoint verifies
  GitHub's HMAC-SHA256 webhook signature over the raw request body and
  de-duplicates retried deliveries by `X-GitHub-Delivery` before anything is
  enqueued.
- **M3 — Queue + Worker (Dockerized Redis/ARQ).** A validated webhook enqueues
  a job to Redis via ARQ (`backend/job_queue/redis_arq.py`), and a separate
  worker process dequeues and logs it — behind the same `JobQueue` interface
  M2 already coded against, so the router needed zero changes.
- **M4 — Orchestrator Fan-Out (Stub Agents).** A LangGraph `StateGraph`
  (`backend/orchestrator/`) fans out to four parallel stub specialist nodes
  via the Send API and fans back in through a join node, checkpointed with a
  file-backed SQLite saver so a crashed run resumes from where it left off
  instead of re-running completed nodes. See the note under Architecture
  below: this orchestrator exists and is tested in isolation, but nothing
  yet calls it from the webhook/queue path.

**Not built yet:** the aggregator and confidence-weighted HITL gate (M5), the
reliability layer (M6), the events/audit spine (M7), the four *real*
LLM-backed specialist agents (M8, M10), hybrid retrieval/memory (M9), real
GitHub posting (M11), the Tiger Cloud migration (M12), and the dashboard
(M13). None of those exist in this repository today — see
[`.genesis/PLAN.md`](.genesis/PLAN.md) for the full milestone list and demo
commands. Right now this repo can accept and validate a signed webhook, queue
it in Redis, and — as a separate, not-yet-wired-in capability — run a
LangGraph graph that fans a job out to four canned stub agents and merges
their (fake) findings back. It does not review anything yet, and enqueuing a
webhook does not currently trigger the orchestrator.

## Architecture

Planned end-to-end shape:

```
GitHub webhook --> FastAPI ingress --> queue --> LangGraph orchestrator --> [security | quality | tests | docs] agents --> aggregator --> HITL gate --> GitHub
```

**This is the target shape, not the current wiring.** Ingress, the queue,
and the orchestrator each exist and are independently tested today, but the
arrows into and out of the orchestrator in that diagram are not implemented
yet: nothing in `backend/webhook_receiver/` or `backend/job_queue/` calls
`LangGraphWorkflowEngine`, and the orchestrator's four specialists are canned
stubs, not real agents. Treat every component below as real in isolation,
and the connections between them as still planned unless marked otherwise.

What exists today vs. what's planned:

| Component | Status | Notes |
|---|---|---|
| FastAPI webhook ingress (`backend/webhook_receiver/`, `backend/api/`) | **Built (M2)** | HMAC-SHA256 verification, idempotency, job queue behind an interface |
| Queue (Redis / ARQ) | **Built (M3)** | `RedisJobQueue` (`backend/job_queue/redis_arq.py`) behind the same `JobQueue` interface the router codes against, plus an ARQ worker (`backend/job_queue/arq_worker.py`) whose job handler is still a stub |
| Orchestrator (LangGraph) | **Built (M4), not wired in** | `backend/orchestrator/` fans out to four stub agent nodes via the Send API with file-backed SQLite checkpointing; crash-resumable and covered by integration tests, but nothing in the webhook/queue path invokes it yet — that wiring is a later milestone |
| Specialist agents (security, quality, tests, docs) | Planned (M5, M8, M10) | M4's four nodes are canned stubs (no LLM call); real LLM-backed reasoners, each validated against the `Finding` schema, land in M8/M10 |
| Retrieval / memory (TimescaleDB + pgvector, later Tiger Cloud) | Planned (M9, M12) | Hybrid vector + full-text search over codebase chunks |
| Aggregator + confidence-weighted HITL gate | Planned (M5) | M4's join node is an intentional no-op; dedup, confidence scoring, and HITL routing land in M5 |
| Dashboard (Next.js) | Planned (M13) | Renders the HITL queue and per-agent cost/latency |

Domain contracts (`Finding`, `Review`, `WebhookEvent`) already live in
`backend/models/` and are shared by every layer above them; `backend/core/`
holds cross-cutting base abstractions (`Settings`, and now the ADR-001
`WorkflowEngine` Protocol that `LangGraphWorkflowEngine` structurally
satisfies) that nothing else may depend outward from, per ADR-002's
inward-only dependency rule (mechanically enforced by `import-linter`, see
`.importlinter`).

## Setup

Requires Python 3.12.

Clone the repository:

```bash
git clone https://github.com/vladimir-mawla/ai-pr-review-agent.git
```

```bash
cd ai-pr-review-agent
```

Create a virtual environment:

```bash
python3.12 -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

Install the package with its dev dependencies:

```bash
pip install -e ".[dev]"
```

Copy the environment template:

```bash
cp .env.example .env
```

Then edit `.env` and set `GITHUB_WEBHOOK_SECRET` to any non-empty string for
local development (it just has to match whatever secret you sign test
payloads with — see the demo below). There is no default value on purpose:
`backend/core/settings.py` fails fast at startup if it's missing or blank,
rather than silently accepting a well-known secret.

## Local development notes

- **`docker compose down && docker compose up` wipes Redis, including the
  idempotency store.** The `redis` service in `docker-compose.yml` has no
  volume, so a recreate always starts it empty. That's fine for M3's queue
  (no orphaned jobs survive a restart, which is the point), but it also
  means replay protection resets: a `X-GitHub-Delivery` id this process
  already saw before the recreate will be treated as new and re-enqueued
  after it. A volume would fix this but isn't configured today.
- **The 9 queue integration tests need a real Redis and skip cleanly without
  one.** `tests/integration/test_queue_roundtrip.py` checks for a reachable
  Redis at collection time and skips (not fails) if it can't connect. Run
  `docker compose up -d redis` first if you want them to actually execute;
  otherwise `pytest -v` still passes, just with those 9 reported as
  `skipped` rather than `passed` — check the summary line, since a skip is
  not the same thing as a pass.
- **The M4 orchestrator has no CLI demo yet.** Unlike M2/M3, there is no
  script that runs a LangGraph review end-to-end from the command line — its
  behavior (parallel fan-out, crash/resume) is proven entirely by
  `tests/integration/test_orchestrator_fanout.py`, which needs no Docker and
  no external services (its checkpoint database is a plain local SQLite
  file).
- **The `pgvector` service (M9) has no volume, by design, and its corpus
  vanishes on every container recreate.** `docker compose down && up` (or
  any event that recreates the container — a host reboot, a Docker prune,
  simply never having started it before) leaves `code_chunks` at zero rows,
  even though `var/retrieval_seed_marker.json` (gitignored local state) may
  still claim a corpus is already seeded — the marker file is host-side and
  outlives the container, so a stale marker plus an empty table is a real,
  observed trap, not a hypothetical one: it has cost two separate
  verification sessions a surprise re-seed and real API spend already. The
  test fixtures handle this correctly on their own (they compare the
  marker's claimed row count against a live `SELECT count(*)` before
  trusting it, and re-seed if they disagree), so a `pytest -v` run is always
  safe — but a human running `docker compose up -d pgvector` by hand and
  trusting the marker file is not protected by that check. If you've
  recreated the container, re-seed explicitly before relying on retrieval
  results: `python scripts/seed_code_chunks.py --repo .`. Re-seeding with
  the real OpenAI backend (`EMBEDDER_BACKEND=openai`) costs roughly **$0.02**
  per full run (~150k tokens of `text-embedding-3-large` at $0.13/M,
  measured directly — see `checkpoints/CURRENT.md`'s M9 history); the
  fixture backend (`DeterministicFixtureEmbedder`, the default) costs
  nothing.
- **In `zsh`, `env $SOME_VAR command` silently drops everything after the
  first variable — it does NOT word-split like bash.** This bit real, live
  debugging: reconstructing several `LANGSMITH_*` vars from `.env` and
  handing them to `env` as one unquoted expansion,
  ```bash
  LS_ENV=$(grep '^LANGSMITH_' .env | tr '\n' ' '); env $LS_ENV python -m backend.observability.tracing
  ```
  looks like it exports five variables, but in `zsh` an unquoted `$VAR`
  expansion is a single word, not five — `env` sees ONE argument shaped
  like `LANGSMITH_API_KEY=... LANGSMITH_ENDPOINT=... ...` and only sets the
  first `NAME=value` pair (`LANGSMITH_API_KEY`), silently dropping
  `LANGSMITH_ENDPOINT` (and everything else). The child process then falls
  back to the LangSmith SDK's default endpoint instead of this org's AWS
  host, which 403s identically to a bad key — and because LangSmith's
  ingestion swallows that failure by design (see
  `backend/observability/tracing.py`'s module docstring), nothing at the
  call site notices. Confirmed directly:
  `zsh -c 'LS_ENV=$(grep "^LANGSMITH_" .env | tr "\n" " "); env $LS_ENV .venv/bin/python -c "import os; print([k for k in os.environ if k.startswith(\"LANGSMITH\")])"'`
  prints `['LANGSMITH_API_KEY']` — one variable, not five. (`bash` word-splits
  the same unquoted expansion and would set all five; this is a `zsh`-specific
  footgun, and this project's documented shell is `zsh`.) Prefer not
  reconstructing env vars into a string at all: pydantic-settings
  (`backend/core/settings.py`) already reads `.env` directly, with no shell
  export needed — see the verified command below.
- **`set -a && source .env && set +a` exports the Tiger Cloud `PG*` vars
  globally and can break a LOCAL Postgres/psql connection.** `.env` has a
  non-empty `PGUSER=tsdbadmin` (Tiger Cloud's admin user, for the M12
  `TIGER_DATABASE_URL`/native-`PG*` connection path — see `.env.example`'s
  M12 block) alongside the M7 events Postgres and M9 pgvector, both on
  different ports (5433/5434) with their own credentials baked into
  `DATABASE_URL`/`PGVECTOR_URL`. Exporting the whole file into the shell
  (rather than letting pydantic-settings read it directly) puts `PGUSER`
  into the environment, and any *bare* `psql`/libpq call made afterward
  with no explicit user in its connection string picks that up instead of
  the local role it should be using. This project's own M7 demo command
  already works around exactly this by using an explicit DSN
  (`psql "$DATABASE_URL"`, not a bare `psql`) every time `.env` is
  sourced — keep doing that if you ever `source .env`, or better, don't
  source it at all (see below).
- **The verified, working way to run `review_local` with LangSmith tracing
  on: don't export anything — just run it.** `Settings` (pydantic-settings)
  reads `.env` on its own; no `source`/`env`/`set -a` dance is needed or
  recommended for `review_local` itself. Confirmed working in this exact
  shell, in both directions:
  ```bash
  # Real config from .env (tracing verified, prints the LangSmith project URL, makes 4 real Anthropic calls):
  .venv/bin/python -m backend.cli.review_local --diff tests/fixtures/sample_pr_diff.patch --out out/review.json --verify-tracing
  ```
  To reproduce the wrong-endpoint failure on purpose (e.g. to prove
  `--verify-tracing` actually catches it) without touching `.env`, override
  exactly one variable with a plain, single `NAME=value` prefix — never a
  reconstructed multi-var string — which is safe in both `zsh` and `bash`:
  ```bash
  LANGSMITH_ENDPOINT=https://api.smith.langchain.com .venv/bin/python -m backend.cli.review_local --diff tests/fixtures/sample_pr_diff.patch --out out/review.json --verify-tracing
  ```
  This fails fast (before any LLM call — see `backend/cli/review_local.py`'s
  module docstring) with a clear `LANGSMITH_ENDPOINT is currently
  'https://api.smith.langchain.com'` diagnosis and exit code 1, rather than
  a silently-empty LangSmith project.

## Running the checks

All four are expected to pass on `main`:

```bash
ruff check .
```

```bash
mypy --strict backend/
```

```bash
pytest -v
```

```bash
lint-imports --config .importlinter
```

(If you didn't activate the venv, prefix each with `.venv/bin/`, e.g.
`.venv/bin/pytest -v`.)

## Running the webhook demo

This proves the M2 slice end-to-end: a correctly-signed `pull_request`
payload is accepted and enqueued; a tampered one is rejected.

Start the server (needs `.env` set up as above):

```bash
uvicorn backend.api.main:app --port 8000
```

In a second terminal, with the same venv activated, sign and send the sample
fixture payload (the secret here must match `GITHUB_WEBHOOK_SECRET` in your
`.env`):

```bash
python scripts/send_signed_webhook.py --secret change-me-to-a-real-shared-secret --url http://localhost:8000/webhook
```

A successful run prints `POST ... -> 200` and a JSON body with
`"status": "accepted"`. By default the script generates a fresh random
delivery ID every time it runs, so running the exact command above twice
produces two `"accepted"` responses, not a duplicate. To see the idempotency
check itself, pass the same `--delivery-id` (a well-formed UUID) both times:

```bash
python scripts/send_signed_webhook.py --secret change-me-to-a-real-shared-secret --url http://localhost:8000/webhook --delivery-id 11111111-1111-1111-1111-111111111111
```

Run that exact line again and you'll get `"status": "duplicate"` instead —
that's the idempotency check.

You can also run the webhook test suite directly, without starting a server:

```bash
pytest tests/unit/test_webhook_validator.py -v
```

## Running the orchestrator tests

This proves the M4 slice: fan-out to four stub agents is genuinely parallel
(not a sequential chain), their findings merge correctly, and a simulated
worker crash mid-run resumes from a checkpoint instead of re-doing completed
work. No Docker, API key, or running server is needed — every test uses a
temporary, per-test SQLite checkpoint file:

```bash
pytest tests/integration/test_orchestrator_fanout.py -v -k "fanout or checkpoint_resume"
```

This is the orchestrator's own test suite, not a webhook-to-review demo —
nothing currently connects it to the `/webhook` endpoint above.

## Development process

This project is built loop-by-loop following a genesis-kit-style
plan/build/verify methodology: an implementation plan lives
in [`.genesis/PLAN.md`](.genesis/PLAN.md) (mirrored, for humans, in
[`.genesis/DONE.html`](.genesis/DONE.html)), and the binary definition of
done for every milestone — dependency direction, schema validation, security
review, a passing `mypy --strict` + `pytest`, and an independent verify pass
— is in [`.genesis/DONE.html`](.genesis/DONE.html). Rolling session state and
what's actually live right now are tracked in
[`.genesis/checkpoints/CURRENT.md`](.genesis/checkpoints/CURRENT.md) and
[`.genesis/implementation-notes.html`](.genesis/implementation-notes.html).

## License

MIT — see [LICENSE](LICENSE).
