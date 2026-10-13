"""Apply db/migrations/*.sql in filename order, once each.

Usage (from backend/):  uv run python -m grocery_planner.db.migrate
All pending migrations run in one transaction: either all apply or none do.
"""

from pathlib import Path

import psycopg

from grocery_planner.db import CONNECT_KWARGS, MIGRATIONS_DIR, db_url

_LOCK_KEY = 726_001  # arbitrary; stops two runners applying migrations at once

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
-- Same lockdown as every other table: RLS on, no policies.
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;
"""


def migrate(dsn: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations and return their filenames."""
    applied_now = []
    with psycopg.connect(dsn, **CONNECT_KWARGS) as conn:  # commits on clean exit
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        conn.execute(_TRACKING_TABLE)
        done = {r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations")}
        for path in sorted(directory.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            applied_now.append(path.name)
    return applied_now


if __name__ == "__main__":
    applied = migrate(db_url())
    print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
