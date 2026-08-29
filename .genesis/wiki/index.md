# Wiki Index — pr-review-agent

The project knowledge base. Same schema as the agentic-swe-kit wiki: concept pages in `concepts/`,
each with frontmatter and ≥2 `[[wikilinks]]`. The L3 RESEARCH loop writes here; G0 reads here first.

> **Read this file before any milestone (G0 step 1).** Pick candidate pages by name-matching the
> milestone's nouns, then drill in. The wiki is what prevents rebuilding work that already exists.

## Entities (the things this system has)
<!-- - [[concepts/Finding]] — typed object: agent_type, severity, category, file/line, confidence, rationale -->
<!-- - [[concepts/Review]] — one PR's aggregated result: findings + overall_confidence + HITL outcome -->
<!-- - [[concepts/WebhookEvent]] — parsed GitHub pull_request payload plus delivery/idempotency metadata -->
<!-- - [[concepts/code_chunks]] — embedded codebase slices (content + embedding + tsvector) used for retrieval -->
<!-- - [[concepts/agent_events]] — append-only hypertable row: one per span/llm.call/tool.call/decision -->
<!-- - [[concepts/BudgetGuard]] — reads daily spend from the cost rollup and hard-blocks LLM calls past cap -->
<!-- - [[concepts/HITL-queue]] — human approval queue holding low-confidence or CRITICAL reviews -->
<!-- - [[concepts/specialist-agent]] — one of security/quality/tests/docs; shares base_agent shape -->
<!-- - [[concepts/aggregator]] — merges 4 specialists' findings, dedups by (file,line), applies HITL gate -->
<!-- - [[concepts/ARQ-job]] — queued review-request unit consumed by the worker from Redis -->
<!-- - [[concepts/prompt-registry]] — versioned prompt templates, one set per specialist agent -->
<!-- - [[concepts/dispute]] — developer-initiated contest of a posted finding, feeds the feedback loop -->

## Concepts (how it works)
<!-- - [[concepts/hybrid-retrieval]] — DiskANN ANN search + FTS over code_chunks, merged by reciprocal rank fusion -->
<!-- - [[concepts/confidence-weighted-HITL-gate]] — routes on overall_confidence and presence of a CRITICAL finding -->
<!-- - [[concepts/inward-only-dependency-rule]] — ADR-002: 22 modules, dependencies point toward core/ only -->
<!-- - [[concepts/idempotency-key-dedup]] — X-GitHub-Delivery UUID check preventing a double-posted review -->
<!-- - [[concepts/checkpoint-resume]] — LangGraph state checkpointed to Redis per node, resumes after a crash -->

## Sources (research distilled by L3)
<!-- - [[concepts/<source-slug>]] — one-line summary | filed <date> -->

## Seeded from agentic-swe-kit
Relevant global concept pages for this project's phases (pointers only — read on demand). Every
path below was verified to exist with `ls` against `$AGENTIC_SWE_WIKI_ROOT` before being listed.

**llmops-ai-agents** (the four-agent fan-out, retrieval, cost control, eval — spec Parts I/III/IV)
- $AGENTIC_SWE_WIKI_ROOT/llmops-ai-agents/concepts/RAG-Architecture.md — when building the hybrid retrieval path (DiskANN + FTS + reciprocal rank fusion) that grounds each specialist's prompt
- $AGENTIC_SWE_WIKI_ROOT/llmops-ai-agents/concepts/Multi-Agent-Orchestration.md — when designing the aggregator's merge/dedup/confidence logic across the four specialist outputs
- $AGENTIC_SWE_WIKI_ROOT/llmops-ai-agents/concepts/Parallel-and-Fan-Out-Agents.md — when wiring LangGraph's Send API to actually run security/quality/tests/docs concurrently instead of sequentially
- $AGENTIC_SWE_WIKI_ROOT/llmops-ai-agents/concepts/Observability-and-Cost-Control.md — when building the agent_events emitter and the BudgetGuard that reads it before every LLM call
- $AGENTIC_SWE_WIKI_ROOT/llmops-ai-agents/concepts/Evaluation-Frameworks.md — when building the golden-dataset + LLM-as-judge regression gate that blocks CI (spec Phase 09)

**security-engineering** (untrusted webhook input, diff-borne prompt injection, RBAC — spec L8, Phase 11)
- $AGENTIC_SWE_WIKI_ROOT/security-engineering/concepts/Threat-Modeling.md — when writing the Phase 11 threat model for the ingress and the diff-content injection surface
- $AGENTIC_SWE_WIKI_ROOT/security-engineering/concepts/Access-Control.md — when implementing the RBAC dependencies in backend/auth/dependencies.py for the HITL and economics routes
- $AGENTIC_SWE_WIKI_ROOT/security-engineering/concepts/Secure-Development-and-Assurance.md — when verifying HMAC signature checking and secret masking before merging backend/webhook_receiver/validator.py

**distributed-systems** (multi-process ingress → queue → worker → fan-out — spec 3.1/3.2/3.7)
- $AGENTIC_SWE_WIKI_ROOT/distributed-systems/concepts/Fault-Tolerance.md — when designing the retries/circuit-breaker/timeout trio in backend/reliability/** around GitHub and LLM provider calls
- $AGENTIC_SWE_WIKI_ROOT/distributed-systems/concepts/Coordination-and-Clocks.md — when reasoning about idempotency-key ordering and what "resume from last checkpoint" actually guarantees after a worker crash

**clean-architecture** (22-module monolith, ADR-002 dependency rule — spec 4.2)
- $AGENTIC_SWE_WIKI_ROOT/clean-architecture/concepts/Dependency-Rule.md — when enforcing that backend/core/** imports nothing and every other module points inward toward it
- $AGENTIC_SWE_WIKI_ROOT/clean-architecture/concepts/Component-Coupling-Principles.md — when deciding which of the 22 modules in the module map may depend on which, to keep the graph acyclic

**release-it** (the L8 reliability mechanics — spec 3.6, module `reliability/`)
- $AGENTIC_SWE_WIKI_ROOT/release-it/concepts/Circuit-Breaker.md — when implementing backend/reliability/circuit_breaker.py around the LLM client and the GitHub client
- $AGENTIC_SWE_WIKI_ROOT/release-it/concepts/Timeouts.md — when setting the per-call timeout that the production-readiness DoD gate requires on every outbound call

**designing-data-intensive-applications** (one Tiger Cloud store carrying three data shapes — spec Part II)
- $AGENTIC_SWE_WIKI_ROOT/designing-data-intensive-applications/concepts/Polyglot-Persistence.md — when deciding (or re-litigating) one Postgres-compatible store vs. separate Qdrant/Postgres/time-series stores for the memory/truth/time shapes
- $AGENTIC_SWE_WIKI_ROOT/designing-data-intensive-applications/concepts/Partitioning-Strategies.md — when creating the agent_events hypertable partitioned by day and choosing the partition key
