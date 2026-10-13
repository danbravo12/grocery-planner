from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from grocery_planner.domain import CatalogueProduct


def make(**over) -> CatalogueProduct:
    base = dict(
        store="coles", product_id="x-1", product_name="Thing", brand="Coles",
        brand_label="house_brand", category="pantry", search_terms=["thing"],
        pack_size="500", pack_unit="g", price="3.50",
        price_timestamp=datetime(2026, 10, 12, tzinfo=UTC), quality_band="standard",
    )  # fmt: skip
    return CatalogueProduct(**{**base, **over})


def test_unit_price_per_100g():
    assert make().unit_price == Decimal("0.7000")


def test_unit_price_each_is_per_item():
    assert make(pack_unit="each", pack_size="12", price="6.00").unit_price == Decimal("0.5000")


def test_float_price_rejected():
    with pytest.raises(ValidationError):
        make(price=3.5)


def test_special_end_requires_special():
    with pytest.raises(ValidationError):
        make(special_end_date="2026-10-15")


def test_unknown_store_rejected():
    with pytest.raises(ValidationError):
        make(store="foodland")


def test_health():
    from fastapi.testclient import TestClient

    from grocery_planner.main import app

    assert TestClient(app).get("/health").json() == {"status": "ok"}
