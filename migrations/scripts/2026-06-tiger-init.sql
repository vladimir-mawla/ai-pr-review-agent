-- 2026-06-tiger-init.sql
--
-- M12: Tiger Cloud Migration, Stages A-C per ADR-003 (Infra -> Events ->
-- Memory). Applied ONLY against the real Tiger Cloud instance (Settings
-- .events_backend / .memory_backend = "tiger", connection resolved from
-- PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE/PGSSLMODE -- see
-- backend/core/settings.py's Settings.resolve_tiger_dsn) via
-- backend.database.postgres.init_tiger_schema(dsn) -- NEVER against local
-- Postgres/pgvector, and never applied automatically by
-- backend.database.postgres.apply_migrations() or
-- backend.memory.tiger_client.apply_migrations() (the LOCAL-only
-- functions), which is why this file lives in the same
-- migrations/scripts/ directory as dev-pgvector-init.sql but is applied
-- through its own dedicated function rather than a directory-wide glob:
-- the two files are mutually incompatible (this one needs vectorscale/
-- DiskANN/hypertables that do not exist on the local pgvector image;
-- dev-pgvector-init.sql's HNSW index must never also exist on Tiger,
-- since Stage C's whole point is DiskANN replacing HNSW, not
-- supplementing it).
--
-- Idempotent throughout (IF NOT EXISTS / a guarded DO block / policies
-- created with if_not_exists => TRUE), mirroring this project's other
-- migration files -- safe to re-run against an already-migrated Tiger
-- instance.
--
-- Every statement below was run interactively against the real, paid
-- Tiger Cloud instance during L1 BUILD and its real output verified
-- (extensions created, hypertable + chunks + triggers proven to reject
-- real UPDATE/DELETE/TRUNCATE on both the hypertable and a chunk,
-- continuous aggregates proven correct against seeded real+synthetic
-- rows, DiskANN index proven to be planner-selected at 3000-row scale)
-- before being captured here -- see this milestone's build report for
-- the full transcript.

-- =====================================================================
-- STAGE A: INFRA -- vector + vectorscale (DiskANN lives in vectorscale).
-- timescaledb and timescaledb_toolkit are pre-installed by Tiger Cloud
-- itself, not created here.
-- =====================================================================
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;

-- =====================================================================
-- STAGE B: EVENTS -- agent_events as a real TimescaleDB hypertable,
-- chunked by 1-day ranges on `ts`, per the spec's pinned chunking
-- interval.
--
-- SCHEMA DIFFERENCE FROM LOCAL (disclosed): the primary key here is
-- `(id, ts)`, not a bare `id` -- TimescaleDB requires the partitioning
-- column to be part of any unique/primary-key constraint on a
-- hypertable (`create_hypertable` rejects a bare-`id` PK outright). `id`
-- (BIGSERIAL) remains effectively unique in practice via the sequence;
-- the composite constraint is what Postgres/TimescaleDB actually enforce.
-- Every other column is byte-for-byte identical to
-- backend/database/migrations/0001_agent_events.sql's local schema, so
-- backend.database.models.AgentEvent and backend.database.repository.
-- EventRepository's INSERT/SELECT SQL work unmodified against either
-- backend.
-- =====================================================================
CREATE TABLE IF NOT EXISTS agent_events (
    id         BIGSERIAL,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    review_id  TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (
        event_type IN ('span.start', 'span.end', 'llm.call', 'tool.call', 'decision')
    ),
    agent       TEXT,
    model       TEXT,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    cost_usd    NUMERIC(10, 6),
    latency_ms  INTEGER,
    outcome     TEXT,
    confidence  NUMERIC(4, 3),
    PRIMARY KEY (id, ts)
);

SELECT create_hypertable(
    'agent_events', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_agent_events_review_id_ts ON agent_events (review_id, ts);

-- ---------------------------------------------------------------------
-- APPEND-ONLY ENFORCEMENT, mechanism 1: BEFORE ROW triggers for
-- UPDATE/DELETE (identical function/trigger names to local migration
-- 0001, same "fires for every role, superuser included" property).
--
-- VERIFIED (L1 BUILD, live against Tiger): TimescaleDB automatically
-- propagates a ROW-level trigger created on the hypertable to every
-- existing chunk AND to every chunk created afterward -- proven by
-- querying pg_trigger directly against a chunk created before this
-- trigger existed (it was present) and against a chunk created after
-- (also present), and by a real UPDATE/DELETE issued directly against a
-- chunk's own table name (`_timescaledb_internal._hyper_N_M_chunk`),
-- which was rejected exactly like the hypertable-level attempt.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION agent_events_forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'agent_events is append-only: % is not permitted (row id=%)',
        TG_OP,
        OLD.id;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_events_no_update ON agent_events;
CREATE TRIGGER agent_events_no_update
    BEFORE UPDATE ON agent_events
    FOR EACH ROW EXECUTE FUNCTION agent_events_forbid_mutation();

DROP TRIGGER IF EXISTS agent_events_no_delete ON agent_events;
CREATE TRIGGER agent_events_no_delete
    BEFORE DELETE ON agent_events
    FOR EACH ROW EXECUTE FUNCTION agent_events_forbid_mutation();

-- ---------------------------------------------------------------------
-- APPEND-ONLY ENFORCEMENT: TRUNCATE, statement-level trigger.
--
-- CRITICAL FINDING (L1 BUILD, live against Tiger, DISCLOSED -- do not
-- silently rely on this alone): unlike the ROW-level pair above,
-- TimescaleDB does NOT propagate a STATEMENT-level trigger to chunks --
-- neither existing ones nor ones created afterward. This trigger, on the
-- hypertable itself, DOES reject `TRUNCATE agent_events` issued against
-- the hypertable name (proven: real TRUNCATE, real rejection, for both
-- the restricted writer role and the tsdbadmin admin connection). It
-- does NOT reject `TRUNCATE _timescaledb_internal._hyper_N_M_chunk`
-- issued directly against a chunk's own name -- proven concretely: this
-- exact statement, run as tsdbadmin against a real chunk holding real
-- rows, silently succeeded and the rows were gone, no exception raised.
-- Mechanism 2 below (the restricted role's REVOKEd TRUNCATE privilege)
-- is what actually closes that gap -- see its comment for the residual
-- risk that remains even with both mechanisms in place.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION agent_events_forbid_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'agent_events is append-only: TRUNCATE is not permitted';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_events_no_truncate ON agent_events;
CREATE TRIGGER agent_events_no_truncate
    BEFORE TRUNCATE ON agent_events
    FOR EACH STATEMENT EXECUTE FUNCTION agent_events_forbid_truncate();

-- ---------------------------------------------------------------------
-- APPEND-ONLY ENFORCEMENT, mechanism 2: a restricted, non-owning
-- `agent_events_writer` role -- SELECT + INSERT only, UPDATE/DELETE/
-- TRUNCATE explicitly revoked. On local Postgres (migration 0001) this
-- role is DEFENSE IN DEPTH ONLY (the trigger is what actually makes
-- append-only true there, since a REVOKE is silently ineffective against
-- a true superuser). On Tiger's HYPERTABLE, this role is load-bearing,
-- not merely defense in depth: it is the ONLY mechanism proven to close
-- the chunk-level TRUNCATE gap above for a non-owning role, because
-- PostgreSQL privilege GRANTs/REVOKEs on a hypertable are automatically
-- propagated by TimescaleDB to every chunk -- existing AND future ones
-- (verified: a brand-new chunk created by an INSERT with a `ts` five
-- days in the future, after this role already existed, carried the
-- identical restricted ACL with no manual step). Real, live proof: as
-- `agent_events_writer`, `TRUNCATE`/`UPDATE`/`DELETE` issued directly
-- against a real chunk's own table name each failed with
-- "permission denied for table ..." -- rejected by the privilege system
-- itself, before any trigger could even run.
--
-- RESIDUAL, DISCLOSED GAP: this closes the chunk-level TRUNCATE hole for
-- `agent_events_writer` (the role every application code path actually
-- connects as), but NOT for the owning admin role (tsdbadmin) itself --
-- REVOKE cannot restrict a table's own owner, and no statement-level
-- trigger reaches a chunk directly (see above). An operator connected as
-- tsdbadmin who deliberately runs `TRUNCATE` against a specific internal
-- chunk name bypasses both mechanisms. This is judged acceptable, not
-- silently ignored: (1) no production code path ever connects as
-- tsdbadmin -- every real write goes through agent_events_writer, for
-- which the gap is fully closed; (2) doing this requires deliberately
-- discovering and typing an internal `_timescaledb_internal` relation
-- name, not something an ordinary mistake or normal operation could
-- stumble into; (3) this is the same class of "an admin/superuser
-- connection with DDL rights can always disable a trigger or drop a
-- role's restrictions" limitation local's own design already accepts for
-- its own "postgres" superuser -- Tiger's hypertable does not claim a
-- stronger guarantee than local ever provided for its own admin
-- connection.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_events_writer') THEN
        CREATE ROLE agent_events_writer LOGIN;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE tsdb TO agent_events_writer;
GRANT USAGE ON SCHEMA public TO agent_events_writer;
GRANT SELECT, INSERT ON agent_events TO agent_events_writer;
GRANT USAGE, SELECT ON SEQUENCE agent_events_id_seq TO agent_events_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON agent_events FROM agent_events_writer;

-- This migration does NOT set/rotate agent_events_writer's password --
-- that is a secret (TIGER_EVENTS_WRITER_PASSWORD in .env, never
-- committed) set once, out of band, via `ALTER ROLE ... PASSWORD`, by
-- whoever provisions a given Tiger instance. Re-running this file never
-- touches the password, so it is safe to re-apply without accidentally
-- invalidating a credential already handed out.

-- =====================================================================
-- CONTINUOUS AGGREGATES: agent_health_1m (1-minute buckets: llm.call
-- count, cost sum, p95 latency, per agent/model) and pr_cost_hourly
-- (hourly per-review cost/token rollup) -- the two the spec names.
--
-- THE SYNTHETIC-ROW EXCLUSION FILTER, BAKED IN AT THE VIEW DEFINITION,
-- NOT APPLIED LATER (this is the whole point, per M13's own
-- $97k-contamination history): backend.database.repository's
-- `_TEST_FIXTURE_REVIEW_ID_PREFIXES` list is duplicated here as literal
-- `NOT LIKE` clauses in the continuous aggregate's own WHERE clause, so
-- an excluded row is never summed into a bucket in the first place -- a
-- continuous aggregate that pre-aggregated BEFORE this filter, with the
-- filter only applied afterward by the reading query, would silently
-- re-admit synthetic rows the moment anyone forgot to repeat the filter
-- at read time. THESE TWO LISTS MUST BE KEPT IN SYNC BY HAND (there is
-- no way for SQL to import a Python constant) -- see
-- backend.database.repository._TEST_FIXTURE_REVIEW_ID_PREFIXES's own
-- comment, and tests/integration/test_tiger_migration.py's
-- TestContinuousAggregateExclusionSurvivesTheSwap, which proves the two
-- stay in sync by seeding a real row under a prefix NOT in this list and
-- asserting it right IS counted (the two lists actually being
-- consulted, not just present).
--
-- `future-dated` exclusion (the OTHER half of the local, per-request
-- filter in _AGGREGATE_LLM_CALLS_BY_AGENT_SQL) is NOT baked into the
-- view -- it does not need to be: a row with `ts` in the future lands in
-- a future time bucket, and any reader (this migration's own
-- consumer, backend.database.repository.EventRepository.
-- aggregate_llm_calls_by_agent) applies its own `bucket <= now()` bound
-- at query time, structurally excluding it the same way the pre-swap
-- query's `ts <= now()` clause did.
-- =====================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS agent_health_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', ts) AS bucket,
    agent,
    model,
    count(*) AS call_count,
    sum(cost_usd) AS cost_usd_sum,
    -- Raw sums (not just an average-of-averages) so a reader can combine
    -- multiple buckets into one EXACT weighted average via
    -- sum(latency_ms_sum) / sum(call_count) -- precisely what
    -- backend.database.repository.EventRepository.aggregate_llm_calls_by_agent's
    -- Tiger path does (the M13 swap's own SELECT list, not merely its
    -- FROM/GROUP BY -- see that method's docstring for why the "narrow
    -- swap" claim needed this correction).
    sum(latency_ms) AS latency_ms_sum,
    sum(tokens_in) AS tokens_in_sum,
    sum(tokens_out) AS tokens_out_sum,
    -- timescaledb_toolkit's percentile_agg: a partial-aggregable sketch
    -- (t-digest-family), unlike percentile_cont/DISC, which cannot be
    -- incrementally/continuously aggregated. approx_percentile(0.95, ...)
    -- reads this back as an approximate p95 at query time -- infrastructure
    -- this milestone's success criteria names explicitly; not yet consumed
    -- by any dashboard endpoint (disclosed scope boundary, same as
    -- pr_cost_hourly below).
    percentile_agg(latency_ms::double precision) AS latency_percentile_agg
FROM agent_events
WHERE event_type = 'llm.call'
  AND review_id NOT LIKE 'budget-guard-%'
  AND review_id NOT LIKE 'precision-%'
  AND review_id NOT LIKE 'trace-reconstruction-%'
  AND review_id NOT LIKE 'orchestrator-run-%'
  AND review_id NOT LIKE 'append-only-%'
  AND review_id NOT LIKE 'live-test-%'
  AND review_id NOT LIKE 'm11-live-%'
  AND review_id NOT LIKE 'm12-%'
GROUP BY bucket, agent, model
WITH NO DATA;

SELECT add_continuous_aggregate_policy('agent_health_1m',
    start_offset => INTERVAL '1 hour',
    end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS pr_cost_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts) AS bucket,
    review_id,
    count(*) AS call_count,
    sum(cost_usd) AS cost_usd_sum,
    sum(tokens_in) AS tokens_in_sum,
    sum(tokens_out) AS tokens_out_sum
FROM agent_events
WHERE event_type = 'llm.call'
  AND review_id NOT LIKE 'budget-guard-%'
  AND review_id NOT LIKE 'precision-%'
  AND review_id NOT LIKE 'trace-reconstruction-%'
  AND review_id NOT LIKE 'orchestrator-run-%'
  AND review_id NOT LIKE 'append-only-%'
  AND review_id NOT LIKE 'live-test-%'
  AND review_id NOT LIKE 'm11-live-%'
  AND review_id NOT LIKE 'm12-%'
GROUP BY bucket, review_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('pr_cost_hourly',
    start_offset => INTERVAL '3 hours',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);

-- REAL GAP FOUND AND CLOSED (L1 BUILD, live against Tiger): a continuous
-- aggregate is backed by its own materialized hypertable/view -- granting
-- SELECT/INSERT on `agent_events` alone does NOT also grant SELECT on
-- these views. Without this, EventRepository.aggregate_llm_calls_by_agent's
-- Tiger path (which reads agent_health_1m as agent_events_writer, per
-- Settings.resolve_tiger_writer_dsn) fails outright with
-- "permission denied for view agent_health_1m" -- caught by this
-- migration's own live test suite
-- (tests/integration/test_tiger_migration.py), not discovered later at
-- the dashboard. pr_cost_hourly is granted too for consistency, even
-- though no application code reads it yet.
GRANT SELECT ON agent_health_1m TO agent_events_writer;
GRANT SELECT ON pr_cost_hourly TO agent_events_writer;

-- =====================================================================
-- STAGE C: MEMORY -- code_chunks with a real DiskANN index
-- (pgvectorscale) instead of local's HNSW, same VECTOR(256) column and
-- FTS GIN index as migrations/scripts/dev-pgvector-init.sql so
-- backend.memory.context_retriever.HybridRetriever's SQL is unchanged
-- against either backend.
-- =====================================================================
CREATE TABLE IF NOT EXISTS code_chunks (
    id         BIGSERIAL PRIMARY KEY,
    path       TEXT NOT NULL,
    content    TEXT NOT NULL,
    embedding  VECTOR(256) NOT NULL,
    content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- DiskANN (pgvectorscale) ANN index -- verified (L1 BUILD, live against
-- Tiger) that the planner actually chooses this index (an `Index Scan
-- using idx_code_chunks_embedding_diskann`, not a sequential scan) for a
-- `ORDER BY embedding <=> ... LIMIT` query at 3000-row scale.
CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding_diskann
    ON code_chunks USING diskann (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_code_chunks_content_tsv_gin
    ON code_chunks USING gin (content_tsv);
