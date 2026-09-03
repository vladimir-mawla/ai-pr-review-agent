# pr-review-agent

Four specialist LLM reviewers — security, quality, tests, and docs — read a
GitHub pull-request diff in parallel, each grounded in codebase context
retrieved from a hybrid vector/full-text index. Their findings are merged
and deduplicated, and a confidence-weighted gate decides whether the
resulting review posts itself to GitHub or goes to a human queue instead.

## Status: all 13 planned milestones built and independently verified

This was built loop-by-loop under a [genesis-kit](.genesis/) plan/build/verify
process. `.genesis/PLAN.md`'s milestone list (M1–M13) and
[`.genesis/DONE.html`](.genesis/DONE.html) are both complete: every milestone
has a status pill of `done`, each with at least one independent L4 VERIFY
APPROVE recorded in `.genesis/PLAN.md`'s Progress log. No M14 exists; nothing
in the original plan is left to build. Remaining work lives in a Deferred
list (`.genesis/checkpoints/CURRENT.md`), worked ad hoc, not as new
milestones.

**What is genuinely true, end to end:**

- The full chain — webhook ingress → queue → LangGraph orchestrator → four
  real, LLM-backed specialists grounded by retrieval → aggregator → HITL
  gate → GitHub client — exists, is wired together, and has been run for
  real. `backend/cli/review_local.py` sends a real fixture diff through all
  four real Anthropic calls, an aggregator, and the confidence gate, and
  writes a schema-valid `Review` to disk (see **Try it** below for the
  exact command and its real, measured cost).
- The queue → orchestrator wiring is exercised end to end by
  `tests/integration/test_queue_to_orchestrator.py`: an enqueued webhook
  job is picked up by the real ARQ worker and driven all the way into a
  completed orchestrator run, with no manual step in between.
- A real review was posted to a real GitHub pull request
  (`vladimir-mawla/pr-review-agent-testbed#1`) through the real GitHub App
  integration: 2 findings, both mapped inline via diff-position anchoring,
  0 degraded to the summary. Re-running the same `review_id` against the
  same PR was confirmed not to create a duplicate (the hidden idempotency
  marker check works).
- The confidence gate has been proven correct on a real run, not just in
  unit tests: the same testbed PR contains a genuine SQL-injection defect,
  and the real four-agent pipeline correctly found it and correctly routed
  the review to `QUEUED_FOR_HITL` — a CRITICAL finding is never auto-posted,
  by design.

- **A real inbound GitHub webhook has triggered a review, unattended.**
  Opening `vladimir-mawla/pr-review-agent-testbed#2` produced a genuine
  `pull_request` delivery from GitHub's servers over a `cloudflared` tunnel.
  The ingress verified the signature and returned 200, the job went through
  Redis, and the ARQ worker drove the full four-agent graph — 9.9 seconds end
  to end, with no manual step anywhere in the chain.
- **That review then auto-posted after genuinely clearing the confidence
  gate.** 6 findings at overall confidence 0.933 with no CRITICAL present;
  all 6 anchored inline, 0 degraded to the summary. The posted review body
  carries `review_id=webhook-65691f50-…`, and the `webhook-` prefix is
  generated only by `arq_worker` from a delivery id — so that review provably
  came from the webhook path, not a local CLI run.

`backend/job_queue/arq_worker.py` and `backend/cli/review_local.py` are the
only two production call sites, and both call `post_or_queue` exclusively,
never the direct-post method (grep-verified) — so the gate cannot be bypassed
outside a deliberate one-off demo, which is how the earlier PR #1 post was
made before the tunnel existed.

Note that `.genesis/PLAN.md` names `ngrok` as the tunnel tool and refers to a
`scripts/register_webhook.py` that was never written; the live run used
`cloudflared` and the GitHub App API directly. The plan's wording is stale,
not the capability.

**This is still not deployable, and isn't trying to be:** it runs on one
developer's machine, against one throwaway test repository
(`vladimir-mawla/pr-review-agent-testbed`), behind a `cloudflared` quick
tunnel whose URL changes on every restart — so the ingress is only reachable
while someone is sitting there running it. There is no hosting, no
authentication on the dashboard, and no branch-protection rule that lets any
CI gate actually block a merge (see **Limitations**). The gap between this and
something a team could adopt is deployment, not capability.

## Architecture

