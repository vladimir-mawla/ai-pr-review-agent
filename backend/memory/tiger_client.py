"""Connection and migration management for the local pgvector memory store.

Owns: applying ``migrations/scripts/dev-pgvector-init.sql`` -- and ONLY that
one file, by explicit name, NOT every ``*.sql`` file in that directory (see
the M12 UPDATE paragraph below for why that distinction became
safety-critical) -- against the pgvector-enabled Postgres instance
``docker-compose.yml``'s ``pgvector`` service publishes, plus a single
helper for opening a connection with the ``vector`` type adapter
registered.

NAMED ``tiger_client`` (not ``pgvector_client``), per PLAN.md's M9
freeze-boundary file list, deliberately anticipating M12 ("Tiger Cloud
Migration"): that milestone's own stated scope was replacing *this* file's
local implementation with "the real pgvectorscale/DiskANN path", behind
what should be the same import path so ``backend.memory.context_retriever``
does not need to change which module it imports from.

M12 UPDATE: that anticipated replacement turned out not to require any
code change to ``connect()`` at all -- it already accepts an arbitrary DSN
string, and a Tiger Cloud connection string (built by
``backend.core.settings.Settings.resolve_tiger_dsn()``) is just another
DSN. What DID need to be new is the DiskANN-backed ``code_chunks`` schema
itself: that lives in ``migrations/scripts/2026-06-tiger-init.sql`` (Stage
C, same directory as ``dev-pgvector-init.sql``, per PLAN.md's freeze
boundary for each milestone), applied via
``backend.database.postgres.init_tiger_schema`` -- a SEPARATE function,
against a SEPARATE, explicitly-named file, from ``apply_migrations``
below.

THIS SEPARATION IS SAFETY-CRITICAL, NOT COSMETIC (L1 BUILD, real
regression caught by this project's own free test suite): an earlier
version of ``apply_migrations`` below globbed every ``*.sql`` file in
``MIGRATIONS_DIR`` -- fine when that directory held only
``dev-pgvector-init.sql``, but the moment ``2026-06-tiger-init.sql``
existed alongside it, the SAME glob silently started applying the Tiger
file's ``CREATE EXTENSION vectorscale``/``diskann`` index statements
against LOCAL pgvector too, which does not have those extensions
installed -- ``tests/integration/test_hybrid_retrieval.py``'s entire
suite failed with ``FeatureNotSupported: extension "vectorscale" is not
available`` the moment this happened. ``apply_migrations`` below now
applies ``_LOCAL_MIGRATION_FILE`` BY NAME, not a directory glob, so a
future third file in this same directory (local or Tiger) cannot silently
leak into the wrong backend's migration path again. ``HybridRetriever``
itself (``backend.memory.context_retriever``) needed no change either
way -- it only ever depends on ``connect()`` returning a working
connection and ``code_chunks`` having the same columns, both of which
hold for either backend.
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
# init.sql", M12) rather than reusing M7's per-package migrations layout --
# which is exactly why, now that M12 has actually added that second file to
# this same directory, this module applies ONE named file, not a glob (see
# the module docstring's M12 UPDATE paragraph).
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations" / "scripts"
_LOCAL_MIGRATION_FILE = "dev-pgvector-init.sql"


def apply_migrations(dsn: str) -> None:
    """Apply ``migrations/scripts/dev-pgvector-init.sql`` -- and only that file.

    Idempotent (every statement in it uses ``IF NOT EXISTS``), so this is
    safe to call on every process start -- ``scripts/seed_code_chunks.py``
    and ``tests/integration/test_hybrid_retrieval.py`` both do, mirroring
    ``backend.database.postgres.apply_migrations``'s same idempotent-
    reapply pattern for the M7 events spine, required for the identical
    reason: ``docker-compose.yml``'s ``pgvector`` service has no volume, so
    a fresh ``docker compose up`` starts from an empty database every time.

    Unlike ``backend.database.postgres.apply_migrations``, there is only
    one DSN here, not a separate admin/writer split -- see this module's
    docstring and the migration file's own header comment for why
    ``code_chunks`` does not need one.

    Deliberately applies ``_LOCAL_MIGRATION_FILE`` by explicit name, NOT
    ``MIGRATIONS_DIR.glob("*.sql")`` -- see this module's M12 UPDATE
    docstring paragraph for the real regression a directory-wide glob
    caused the moment ``2026-06-tiger-init.sql`` existed in this same
    directory. ``backend.database.postgres.init_tiger_schema`` is that
    other file's own, equally explicit, equally single-file counterpart.

    Uses ``psycopg.ClientCursor`` (client-side parameter binding) for the
    same reason ``backend.database.postgres.apply_migrations`` does: the
    migration file is a multi-statement script, and the default cursor's
    extended query protocol accepts only one statement per ``execute()``
    call.
    """
    path = MIGRATIONS_DIR / _LOCAL_MIGRATION_FILE
    with psycopg.connect(dsn, autocommit=True, cursor_factory=psycopg.ClientCursor) as conn:
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
