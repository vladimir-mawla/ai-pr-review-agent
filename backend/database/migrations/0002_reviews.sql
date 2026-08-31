-- 0002_reviews.sql
--
-- M13: a durable "current state of every review" table, backing the
-- dashboard's HITL queue view and (later) the trace view's review-level
-- context. This is the Postgres-backed queue M5's own docstring
-- (backend/hitl/queue.py) named as explicit future work: "A real
-- persistent queue (Postgres-backed, surfaced through a dashboard) is out
-- of scope here ... future work, same as M2's InMemoryJobQueue was a
-- stand-in until M3's Redis queue." M13 is that future work.
--
-- DELIBERATELY NOT append-only, unlike agent_events (migration 0001):
-- this table is a materialized "latest state per review_id", not an audit
-- log. A review that resumes (LangGraph checkpoint replay in tests) or
-- whose status is later changed by a human decision (out of scope here,
-- but a plausible future consumer) must be able to overwrite its own
-- prior row -- hence the ON CONFLICT (review_id) DO UPDATE upsert
-- backend/database/review_store.py uses, and the plain UPDATE grant
-- below. The append-only invariant (.genesis/context-graph.json's
-- events-table-append-only) is scoped to agent_events alone and is
-- unaffected by this table existing.
--
-- Idempotent, same as 0001: IF NOT EXISTS / OR REPLACE / guarded DO
-- blocks throughout, safe to re-run against an already-migrated database.

CREATE TABLE IF NOT EXISTS reviews (
    review_id           TEXT PRIMARY KEY,
    pr_number            INTEGER NOT NULL,
    repository_owner     TEXT NOT NULL,
    repository_name      TEXT NOT NULL,
    head_sha              TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (
        status IN ('POSTED', 'QUEUED_FOR_HITL', 'REJECTED', 'ERROR')
    ),
    overall_confidence   NUMERIC(4, 3) NOT NULL,
    reason                TEXT NOT NULL,
    -- Full findings snapshot (agent_type/severity/category/file_path/
    -- line_start/line_end/confidence/rationale per backend.models.findings.
    -- Finding), so the HITL queue view can render real findings without a
    -- second table/join -- agent_events intentionally does not carry this
    -- (see this module's docstring in review_store.py for why agent_events
    -- alone is insufficient for this view).
    findings              JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL,
    posted_at             TIMESTAMPTZ,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The HITL queue view's whole access pattern: "every review currently
-- QUEUED_FOR_HITL, oldest first".
CREATE INDEX IF NOT EXISTS idx_reviews_status_created_at ON reviews (status, created_at);

-- Reuses the SAME restricted role migration 0001 created for agent_events
-- (agent_events_writer) rather than minting a second application role --
-- both tables are written by the same process (the orchestrator's
-- aggregate_node) through the same DATABASE_URL connection string, and
-- this project's existing pattern is one application role per database,
-- not one per table. UPDATE is granted here (unlike agent_events' explicit
-- REVOKE) because this table is mutable by design -- see the module
-- docstring above.
GRANT SELECT, INSERT, UPDATE ON reviews TO agent_events_writer;