```
GitHub webhook (HMAC-signed)
  -> FastAPI ingress (backend/webhook_receiver/, backend/api/)
  -> Redis / ARQ queue (backend/job_queue/)
  -> ARQ worker
  -> LangGraph orchestrator (backend/orchestrator/) -- Send-API fan-out, SQLite checkpointing
       -> security / quality / tests / docs specialists (backend/agents/)
            each grounded by HybridRetriever (backend/memory/) -- pgvector + full-text, RRF fusion
       -> aggregator (severity-first dedupe, backend/agents/base_agent.py + orchestrator/nodes.py)
       -> confidence-weighted HITL gate (route_review)
  -> GitHubClient: post_review_comment (inline diff-position mapping) or queue_for_hitl
```

Every arrow above is a real, tested call, not merely a diagram — see
**Status** above for exactly which links have been exercised via a genuine
external trigger versus a direct or mocked call.

| Layer | What it is | Runs |
|---|---|---|
| Webhook ingress | FastAPI route verifying GitHub's HMAC-SHA256 signature (constant-time) and de-duplicating by `X-GitHub-Delivery` before anything is enqueued | Local only |
| Queue | Redis + ARQ, atomic `SET NX EX` idempotency keys, retry → circuit-breaker → timeout composition around every real Redis call | Local Docker Redis, port 6380 |
| Orchestrator | LangGraph `StateGraph`, fans out to four specialists via the Send API (genuinely concurrent, not sequential), file-backed SQLite checkpointing that resumes a crashed run without re-executing finished nodes | Local process |
| Specialists | Four Claude-Haiku-backed reasoners (`SecurityAgent`, `QualityAgent`, `TestsAgent`, `DocsAgent`), one versioned prompt each, a drift-tolerant JSON parser, forced-HITL fallback on any parse or infrastructure failure | Real Anthropic API calls |
| Retrieval | `HybridRetriever`: pgvector ANN search + Postgres full-text search over AST-chunked source, merged by Reciprocal Rank Fusion (k=60); `OpenAIEmbedder` (real `text-embedding-3-large`) or a free deterministic fixture embedder | Local pgvector, port 5434 (or Tiger Cloud, see below) |
| Aggregator + HITL gate | Severity-first dedupe on `(file_path, line_start)` — a CRITICAL finding can never lose a collision to a lower-severity one — then `route_review`: auto-post iff `overall_confidence >= threshold` **and** no CRITICAL finding | Pure Python, in-process |
| GitHub posting | Real GitHub App auth (RS256 JWT, cached installation tokens), inline comments anchored by `line`+`side`, an unmappable finding degrades into the summary instead of 422ing the whole review, a hidden HTML-comment marker prevents double-posting | Real GitHub REST API |
| Events spine | Append-only `agent_events` table, enforced by database triggers (not convention) against UPDATE/DELETE/TRUNCATE, even for a superuser | Local Postgres, port 5433, **or** a real TimescaleDB hypertable on Tiger Cloud |
| Continuous aggregates | `agent_health_1m` (per-minute latency/cost), `pr_cost_hourly` | **Tiger Cloud only** — locally, the same numbers come from a plain SQL query over `agent_events`, structured so the swap is a change to one method's SQL |
| Dashboard | Next.js 15 (`frontend/`), three client-fetched views: HITL queue, cost/latency, review trace reconstruction, reading a JSON API (`backend/api/dashboard.py`) | Local `next dev`, no auth |
| Tracing | LangSmith, opt-in (`LANGSMITH_TRACING=true`), attaches review/PR/model metadata to each orchestrator run; actively verifies traces land (a probe run, not just checking for an SDK error) | Real LangSmith API, when enabled |

Domain contracts (`Finding`, `Review`, `WebhookEvent`) live in
`backend/models/` and are shared by every layer above; `backend/core/` holds
the cross-cutting `Settings` and the `WorkflowEngine` Protocol that
`LangGraphWorkflowEngine` satisfies structurally. Dependency direction is
inward-only and mechanically enforced by `import-linter` (`.importlinter`) —
see `.genesis/decisions/0001-local-simulation-first.md` for the ADR
recording why this project builds credentialed pieces last.

