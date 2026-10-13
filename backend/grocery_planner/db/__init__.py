from psycopg.rows import dict_row

from grocery_planner.env import BACKEND_DIR, getenv

MIGRATIONS_DIR = BACKEND_DIR.parent / "db" / "migrations"

# prepare_threshold=None: no server-side prepared statements, so this also works through
# Supabase's transaction pooler (pgbouncer).
CONNECT_KWARGS = {"row_factory": dict_row, "prepare_threshold": None}


def db_url() -> str:
    """SUPABASE_DB_URL from the environment, falling back to backend/.env. Never log it."""
    url = getenv("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_DB_URL is not set (see .env.example)")
    return url
