import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import floor
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from grocery_planner.domain import CatalogueProduct, PackUnit, Store

DEFAULT_SEED_PATH = Path(__file__).resolve().parents[3] / "db" / "seed" / "price_catalogue.json"
DEFAULT_TOLERANCE = Decimal("0.15")
SYDNEY = ZoneInfo("Australia/Sydney")


def today_sydney() -> date:
    return datetime.now(SYDNEY).date()


class UnitMismatchError(ValueError):
    """Needed quantity and product pack are in different units; no conversion is done here."""


class PriceSource(Protocol):
    def search(
        self, term: str, store: Store, limit: int, brand: str | None = None
    ) -> list[CatalogueProduct]: ...


def matches(term: str, product: CatalogueProduct) -> bool:
    """Case-insensitive whole-word phrase match on product name or any search term."""
    needle = term.strip().lower()
    if not needle:
        return False
    pattern = re.compile(rf"\b{re.escape(needle)}\b")
    haystacks = [product.product_name.lower(), *(t.lower() for t in product.search_terms)]
    return any(pattern.search(h) for h in haystacks)


def _rank(
    products: list[CatalogueProduct], term: str, limit: int, brand: str | None
) -> list[CatalogueProduct]:
    hits = [p for p in products if matches(term, p)]
    if brand is not None:  # filter before ranking and limiting so a named brand is never cut off
        wanted = brand.strip().lower()
        hits = [p for p in hits if p.brand.lower() == wanted]
    hits.sort(key=lambda p: (p.unit_price, p.product_id))  # cheapest first; stable for ties
    return hits[: max(limit, 0)]


def resolve_seed_product(raw: dict, anchor: date) -> CatalogueProduct:
    """Turn a seed row's relative dates into real ones, relative to `anchor`."""
    row = dict(raw)
    age = row.pop("price_age_days")
    ends_in = row.pop("special_ends_in_days")
    row["price_timestamp"] = datetime.combine(anchor - timedelta(days=age), time(), tzinfo=SYDNEY)
    row["special_end_date"] = anchor + timedelta(days=ends_in) if ends_in is not None else None
    return CatalogueProduct.model_validate(row)


def load_seed_products(
    path: Path = DEFAULT_SEED_PATH, anchor: date | None = None
) -> list[CatalogueProduct]:
    anchor = anchor or today_sydney()
    raw = json.loads(Path(path).read_text())
    return [resolve_seed_product(p, anchor) for p in raw["products"]]


class SeededFilePriceSource:
    def __init__(self, path: Path = DEFAULT_SEED_PATH, anchor: date | None = None) -> None:
        self._products = load_seed_products(path, anchor)

    def search(
        self, term: str, store: Store, limit: int, brand: str | None = None
    ) -> list[CatalogueProduct]:
        return _rank([p for p in self._products if p.store == store], term, limit, brand)


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

    def search(
        self, term: str, store: Store, limit: int, brand: str | None = None
    ) -> list[CatalogueProduct]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM price_catalogue WHERE store = %s", (store.value,)
            ).fetchall()
        return _rank([CatalogueProduct.model_validate(r) for r in rows], term, limit, brand)


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


def special_active(product: CatalogueProduct, as_of: date) -> bool:
    """On special and not yet past its end date (the end date itself still counts)."""
    return product.on_special and (
        product.special_end_date is None or as_of <= product.special_end_date
    )


def specials_ending_within(
    products: list[CatalogueProduct], as_of: date, days: int
) -> list[CatalogueProduct]:
    """Active specials whose end date falls within `days` days of `as_of` (inclusive)."""
    return [
        p
        for p in products
        if special_active(p, as_of)
        and p.special_end_date is not None
        and (p.special_end_date - as_of).days <= days
    ]