**What runs locally vs. on the managed instance:** `EVENTS_BACKEND` and
`MEMORY_BACKEND` (`local` by default) select plain local Postgres/pgvector
or the real Tiger Cloud TimescaleDB instance for events and retrieval,
respectively. `reviews` (the HITL-queue table) was deliberately never
migrated to Tiger and always stays local, regardless of that setting. Tiger
Cloud's `code_chunks` table is currently empty — there's no Tiger-side
seeding script yet, only the local `scripts/seed_code_chunks.py`.

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
payloads with — see **Try it** below). There is no default value on
purpose: `backend/core/settings.py` fails fast at startup if it's missing or
blank, rather than silently accepting a well-known secret.

## Local development notes

- **`docker compose down && docker compose up` wipes Redis, including the
  idempotency store.** The `redis` service in `docker-compose.yml` has no
  volume, so a recreate always starts it empty. That's fine for the queue
  (no orphaned jobs survive a restart, which is the point), but it also
  means replay protection resets: an `X-GitHub-Delivery` id this process
  already saw before the recreate will be treated as new and re-enqueued
  after it. A volume would fix this but isn't configured today.
- **The `pgvector` service has no volume, by design, and its corpus vanishes
  on every container recreate.** `docker compose down && up` (or any event
  that recreates the container — a host reboot, a Docker prune, simply
  never having started it before) leaves `code_chunks` at zero rows, even
  though `var/retrieval_seed_marker.json` (gitignored local state) may still
  claim a corpus is already seeded — the marker file is host-side and
  outlives the container, so a stale marker plus an empty table is a real,
  observed trap, not a hypothetical one: it has cost multiple verification
  sessions a surprise re-seed and real API spend already, most recently on
  2026-09-03. The test fixtures handle this correctly on their own (they
  compare the marker's claimed row count against a live `SELECT count(*)`
  before trusting it, and re-seed if they disagree), so a `pytest -v` run
  is always safe — but a human running `docker compose up -d pgvector` by
  hand and trusting the marker file is not protected by that check. If
  you've recreated the container, re-seed explicitly before relying on
  retrieval results: `python scripts/seed_code_chunks.py --repo .`.
  Re-seeding with the real OpenAI backend (`EMBEDDER_BACKEND=openai`) costs
  roughly **$0.02** per full run (~150k tokens of `text-embedding-3-large`
  at $0.13/M, measured directly); the fixture backend
  (`DeterministicFixtureEmbedder`, the default) costs nothing.
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
  M12 block) alongside the events Postgres and pgvector, both on different
  ports (5433/5434) with their own credentials baked into
  `DATABASE_URL`/`PGVECTOR_URL`. Exporting the whole file into the shell
  (rather than letting pydantic-settings read it directly) puts `PGUSER`
  into the environment, and any *bare* `psql`/libpq call made afterward
  with no explicit user in its connection string picks that up instead of
  the local role it should be using. Use an explicit DSN
  (`psql "$DATABASE_URL"`, not a bare `psql`) every time `.env` is
  sourced — or better, don't source it at all (see below).
- **LangSmith's AWS-deployment orgs need `LANGSMITH_WORKSPACE_ID` set, or
  every call 403s with a bare `Forbidden` that looks identical to a bad
  key.** This org's LangSmith account is on the AWS deployment
  (`https://aws.api.smith.langchain.com`, not the SDK's default
  `https://api.smith.langchain.com`), and a service-account key 403s on
  every endpoint there — create a run, read a run, everything — unless
  `LANGSMITH_WORKSPACE_ID` is also set, even though the key itself is
  completely valid. LangSmith's own quickstart doesn't mention this
  variable, so following it verbatim silently fails. `GET
  /api/v1/api-key/current` is *not* a usable health check for a service
  key either — it returns a different, unrelated 401 even with a fully
  correct key and workspace id. `backend.observability.tracing.
  assert_tracing_healthy` verifies tracing is actually working with a real
  probe run (write, flush, read back by id) instead of trusting the SDK's
  own error reporting, because a misconfigured deployment produces zero
  traces **and** zero errors. See `.env.example`'s LangSmith block for the
  full account.
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

## Try it

Four things are actually runnable here, in increasing order of cost. Run
each from an activated venv, or prefix commands with `.venv/bin/`.

### Free: the test suite (no credentials, no network calls)

```bash
pytest -v
```

