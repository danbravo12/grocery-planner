"""The canonical generic-ingredient list, derived from the seeded catalogue."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from grocery_planner.tools.price_lookup import DEFAULT_SEED_PATH


class VocabEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str  # the catalogue's first search term for the ingredient; also a valid search term
    category: str
    unit: Literal["g", "ml", "each"]  # canonical unit: how the catalogue sells it
    pantry_staple: bool


@lru_cache
def ingredient_vocabulary(path: Path = DEFAULT_SEED_PATH) -> dict[str, VocabEntry]:
    """name -> entry, one per distinct generic ingredient, ordered by category then name.
    Reused by the price agent. Pantry staples come from the seed file's `pantry_staples` list."""
    raw = json.loads(Path(path).read_text())
    staples = set(raw.get("pantry_staples", []))
    found: dict[str, VocabEntry] = {}
    for p in raw["products"]:
        # product_id is store prefix + ingredient slug + variant number: "col-eye-fillet-01"
        slug = p["product_id"].split("-", 1)[1].rsplit("-", 1)[0]
        entry = VocabEntry(
            name=p["search_terms"][0],
            category=p["category"],
            unit=p["pack_unit"],
            pantry_staple=p["search_terms"][0] in staples,
        )
        known = found.setdefault(slug, entry)
        if known != entry:
            raise ValueError(f"inconsistent catalogue rows for ingredient '{slug}'")
    names = [e.name for e in found.values()]
    if len(set(names)) != len(names):
        raise ValueError("two catalogue ingredients share a name")
    unknown = staples - set(names)
    if unknown:
        raise ValueError(f"pantry_staples not in catalogue: {sorted(unknown)}")
    return {e.name: e for e in sorted(found.values(), key=lambda e: (e.category, e.name))}


@lru_cache
def _product_foods(path: Path = DEFAULT_SEED_PATH) -> dict[str, frozenset[str]]:
    """product_id -> every name its generic ingredient goes by: the vocabulary name plus the
    search terms of all products of that ingredient (so "fillet mignon" is beef eye fillet)."""
    raw = json.loads(Path(path).read_text())
    names: dict[str, set[str]] = {}
    slug_of: dict[str, str] = {}
    for p in raw["products"]:
        slug = p["product_id"].split("-", 1)[1].rsplit("-", 1)[0]
        slug_of[p["product_id"]] = slug
        names.setdefault(slug, set()).update(t.lower() for t in p["search_terms"])
    return {pid: frozenset(names[slug]) for pid, slug in slug_of.items()}


def same_food(item_name: str, product_id: str, path: Path = DEFAULT_SEED_PATH) -> bool:
    """Is this catalogue product the item's own generic ingredient? Each product belongs to
    exactly one ingredient (the one it was seeded under), so coconut milk is not milk and hash
    browns are not potato gems. A product id that is not in the catalogue is not judged."""
    foods = _product_foods(path).get(product_id)
    return True if foods is None else item_name.strip().lower() in foods
