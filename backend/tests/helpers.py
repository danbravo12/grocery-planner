"""Builders for PricedItem fixtures, driven by the real seed through search + packs_needed."""

from datetime import date, datetime
from decimal import Decimal

from grocery_planner.domain import MatchTier, PricedItem, Store
from grocery_planner.tools.price_lookup import SeededFilePriceSource, packs_needed

AS_OF = date(2026, 10, 12)
SOURCE = SeededFilePriceSource(anchor=AS_OF)
COLES, WOOL = Store.COLES, Store.WOOLWORTHS


def priced(
    item_id: str,
    store: Store,
    term: str,
    product_id: str,
    *,
    tier: MatchTier = "equivalent",
    price: str | None = None,
    factor: str | None = None,
) -> PricedItem:
    """Look the product up in the seed, buy one pack, and build the row.
    `price` / `factor` override the seed price to construct a scenario."""
    product = next(p for p in SOURCE.search(term, store, 50) if p.product_id == product_id)
    if price is not None:
        product = product.model_copy(update={"price": Decimal(price)})
    if factor is not None:
        product = product.model_copy(update={"price": product.price * Decimal(factor)})
    packs, total, leftover = packs_needed(product.pack_size, product.pack_unit, product)
    return PricedItem(
        item_id=item_id,
        item_name=term,
        category=product.category,
        store=store,
        product_id=product.product_id,
        product_name=product.product_name,
        brand=product.brand,
        brand_label=product.brand_label,
        pack_size=product.pack_size,
        pack_unit=product.pack_unit,
        price=product.price,
        unit_price=product.unit_price,
        on_special=product.on_special,
        special_end_date=product.special_end_date,
        packs_needed=packs,
        purchased_qty=packs * product.pack_size,
        leftover_qty=leftover,
        total_cost=total,
        match_tier=tier,
        match_confidence="high",
        price_timestamp=product.price_timestamp,
        data_source=product.data_source,
    )


def not_found(item_id: str, store: Store, name: str = "thing") -> PricedItem:
    return PricedItem(
        item_id=item_id, item_name=name, category="x", store=store,
        match_tier="not_found", match_confidence="low",
    )  # fmt: skip


def synthetic(item_id: str, store: Store, total: str, tier: MatchTier = "equivalent") -> PricedItem:
    """A minimal row with a fixed cost, for rules that don't depend on seed data."""
    if tier == "not_found":
        return not_found(item_id, store)
    cost = Decimal(total)
    return PricedItem(
        item_id=item_id,
        item_name=item_id,
        category="x",
        store=store,
        product_id=f"{store.value}-{item_id}",
        product_name=item_id,
        brand="b",
        brand_label="branded",
        pack_size=Decimal(1),
        pack_unit="each",
        price=cost,
        unit_price=cost,
        packs_needed=1,
        purchased_qty=Decimal(1),
        leftover_qty=Decimal(0),
        total_cost=cost,
        match_tier=tier,
        match_confidence="high",
        price_timestamp=datetime(2026, 10, 12),
        data_source="seeded",
    )


# Edge case d: item_id, search term, product slug (house-brand product "-01" at each store)
BASKET_D = [
    ("milk", "milk", "milk"),
    ("bread", "white bread", "white-bread"),
    ("eggs", "eggs", "eggs"),
    ("banana", "banana", "banana"),
    ("onion", "brown onion", "brown-onion"),
    ("mince", "beef mince", "beef-mince"),
    ("cheddar", "cheddar", "cheddar"),
    ("carrot", "carrot", "carrot"),
]


def basket_d(coles_factor: str | None = None, wool_factor: str | None = None) -> list[PricedItem]:
    out = []
    for item_id, term, slug in BASKET_D:
        out.append(priced(item_id, COLES, term, f"col-{slug}-01", factor=coles_factor))
        out.append(priced(item_id, WOOL, term, f"wol-{slug}-01", factor=wool_factor))
    return out


def fillet(col_price: str) -> list[PricedItem]:
    """Beef eye fillet, real Woolworths price ($21.50), Coles price overridden."""
    return [
        priced("fillet", COLES, "eye fillet", "col-eye-fillet-01", price=col_price),
        priced("fillet", WOOL, "eye fillet", "wol-eye-fillet-01"),
    ]