Verified today: **422 passed, 31 deselected**, exit 0, with no
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` set and Docker's Redis/Postgres/pgvector
up. The 31 deselected tests are everything marked `@pytest.mark.live` — real,
billable Anthropic/OpenAI/GitHub calls, opted out of by `pyproject.toml`'s
`addopts = "-ra -m 'not live'"`. Run them for real with `pytest -m live -v` —
that costs money and is out of scope for this document (see
`pyproject.toml`'s `live` marker docstring for the exact mechanism).

### ~$0.02–0.03: the local four-agent review CLI (needs `ANTHROPIC_API_KEY`)

```bash
.venv/bin/python -m backend.cli.review_local --diff tests/fixtures/sample_pr_diff.patch --out out/review.json
```

This is the real pipeline — four real Anthropic calls, one per specialist,
grounded by retrieval, aggregated, and gated — writing one schema-valid
`Review` to `out/review.json`. Real measured costs from past runs of this
exact command: $0.026943 for 14 findings, $0.021348 for a later run that
also verified tracing. GitHub posting is mocked
(`GITHUB_CLIENT_BACKEND` defaults to `mock`), so nothing is sent anywhere —
this proves the pipeline, not the GitHub integration.

### Free: the dashboard (no credentials, reads whatever's in your local DB)

```bash
npm --prefix frontend run build
```

Verified today: builds and lints clean (`npm run lint` exits 0). Run
`npm --prefix frontend run dev` and open `http://localhost:3000` for three
views — the HITL queue, per-agent cost/latency, and review trace
reconstruction — fetched client-side from `backend/api/dashboard.py`. There
is no login: anyone who can reach the port sees everything.

### Real infrastructure required: the live webhook path

This is the one link never yet exercised (see **Status**). To actually try
it, you need two environment overrides beyond `.env.example`'s defaults,
plus a tunnel:

```bash
JOB_QUEUE_BACKEND=redis
```

```bash
GITHUB_CLIENT_BACKEND=real
```

`JOB_QUEUE_BACKEND` defaults to `in_memory` and `GITHUB_CLIENT_BACKEND`
defaults to `mock` — both deliberately safe defaults so tests and a keyless
checkout never touch real infrastructure. Forget either one and the webhook
demo below still returns `200`/`"accepted"` and looks like it worked — it
just quietly queues in an in-process list that vanishes on restart, or would
post nowhere real, instead of failing loudly. There is no warning printed
either way; the silent no-op is the actual, current behavior, not a bug
being flagged for a future fix.

With both set, `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY_PATH` configured, and
a tunnel (e.g. `ngrok http 8000`, not installed on any machine this project
has been built on so far) registered as the app's webhook URL in GitHub's
App settings, a real `pull_request` event would flow ingress → queue →
orchestrator → GitHub, unattended, for the first time. Short of that, you
can prove every piece except the actual inbound delivery:

```bash
uvicorn backend.api.main:app --port 8000
```

```bash
python scripts/send_signed_webhook.py --secret change-me-to-a-real-shared-secret --url http://localhost:8000/webhook
```

A successful run prints `POST ... -> 200` and a JSON body with
`"status": "accepted"`. Run it again with the same `--delivery-id` (a
well-formed UUID) and you get `"status": "duplicate"` instead — that's the
idempotency check.

## How it was built

13 milestones (M1–M13), each with an executable demo command recorded in
`.genesis/DONE.html` and `.genesis/PLAN.md`, built and verified in a
plan → build → verify loop. Every milestone was independently verified by a
separate model session with no memory of building it — the same discipline
this README is being held to right now.

**Four milestones were formally REJECTed by an independent L4 VERIFY
session at least once**, each fixed in a follow-up L2 DEBUG loop and
re-APPROVEd by a second independent session:

- **M5** (Aggregator + HITL gate): the original dedupe tie-break compared
  confidence only, so a lower-confidence CRITICAL finding could lose to a
  higher-confidence, lower-severity one at the same collision key — a real
  safety bug, not a nitpick. Fixed by making dedupe severity-first.
- **M7** (Events spine): a synchronous events write on uvicorn's
  event-loop thread serialized every concurrent webhook request behind a
  locked table (~4.4s each, measured). Fixed by offloading the write onto
  a thread.
- **M9** (Hybrid retrieval): the named recall fixture from the plan had
  never actually been built, and the demo's own test truncated the corpus
  it had just seeded. Fixed by building the fixture for real and reporting
  the honest number — 4/10 (40%) on the free fixture embedder, 7/10 (70%)
  once measured against real OpenAI embeddings — rather than the literal
  100% the plan had asked for.
- **M13** (Dashboard + eval gate): `/costs` summed ~$40,261 of 2030-dated
  fixture rows into "real" spend with no filter, alongside a false "nothing
  here is fabricated" claim; and the eval-gate CI workflow never triggered
  on a pull request, so it could never block a merge. Both fixed.

