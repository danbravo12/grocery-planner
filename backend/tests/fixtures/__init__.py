"""Saved price-agent outputs, loadable without any API call. Evals reuse them.

Regenerate from live runs with:
    SAVE_PRICE_FIXTURES=1 uv run pytest -m live tests/test_price_live.py
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from grocery_planner.domain import PricedItem, Store

FIXTURE_DIR = Path(__file__).parent


class PriceFixture(BaseModel):
    name: str
    as_of: date
    captured_with_model: str
    items: list[PricedItem]  # one per item per store, as the price agent returned them


def load_price_fixture(name: str) -> PriceFixture:
    return PriceFixture.model_validate_json((FIXTURE_DIR / f"price_{name}.json").read_text())


def save_price_fixture(fixture: PriceFixture) -> Path:
    path = FIXTURE_DIR / f"price_{fixture.name}.json"
    path.write_text(fixture.model_dump_json(indent=1) + "\n")
    return path


def with_total(row: PricedItem, total: Decimal | str) -> PricedItem:
    """Copy of a matched row with a different total cost (price and unit price follow). A row
    that is not on special has the new price as its regular price too."""
    total = Decimal(total)
    assert row.packs_needed and row.pack_size and row.total_cost is not None
    price = total / row.packs_needed
    per = Decimal(1) if row.pack_unit == "each" else Decimal(100)
    update = {
        "total_cost": total,
        "price": price,
        "unit_price": (price / row.pack_size * per).quantize(Decimal("0.0001")),
    }
    if not row.on_special:
        update |= {"regular_price": price, "regular_total_cost": total}
    return row.model_copy(update=update)


def scale_store(items: list[PricedItem], store: Store, factor: Decimal | str) -> list[PricedItem]:
    """Multiply every matched row's cost at one store."""
    return [
        with_total(r, r.total_cost * Decimal(factor)) if r.store == store and r.total_cost else r
        for r in items
    ]


def large_gap_scenario() -> PriceFixture:
    """Fixture b with Coles 20% dearer on everything: expect one_store (Woolworths)."""
    base = load_price_fixture("b")
    return base.model_copy(
        update={"name": "d", "items": scale_store(base.items, Store.COLES, "1.2")}
    )


def split_scenario() -> PriceFixture:
    """Fixture b reshaped so Coles is cheapest overall (by only $3), but beef mince is $10
    cheaper at Woolworths: a split clears the $8 threshold; expect split."""
    base = load_price_fixture("b")
    items = scale_store(base.items, Store.WOOLWORTHS, "1.33")
    mince = next(r for r in items if r.item_name == "beef mince" and r.store == Store.COLES)
    items = [
        with_total(r, mince.total_cost - Decimal(10))
        if r.item_name == "beef mince" and r.store == Store.WOOLWORTHS
        else r
        for r in items
    ]
    return base.model_copy(update={"name": "e", "items": items})


def _mince_rows(items: list[PricedItem]) -> tuple[str, PricedItem]:
    wool = next(r for r in items if r.item_name == "beef mince" and r.store == Store.WOOLWORTHS)
    return wool.item_id, wool


SPECIAL_AS_OF = date(2026, 10, 19)  # a Monday: the sale week ends tomorrow, Tue 2026-10-20
SPECIAL_ENDS = date(2026, 10, 20)


def special_scenario() -> PriceFixture:
    """Fixture b where Woolworths beef mince is $42.00 on a special ending Tue 2026-10-20 (regular
    $52.00, the same as Coles). Planned on Mon 2026-10-19, Woolworths is $11.40 cheaper overall
    (one_store), but at regular prices the gap is $1.40: the verdict is conditional, valid until
    2026-10-20, with a no_meaningful_difference fallback."""
    base = load_price_fixture("b")
    _, wool = _mince_rows(base.items)
    special = with_total(wool, "42.00").model_copy(
        update={
            "on_special": True,
            "special_end_date": SPECIAL_ENDS,
            "regular_price": Decimal("52.00"),
            "regular_total_cost": Decimal("52.00"),
        }
    )
    items = [special if r is wool else r for r in base.items]
    return base.model_copy(update={"name": "f", "as_of": SPECIAL_AS_OF, "items": items})


def doubtful_match_scenario() -> PriceFixture:
    """Fixture b where the whole saving comes from beef mince at Woolworths ($42.00 vs $52.00)
    and that pick is doubtful: a branded product against Coles' house brand (tier gap) with low
    confidence. Code says one_store; match_quality is the available reason to override."""
    base = load_price_fixture("b")
    _, wool = _mince_rows(base.items)
    doubtful = with_total(wool, "42.00").model_copy(
        update={"brand": "Kingsway", "brand_label": "branded", "match_confidence": "low"}
    )
    items = [doubtful if r is wool else r for r in base.items]
    return base.model_copy(update={"name": "g", "items": items})


def reprice_fixture(fixture: PriceFixture, anchor: date) -> PriceFixture:
    """The same picks, tiers and confidences, re-read from the seed at a new anchor date: the
    special dates, regular prices and timestamps move, nothing is asked of a model."""
    from grocery_planner.tools.price_lookup import SeededFilePriceSource

    source = SeededFilePriceSource(anchor=anchor)
    out = []
    for r in fixture.items:
        if r.product_id is None:
            out.append(r)
            continue
        product = next(
            p for p in source.search(r.item_name, r.store, 50) if p.product_id == r.product_id
        )
        regular = product.regular_price if product.regular_price is not None else product.price
        out.append(
            r.model_copy(
                update={
                    "price": product.price,
                    "regular_price": regular,
                    "unit_price": product.unit_price,
                    "on_special": product.on_special,
                    "special_end_date": product.special_end_date,
                    "total_cost": product.price * r.packs_needed,
                    "regular_total_cost": regular * r.packs_needed,
                    "price_timestamp": product.price_timestamp,
                }
            )
        )
    return fixture.model_copy(update={"as_of": anchor, "items": out})
