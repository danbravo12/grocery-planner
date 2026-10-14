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
    """Copy of a matched row with a different total cost (price and unit price follow)."""
    total = Decimal(total)
    assert row.packs_needed and row.pack_size and row.total_cost is not None
    price = total / row.packs_needed
    per = Decimal(1) if row.pack_unit == "each" else Decimal(100)
    return row.model_copy(
        update={
            "total_cost": total,
            "price": price,
            "unit_price": (price / row.pack_size * per).quantize(Decimal("0.0001")),
        }
    )


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