**Two further defects were caught downstream of their own milestone's
original APPROVE**, in a later L2 DEBUG pass — genuine defects the passing
test suite had missed, distinct from the four formal REJECTs above:

- **M8**: an invalid Anthropic API key crashed the orchestrator outright,
  while a *missing* key correctly triggered the safe fallback — exposed
  only once a real credential was rotated in.
- **M6/M7**: the same event-loop-blocking defect class M7 had just fixed
  on the events-write path turned out to also apply to the queue's Redis
  enqueue call, missed by M6's own verification because it tested the
  retry/breaker/timeout primitives without asking whether their caller
  blocked the loop.

That is six defects the plan's own Progress log (`.genesis/PLAN.md`,
"Milestones an independent L4 VERIFY session REJECTED at least once")
explicitly tallies by name — four formal L4 REJECTs and two more found in
later debug passes. Several additional non-blocking findings were raised
and closed at individual milestones (see
`.genesis/checkpoints/CURRENT.md`'s Deferred/Resolved sections for the
full, dated account), but they are not folded into this count.

Several success criteria were **amended** after the fact — always with the
original text preserved above the correction, never silently rewritten.
Notable ones: M9's literal "100% recall@5" bar was retired in favor of a
statistically-honest fusion-vs-vector-alone comparison; M11's literal "post
within 60 seconds" bar was retired because the test PR's real CRITICAL
finding correctly forced a human-review route instead, and the operative
bar became proving the posting mechanism itself works.

## Limitations

Drawn from `.genesis/checkpoints/CURRENT.md`'s Deferred list — not
invented, and not exhaustive (see that file for the complete, dated
account). Highlights:

- **Not deployed anywhere.** Runs on one machine, against one throwaway
  test repository. No tunnel is currently set up, no hosting exists, and
  (see **Status**) no real inbound webhook has ever actually been
  delivered.
- **`main` has no branch protection.** `gh api .../branches/main/protection`
  returns 404, so the eval gate (`eval-gate.yml`) cannot actually block a
  merge even though it now runs on every pull request — that requires a
  repo-admin setting this project hasn't configured.
- **The eval gate live-scores only 1 of the 4 golden cases** on every
  trigger, including the weekly cron. Regressions in the other three
  (clean-code false-positive avoidance, docs/test detection) would go
  uncaught by the live judge; the free, canned-judge classes still cover
  all 4 cases structurally, but that's not the same as a real model
  re-scoring them.
- **LLM line attribution was two lines off on a real run.** Not
  independently re-measured beyond that one observation.
- **Adjacent-line duplicate findings from different agents escape
  dedupe.** `dedupe_findings`'s key is exact-match `(file_path,
  line_start)` — two different agents' findings on adjacent, not identical,
  lines both survive as separate findings instead of colliding. Real
  interval-overlap detection would be needed to close this.
- **`tests/integration/test_events_spine.py` still writes fixture rows
  into the real, production, append-only `agent_events` table on every
  free `pytest` run** (~4 rows/run, permanently, since the table can never
  be cleaned by design). Other test files in the same suite use per-run
  disposable schemas; this one was never migrated to that pattern.
- **No dashboard authentication.** Anyone who can reach the port sees the
  HITL queue, cost data, and trace reconstruction.
- **The synthetic-row cost exclusion is a denylist, not a provenance
  flag.** A today-dated row with an unlisted `review_id` prefix counts as
  real spend on `/costs` — proven directly by a rolled-back-transaction
  probe.
- **A high-severity `postcss` npm advisory** (transitive, via `next@15.5.24`)
  remains unresolved — fixable only by a breaking Next 16 upgrade,
  deliberately not taken mid-milestone.
- **No frontend test suite** (no Jest/Vitest/Playwright) — `npm run
  build`/`npm run lint` and a real browser check are the only frontend
  verification.

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
Architecture decisions live in [`.genesis/decisions/`](.genesis/decisions/)
(one ADR recorded so far,
[`0001-local-simulation-first`](.genesis/decisions/0001-local-simulation-first.md));
a rich, teaching-oriented walkthrough of each milestone's build exists in
[`.genesis/explanations/`](.genesis/explanations/) (13 pages, one per
milestone, M1 through M13).

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

## License

MIT — see [LICENSE](LICENSE).
