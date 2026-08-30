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

**Not built yet:** the queue/worker (M3, Redis + ARQ), the LangGraph
orchestrator (M4), the four specialist agents and aggregator/HITL gate (M5,
M8, M10), retrieval/memory (M9), real GitHub posting (M11), and the dashboard
(M13). None of those exist in this repository today — see
[`.genesis/PLAN.md`](.genesis/PLAN.md) for the full milestone list and demo
commands. Right now this repo can accept and validate a signed webhook and
enqueue it in memory. It does not review anything yet.

## Architecture

Planned end-to-end shape:

```
GitHub webhook --> FastAPI ingress --> queue --> LangGraph orchestrator --> [security | quality | tests | docs] agents --> aggregator --> HITL gate --> GitHub
```

What exists today vs. what's planned:

| Component | Status | Notes |
|---|---|---|
| FastAPI webhook ingress (`backend/webhook_receiver/`, `backend/api/`) | **Built (M2)** | HMAC-SHA256 verification, idempotency, in-memory job queue behind an interface |
| Queue (Redis / ARQ) | Planned (M3) | Ingress already codes against a `JobQueue` interface (`backend/job_queue/interface.py`) so the in-memory stand-in can be swapped without touching the router |
| Orchestrator (LangGraph) | Planned (M4) | Fan-out to four agent nodes with crash-resumable checkpointing |
| Specialist agents (security, quality, tests, docs) | Planned (M5, M8, M10) | LLM-backed reasoners, each validated against the `Finding` schema |
| Retrieval / memory (TimescaleDB + pgvector, later Tiger Cloud) | Planned (M9, M12) | Hybrid vector + full-text search over codebase chunks |
| Aggregator + confidence-weighted HITL gate | Planned (M5) | Dedupes findings, routes low-confidence/critical reviews to a human queue |
| Dashboard (Next.js) | Planned (M13) | Renders the HITL queue and per-agent cost/latency |

Domain contracts (`Finding`, `Review`, `WebhookEvent`) already live in
`backend/models/` and are shared by every layer above them; `backend/core/`
holds cross-cutting base abstractions (currently just `Settings`) that
nothing else may depend outward from, per ADR-002's inward-only dependency
rule (mechanically enforced by `import-linter`, see `.importlinter`).

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
`"status": "accepted"`. Run it a second time with the same delivery ID and
you'll get `"status": "duplicate"` instead — that's the idempotency check.

You can also run the webhook test suite directly, without starting a server:

```bash
pytest tests/unit/test_webhook_validator.py -v
```

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
