# ADR 0001 — Local-simulation-first build order for the PR review agent

- **Date:** 2026-08-29T14:35:08Z
- **Status:** accepted
- **Phase / milestone:** Genesis G5 (plan slicing), applies to all 13 milestones (M1–M13)

## Context

The repo is freshly `git init`'d — one commit, only the `.genesis/` spine exists, no
application code. The spec being implemented ("Designing an AI Pull-Request Review Agent")
describes a large, 20-phase design: a multi-process system (FastAPI ingress, Redis/ARQ queue,
LangGraph orchestrator fanning out to four LLM specialist agents, an aggregator, a Tiger
Cloud-backed memory/events/dashboard store, and a Next.js frontend). Building any of the
credentialed parts of this — a real GitHub App, an LLM API key, a Tiger Cloud instance —
costs real money and requires the user to go set up external accounts before a single
milestone can be verified. The plan has to be sliced into an order that a driver model can
execute milestone-by-milestone without stalling on "waiting for credentials" on milestone 1,
while still building the architecture the spec actually specifies rather than a disposable
prototype that gets rewritten once real integrations arrive.

## Decision

We considered three fundamentally different build orders:

1. **Approach A — Thin Vertical Slice.** Wire up a real GitHub webhook → one stub
   "specialist" → a real posted comment first, then widen into the full fan-out/retrieval/HITL
   design. Rejected: it requires a GitHub App before milestone 1 is even done, which violates
   the "M1 needs no external credentials" constraint, and it forces architecture decisions to
   be made under live-integration pressure instead of deliberately.
2. **Approach B — Infrastructure-First.** Build the full data spine, module skeleton,
   reliability layer, and observability plumbing before any agent logic exists. Rejected:
   nothing is demoable to a non-technical stakeholder for several milestones, and it risks the
   "infrastructure astronaut" failure mode of over-building plumbing for agent behavior that
   hasn't been validated yet.
3. **Approach C — Local-Simulation-First (chosen).** Build the entire cognitive pipeline —
   ingress, queue, orchestrator, four agents, aggregator, HITL gate, retrieval — against local
   fixtures and mocks (a hand-signed fake webhook payload, Dockerized Redis and
   Postgres+pgvector, a mocked GitHub client, a real LLM call only once a key exists) so that
   every milestone through the full local dry-run needs zero cloud accounts. Real GitHub,
   Tiger Cloud, and deployment become separate, later milestones that swap one adapter at a
   time behind interfaces the spec already designs for (the `workflow_engine` abstraction, the
   staged four-phase Tiger Cloud migration plan).

**We choose Approach C.** It is the only approach that satisfies the hard constraint that M1,
and roughly the first two-thirds of the plan, need no GitHub App / LLM key / Tiger Cloud
account, while still building the real architecture rather than a throwaway prototype — the
spec already committed to clean swap points (the `workflow_engine` interface, the staged
Tiger migration), so deferring credentials defers cost and setup without deferring real
design decisions.

## Consequences

- **Positive:** 8 of the 13 milestones (M1–M7, plus M9's container path) need no external
  credentials at all and are runnable with nothing but Docker and Python/Node installed
  locally, ordered first per the user-approved "credential-free milestones first" rule.
- **Positive:** hard-to-retrofit interface decisions (the `workflow_engine` abstraction, the
  mock-backed `github_client` interface, the local-vs-Tiger memory client split) get made
  early and deliberately, under the discipline of "this must also work against a mock."
- **Negative / cost:** two Tiger Cloud migration milestones are unavoidable later (M12, and
  the Tiger-specific verification folded into M9/M7's later re-run) — a local
  Postgres+pgvector container cannot fully simulate DiskANN, hypertables, or continuous
  aggregates, so that gap has to be closed with real infrastructure eventually, not mocked
  away indefinitely.
- **Negative / cost:** mock-drift risk — a mocked GitHub client (used through M10) can silently
  diverge from the real GitHub REST API's edge cases, producing a system that "works locally,
  breaks on the first real webhook." This is mitigated by requiring a contract test
  (`tests/contract/test_github_client_contract.py`) at M11, when the real client is swapped
  in, that pins the mock's assumed shape against the real API's observed responses.
- **Invariant added to context-graph.json:** none directly from this ADR — the four invariants
  already added in G2 (inward-only-dependencies, hmac-verified-before-any-work,
  budget-guard-hard-blocks, events-table-append-only) are the checkable rules this build order
  is designed to make achievable milestone-by-milestone; this ADR governs sequencing, not a
  new structural invariant.

## Alternatives rejected

- **Approach A (Thin Vertical Slice)** — requires GitHub App credentials before M1 can even be
  demoed; violates the credential-free-first ordering constraint and front-loads integration
  risk over design deliberation.
- **Approach B (Infrastructure-First)** — nothing observable for several milestones, and risks
  building reliability/observability plumbing sized for agent behavior that has never actually
  run yet.

<!-- Copy this file to NNNN-<slug>.md for each irreversible decision.
     Then add a one-line pointer in implementation-notes.html "Decisions that bind". -->
