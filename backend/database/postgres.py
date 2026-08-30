"""Connection and migration management for the events-spine Postgres database.

Owns: applying ``backend/database/migrations/*.sql`` against an ADMIN
connection. The DSN application code actually writes through at runtime
(the restricted ``agent_events_writer`` role migration 0001 creates) lives
in ``backend.core.settings.Settings.database_url`` -- this module never
constructs that connection itself, it only runs the migrations that make
the role and table exist.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


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
