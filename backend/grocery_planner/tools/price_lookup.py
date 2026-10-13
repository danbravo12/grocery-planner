import json
import re
from decimal import Decimal
from math import floor
from pathlib import Path
from typing import Protocol

from grocery_planner.domain import CatalogueProduct, PackUnit, Store

DEFAULT_SEED_PATH = Path(__file__).resolve().parents[3] / "db" / "seed" / "price_catalogue.json"
DEFAULT_TOLERANCE = Decimal("0.15")


class UnitMismatchError(ValueError):
    """Needed quantity and product pack are in different units; no conversion is done here."""


class PriceSource(Protocol):
    def search(self, term: str, store: Store, limit: int) -> list[CatalogueProduct]: ...


def matches(term: str, product: CatalogueProduct) -> bool:
    """Case-insensitive whole-word phrase match on product name or any search term."""
    needle = term.strip().lower()
    if not needle:
        return False
    pattern = re.compile(rf"\b{re.escape(needle)}\b")
    haystacks = [product.product_name.lower(), *(t.lower() for t in product.search_terms)]
    return any(pattern.search(h) for h in haystacks)


def _rank(products: list[CatalogueProduct], term: str, limit: int) -> list[CatalogueProduct]:
    hits = [p for p in products if matches(term, p)]
    hits.sort(key=lambda p: (p.unit_price, p.product_id))  # cheapest first; stable for ties
    return hits[: max(limit, 0)]


class SeededFilePriceSource:
    def __init__(self, path: Path = DEFAULT_SEED_PATH) -> None:
        raw = json.loads(Path(path).read_text())
        self._products = [CatalogueProduct.model_validate(p) for p in raw["products"]]

    def search(self, term: str, store: Store, limit: int) -> list[CatalogueProduct]:
        return _rank([p for p in self._products if p.store == store], term, limit)


_COLUMNS = (
    "store, product_id, product_name, brand, brand_label, category, search_terms, "
    "pack_size, pack_unit, price, on_special, special_end_date, price_timestamp, "
    "data_source, quality_band"
)


class PostgresPriceSource:
    """Reads price_catalogue. Rows are fetched per store and matched with the same Python
    `matches` function as the seeded source, so both sources behave identically."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def search(self, term: str, store: Store, limit: int) -> list[CatalogueProduct]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM price_catalogue WHERE store = %s", (store.value,)
            ).fetchall()
        return _rank([CatalogueProduct.model_validate(r) for r in rows], term, limit)


def packs_needed(
    needed_qty: Decimal | int, needed_unit: PackUnit, product: CatalogueProduct
) -> tuple[int, Decimal, Decimal]:
    """Whole packs to buy, what they cost, and the leftover quantity (not credited)."""
    if needed_unit != product.pack_unit:
        raise UnitMismatchError(
            f"need {needed_unit} but {product.product_id} is sold in {product.pack_unit}"
        )
    qty = Decimal(needed_qty)
    if qty <= 0:
        raise ValueError("needed_qty must be positive")
    packs = floor(qty // product.pack_size)
    if qty % product.pack_size:
        packs += 1
    return packs, product.price * packs, product.pack_size * packs - qty


def pack_size_within_tolerance(
    needed_qty: Decimal | int, pack_size: Decimal | int, tolerance: Decimal = DEFAULT_TOLERANCE
) -> bool:
    """True when the pack is within `tolerance` (inclusive) of the needed size."""
    needed = Decimal(needed_qty)
    return abs(Decimal(pack_size) - needed) <= tolerance * needed
