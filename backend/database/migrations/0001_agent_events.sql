-- 0001_agent_events.sql
--
-- M7: the events spine's append-only table, its time-ordered-by-run-id
-- index, and its append-only enforcement.
--
-- Applied by backend.database.postgres.apply_migrations() against an ADMIN
-- connection (Settings.database_admin_url -- the "postgres" superuser in
-- local dev) -- never against the restricted application role this file
-- itself creates (agent_events_writer), since creating roles/tables/
-- triggers requires privileges that role is deliberately never granted.
--
-- Idempotent: every statement below uses IF NOT EXISTS / OR REPLACE / a
-- guarded DO block, so re-running this file against an already-migrated
-- database is a no-op, not an error (docker-compose.yml's postgres service
-- has no volume, so every `docker compose down && up` re-runs this from
-- scratch, and a demo run within the same up/down cycle must also be safe
-- to re-run without erroring).

CREATE TABLE IF NOT EXISTS agent_events (
    id         BIGSERIAL PRIMARY KEY,
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
    confidence  NUMERIC(4, 3)
);

-- Time-ordered reads by review/run id: exactly the access pattern
-- PLAN.md's M7 demo command (`... WHERE review_id='demo-1' ORDER BY ts`)
-- and backend.observability.audit.reconstruct_review_trace both use.
CREATE INDEX IF NOT EXISTS idx_agent_events_review_id_ts ON agent_events (review_id, ts);

-- ---------------------------------------------------------------------
-- APPEND-ONLY ENFORCEMENT, mechanism 1 of 2: a BEFORE trigger that raises
-- on any UPDATE or DELETE, regardless of which role issues it.
--
-- This is the PRIMARY mechanism, not the GRANT/REVOKE below, because a
-- PostgreSQL superuser bypasses privilege checks entirely -- a REVOKE
-- alone would be silently ineffective against a superuser connection
-- (e.g. the "postgres" admin role this very migration runs as). A BEFORE
-- trigger fires unconditionally for every role, superuser included, so it
-- is what actually makes the events-table-append-only invariant true
-- rather than merely documented.
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
-- APPEND-ONLY ENFORCEMENT, mechanism 2 of 2 (defense in depth): a
-- dedicated, non-superuser application role that can only SELECT and
-- INSERT on agent_events. This is what Settings.database_url actually
-- connects as at application runtime -- never the "postgres" admin
-- superuser used to apply migrations. UPDATE/DELETE/TRUNCATE are
-- explicitly REVOKEd (not merely "never GRANTed") so this file's intent
-- is unambiguous to a future reader, even though the trigger above is
-- what actually makes mutation impossible regardless of role.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_events_writer') THEN
        CREATE ROLE agent_events_writer LOGIN PASSWORD 'agent_events_writer';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE pr_review_agent TO agent_events_writer;
GRANT USAGE ON SCHEMA public TO agent_events_writer;
GRANT SELECT, INSERT ON agent_events TO agent_events_writer;
GRANT USAGE, SELECT ON SEQUENCE agent_events_id_seq TO agent_events_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON agent_events FROM agent_events_writer;
