"""Load the seeded price catalogue into Postgres (upsert, safe to re-run daily).

Usage (from backend/):  uv run python -m grocery_planner.db.seed [YYYY-MM-DD]
The optional date is the anchor for the seed's relative dates (default: today in Sydney).
"""

import sys
from datetime import date

import psycopg

from grocery_planner.db import CONNECT_KWARGS, db_url
from grocery_planner.tools.price_lookup import load_seed_products
from grocery_planner.tools.seed_sql import insert_sql


def seed(dsn: str, anchor: date | None = None) -> int:
    products = load_seed_products(anchor=anchor)
    with psycopg.connect(dsn, **CONNECT_KWARGS) as conn:  # one transaction, one round trip
        conn.execute("\n".join(insert_sql(p) for p in products))
    return len(products)


if __name__ == "__main__":
    anchor = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    print(f"upserted {seed(db_url(), anchor)} products")
