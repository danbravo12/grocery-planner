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
