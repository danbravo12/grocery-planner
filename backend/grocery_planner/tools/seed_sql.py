"""Emit INSERT statements for price_catalogue from the seed JSON.

Usage (from backend/):
    uv run python -m grocery_planner.tools.seed_sql > ../db/seed/price_catalogue.sql
"""

import json
import sys
from pathlib import Path

from grocery_planner.domain import CatalogueProduct
from grocery_planner.tools.price_lookup import DEFAULT_SEED_PATH

_COLUMNS = (
    "store, product_id, product_name, brand, brand_label, category, search_terms, "
    "pack_size, pack_unit, price, on_special, special_end_date, price_timestamp, "
    "data_source, quality_band"
)


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def insert_sql(p: CatalogueProduct) -> str:
    values = [
        _q(p.store.value),
        _q(p.product_id),
        _q(p.product_name),
        _q(p.brand),
        _q(p.brand_label),
        _q(p.category),
        "ARRAY[" + ", ".join(_q(t) for t in p.search_terms) + "]::text[]",
        str(p.pack_size),
        _q(p.pack_unit),
        str(p.price),
        "true" if p.on_special else "false",
        _q(p.special_end_date.isoformat()) if p.special_end_date else "NULL",
        _q(p.price_timestamp.isoformat()),
        _q(p.data_source),
        _q(p.quality_band),
    ]
    return (
        f"INSERT INTO price_catalogue ({_COLUMNS}) VALUES ({', '.join(values)})\n"
        "ON CONFLICT (store, product_id) DO UPDATE SET "
        "product_name = EXCLUDED.product_name, brand = EXCLUDED.brand, "
        "brand_label = EXCLUDED.brand_label, category = EXCLUDED.category, "
        "search_terms = EXCLUDED.search_terms, pack_size = EXCLUDED.pack_size, "
        "pack_unit = EXCLUDED.pack_unit, price = EXCLUDED.price, "
        "on_special = EXCLUDED.on_special, special_end_date = EXCLUDED.special_end_date, "
        "price_timestamp = EXCLUDED.price_timestamp, data_source = EXCLUDED.data_source, "
        "quality_band = EXCLUDED.quality_band;"
    )


def seed_sql(path: Path = DEFAULT_SEED_PATH) -> str:
    products = [
        CatalogueProduct.model_validate(p) for p in json.loads(path.read_text())["products"]
    ]
    return "BEGIN;\n" + "\n".join(insert_sql(p) for p in products) + "\nCOMMIT;\n"


if __name__ == "__main__":
    sys.stdout.write(seed_sql())
