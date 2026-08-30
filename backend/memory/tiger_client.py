"""Connection and migration management for the local pgvector memory store.

Owns: applying ``migrations/scripts/dev-pgvector-init.sql`` (and any future
file in that directory, in filename order) against the pgvector-enabled
Postgres instance ``docker-compose.yml``'s ``pgvector`` service publishes,
plus a single helper for opening a connection with the ``vector`` type
adapter registered.

NAMED ``tiger_client`` (not ``pgvector_client``), per PLAN.md's M9
freeze-boundary file list, deliberately anticipating M12 ("Tiger Cloud
Migration"): that milestone's own stated scope is replacing *this* file's
local implementation with "the real pgvectorscale/DiskANN path", behind
what should be the same import path so ``backend.memory.context_retriever``
does not need to change which module it imports from. Everything in this
file today talks to a plain pgvector-enabled Postgres container, not Tiger
Cloud -- there is no DiskANN, no hypertable, no Tiger-specific extension
here yet; that is exactly M12's job, not this one's.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

# migrations/scripts/, at the repo root -- NOT backend/database/migrations/
# (that directory is M7's events-spine schema, an unrelated table/database).
# PLAN.md's M9 freeze boundary names this exact path
# ("migrations/scripts/dev-pgvector-init.sql"), mirroring the spec's
# eventual Tiger Cloud migration path ("migrations/scripts/2026-06-tiger-
# init.sql", M12) rather than reusing M7's per-package migrations layout.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations" / "scripts"


def apply_migrations(dsn: str) -> None:
    """Apply every ``*.sql`` file in ``migrations/scripts/``, in filename order.

    Idempotent (every statement in ``dev-pgvector-init.sql`` uses
    ``IF NOT EXISTS``), so this is safe to call on every process start --
    ``scripts/seed_code_chunks.py`` and
    ``tests/integration/test_hybrid_retrieval.py`` both do, mirroring
    ``backend.database.postgres.apply_migrations``'s same idempotent-
    reapply pattern for the M7 events spine, required for the identical
    reason: ``docker-compose.yml``'s ``pgvector`` service has no volume, so
    a fresh ``docker compose up`` starts from an empty database every time.

    Unlike ``backend.database.postgres.apply_migrations``, there is only
    one DSN here, not a separate admin/writer split -- see this module's
    docstring and the migration file's own header comment for why
    ``code_chunks`` does not need one.

    Uses ``psycopg.ClientCursor`` (client-side parameter binding) for the
    same reason ``backend.database.postgres.apply_migrations`` does: each
    migration file is a multi-statement script, and the default cursor's
    extended query protocol accepts only one statement per ``execute()``
    call.
    """
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    with psycopg.connect(dsn, autocommit=True, cursor_factory=psycopg.ClientCursor) as conn:
        for path in migration_files:
            conn.execute(path.read_text())


def connect(dsn: str, *, connect_timeout_seconds: int = 5) -> psycopg.Connection:
    """Open one connection to the pgvector store with the ``vector`` adapter registered.

    ``register_vector`` (the ``pgvector`` package's psycopg3 integration)
    is what lets ``backend.memory.context_retriever.HybridRetriever`` pass
    a plain ``list[float]`` straight through as a query parameter and get
    one back on read, instead of hand-rolling ``'[0.1,0.2,...]'::vector``
    string formatting at every call site -- registered per-connection
    (not process-wide) because it depends on the connection's own type OID
    lookup for ``vector``, done once here rather than repeated by every
    caller.
    """
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=connect_timeout_seconds)
    register_vector(conn)
    return conn
