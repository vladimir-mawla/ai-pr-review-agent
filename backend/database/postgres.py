"""Connection and migration management for the events-spine Postgres database.

Owns: applying ``backend/database/migrations/*.sql`` against an ADMIN
connection. The DSN application code actually writes through at runtime
(the restricted ``agent_events_writer`` role migration 0001 creates) lives
in ``backend.core.settings.Settings.database_url`` -- this module never
constructs that connection itself, it only runs the migrations that make
the role and table exist.

M12: also owns ``init_tiger_schema``, the Tiger Cloud analog of
``apply_migrations`` above -- applies the ONE combined
``migrations/scripts/2026-06-tiger-init.sql`` file (Stages A-C: extensions,
the real ``agent_events`` hypertable + append-only triggers + restricted
role + continuous aggregates, and ``code_chunks`` + its DiskANN index)
against the real Tiger Cloud instance (``Settings.resolve_tiger_dsn()`` --
never against local Postgres/pgvector, and this function is never called by
``apply_migrations`` above or by
``backend.memory.tiger_client.apply_migrations`` -- see that Tiger init
file's own header comment for why the two migration paths must stay
mutually exclusive, not merged into one directory-wide glob.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# migrations/scripts/, at the repo root -- the SAME directory
# backend.memory.tiger_client's local pgvector migration lives in (mirrors
# that module's own MIGRATIONS_DIR comment), but this constant names one
# specific file within it rather than globbing the whole directory, since
# glob-applying both files against either target would be actively wrong
# (see this file's module docstring and the migration file's own header).
_TIGER_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations" / "scripts"
_TIGER_MIGRATION_FILE = "2026-06-tiger-init.sql"


def apply_migrations(admin_dsn: str) -> None:
    """Apply every ``*.sql`` file in ``migrations/``, in filename order.

    Idempotent: each migration file is itself written with
    ``IF NOT EXISTS`` / ``OR REPLACE`` / a guarded ``DO`` block (see
    ``0001_agent_events.sql``), so re-running this against an
    already-migrated database is a no-op, not an error -- required because
    ``docker-compose.yml``'s postgres service has no volume, so a fresh
    ``docker compose up`` starts from an empty database every time, and a
    demo run within the same session must also be safe to invoke more than
    once.

    Must be called with an ADMIN connection string (a superuser or
    equivalent -- ``Settings.database_admin_url``, never
    ``Settings.database_url``): the restricted ``agent_events_writer`` role
    these migrations create is deliberately never granted
    CREATE TABLE/ROLE/TRIGGER privileges, so it cannot apply its own
    migrations.

    Uses ``psycopg.ClientCursor`` (client-side parameter binding, PostgreSQL's
    simple query protocol) rather than the default server-side-binding
    ``Cursor``, because each migration file is a multi-statement script
    (several ``CREATE``/``GRANT``/``DO`` statements per file) -- the default
    cursor's extended query protocol accepts only one statement per
    ``execute()`` call and would reject the rest of the file.
    """
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    with psycopg.connect(
        admin_dsn, autocommit=True, cursor_factory=psycopg.ClientCursor
    ) as conn:
        for path in migration_files:
            conn.execute(path.read_text())


def init_tiger_schema(admin_dsn: str) -> None:
    """Apply ``migrations/scripts/2026-06-tiger-init.sql`` against a real Tiger Cloud instance.

    The Tiger-only counterpart to ``apply_migrations`` above: same
    idempotent-reapply contract (every statement in that file uses
    ``IF NOT EXISTS``/a guarded ``DO`` block/``if_not_exists => TRUE``
    policy creation, so calling this more than once against an
    already-migrated instance is a no-op, not an error), same
    ``ClientCursor``-for-multi-statement-scripts reasoning as
    ``apply_migrations``.

    Must be called with an ADMIN connection string
    (``Settings.resolve_tiger_dsn()``, e.g. Tiger Cloud's ``tsdbadmin`` --
    NEVER ``Settings.resolve_tiger_writer_dsn()``): creating the hypertable,
    extensions, continuous aggregates, and the restricted
    ``agent_events_writer`` role itself all require privileges that
    restricted role is deliberately never granted, mirroring
    ``apply_migrations``'s own admin-vs-writer split for local Postgres.
    """
    path = _TIGER_MIGRATIONS_DIR / _TIGER_MIGRATION_FILE
    with psycopg.connect(
        admin_dsn, autocommit=True, cursor_factory=psycopg.ClientCursor
    ) as conn:
        conn.execute(path.read_text())
