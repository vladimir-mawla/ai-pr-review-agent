-- dev-pgvector-init.sql
--
-- M9: the hybrid-retrieval memory layer's schema -- a local, pre-Tiger-Cloud
-- analog of the spec's eventual `2026-06-tiger-init.sql` (M12), against the
-- `pgvector` service in docker-compose.yml, not the M7 `postgres` events
-- spine service.
--
-- Applied by backend.memory.tiger_client.apply_migrations() against
-- Settings.pgvector_url. This local instance has no separate admin/writer
-- role split (unlike migrations/0001_agent_events.sql's
-- agent_events_writer): `code_chunks` is a derived, fully-rebuildable
-- retrieval index over this repo's own source, not an audit trail that
-- must resist mutation by design (there is no `events-table-append-only`-
-- style invariant over it) -- re-seeding it (backend/memory's
-- HybridRetriever, via scripts/seed_code_chunks.py) legitimately needs to
-- delete and re-insert rows, so a single connection role is the right
-- amount of ceremony here.
--
-- Idempotent: every statement below uses IF NOT EXISTS, so re-running this
-- file against an already-migrated database is a no-op, not an error --
-- required because docker-compose.yml's pgvector service has no volume
-- (see that file's comment), so every `docker compose down && up` re-runs
-- this from scratch, and scripts/seed_code_chunks.py re-applies it on every
-- invocation for the same reason.

-- pgvector's own extension -- provides the VECTOR column type, the `<=>`
-- cosine-distance operator, and the HNSW/IVFFlat index access methods. The
-- pgvector/pgvector:pg16 image (docker-compose.yml) ships this extension
-- pre-built; this just activates it in this specific database.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS code_chunks (
    id         BIGSERIAL PRIMARY KEY,
    -- Repo-relative file path the chunk was extracted from (see
    -- scripts/seed_code_chunks.py's AST-based chunking) -- e.g.
    -- "backend/memory/context_retriever.py".
    path       TEXT NOT NULL,
    -- The chunk's actual source text -- one function/class/module-level
    -- block, per scripts/seed_code_chunks.py's chunking strategy. Both the
    -- full-text index below and the embedding column are derived from
    -- this.
    content    TEXT NOT NULL,
    -- Per the spec's pinned config: OpenAI text-embedding-3-large,
    -- truncated to 256 dimensions via the API's own `dimensions` parameter
    -- (backend/memory/embedder.py's OpenAIEmbedder) -- NOT the model's
    -- native 3072-dim output. NOT NULL: every row must be searchable by
    -- vector similarity, so a chunk with no embedding is not a valid row
    -- at all (the embedder runs before the INSERT, never after).
    embedding  VECTOR(256) NOT NULL,
    -- Generated, not written by application code: Postgres derives and
    -- stores this from `content` on every INSERT (STORED, not VIRTUAL, so
    -- the GIN index below can be built directly against a real column
    -- rather than recomputing to_tsvector(content) on every query). The
    -- 'english' configuration is a literal, constant argument (not looked
    -- up from a session-mutable GUC), which is exactly what makes this
    -- expression usable in a generated column at all -- Postgres requires
    -- a generated column's expression to be immutable for a given row,
    -- and to_tsvector(regconfig, text) qualifies when the regconfig is a
    -- fixed literal like this rather than derived from `default_text_
    -- search_config`.
    content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ANN index over the embedding column: HNSW (Hierarchical Navigable Small
-- World), not IVFFlat -- see docker-compose.yml's pgvector service comment
-- for the full reasoning (in short: IVFFlat's `lists` parameter needs to be
-- tuned to the table's row count *at build time*, which is a poor fit for
-- a table that always starts empty on `docker compose up`; HNSW has no
-- such a-priori tuning requirement and gives good recall from the first
-- row onward). `vector_cosine_ops` -- cosine distance (`<=>`) -- matches
-- how backend.memory.context_retriever.HybridRetriever.search_vector
-- orders its ANN query; OpenAI's own documentation recommends cosine
-- similarity for text-embedding-3-large.
CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding_hnsw
    ON code_chunks USING hnsw (embedding vector_cosine_ops);

-- Full-text index over the generated tsvector column -- GIN (Generalized
-- Inverted Index), the standard, recommended index type for tsvector
-- columns (fast lookups for "which rows contain this lexeme", at the cost
-- of somewhat slower writes than a GiST index -- an entirely acceptable
-- trade-off for this milestone's occasional-bulk-reseed, read-heavy
-- workload).
CREATE INDEX IF NOT EXISTS idx_code_chunks_content_tsv_gin
    ON code_chunks USING gin (content_tsv);
