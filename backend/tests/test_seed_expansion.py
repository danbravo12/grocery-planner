"""The second batch of seeded ingredients (65 added to the original 103): every new ingredient is
sold at both stores, ids and specials are well formed, the original rows are untouched, and the
names of the new ingredients do not pull in products of other ingredients except where listed."""

import hashlib
import json
from collections import defaultdict
from decimal import Decimal

from grocery_planner.config import ACTIVE_STORES
from grocery_planner.domain import Store
from grocery_planner.tools.price_lookup import DEFAULT_SEED_PATH, SeededFilePriceSource
from grocery_planner.tools.vocabulary import ingredient_vocabulary, same_food

RAW = json.loads(DEFAULT_SEED_PATH.read_text())
PRODUCTS = RAW["products"]
ORIGINAL_ROWS = 346
ORIGINAL_SHA256 = "5020d994dfa3def88221bef1aa2873726737bf2232d48fe7d73de1e5e926ec70"

NEW_NAMES = {
    "pork mince", "lamb mince", "chicken mince", "chicken wings", "scotch fillet", "beef strips",
    "pork belly", "chorizo", "barramundi", "smoked salmon", "pumpkin", "eggplant", "leek", "kale",
    "asparagus", "salad mix", "bok choy", "fresh chilli", "fresh basil", "mint", "rosemary",
    "thyme", "strawberries", "blueberries", "orange", "mango", "pear", "grapes", "ricotta",
    "halloumi", "goat cheese", "greek yogurt", "ghee", "tofu", "oat milk", "almond milk",
    "margarine", "passata", "couscous", "quinoa", "red lentils", "egg noodles", "rice noodles",
    "lasagne sheets", "pasta sauce", "pesto", "curry paste", "fish sauce", "oyster sauce",
    "balsamic vinegar", "white vinegar", "chilli flakes", "cinnamon", "baking powder",
    "brown sugar", "maple syrup", "tahini", "cannellini beans", "canned salmon", "pita", "naan",
    "frozen pizza", "puff pastry", "edamame", "frozen corn",
}  # fmt: skip

# Vocabulary names whose search also returns a product of another ingredient, because the other
# product's name or search terms contain the name as a whole word. The new ones cannot be named
# around ("Oat Milk" is milk-based, "Brown Sugar" is sugar); same_food rejects every pair.
# Left over from the first 103 ingredients: the 22 pairs from the original seed.
EXPECTED_COLLISIONS = {
    ("bread", "bread rolls"), ("bread", "breadcrumbs"), ("bread", "garlic bread"),
    ("bread", "sourdough"), ("bread", "wholemeal bread"), ("butter", "peanut butter"),
    ("cream", "coconut milk"), ("cream", "cream cheese"), ("cream", "ice cream"),
    ("cream", "milk"), ("cream", "sour cream"), ("flour", "tortillas"),
    ("garlic", "garlic bread"), ("milk", "coconut milk"), ("pepper", "capsicum"),
    ("potato", "hash browns"), ("potato", "potato gems"), ("potato", "sweet potato"),
    ("spinach", "frozen spinach"), ("tomato", "cherry tomatoes"), ("tomato", "tomato paste"),
    ("tomato", "tomato sauce"),
}  # fmt: skip
NEW_COLLISIONS = {
    ("milk", "almond milk"), ("milk", "oat milk"), ("rice", "rice noodles"),
    ("salmon", "canned salmon"), ("salmon", "smoked salmon"), ("sugar", "brown sugar"),
}  # fmt: skip


def slug(product_id: str) -> str:
    return product_id.split("-", 1)[1].rsplit("-", 1)[0]


def by_ingredient() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for p in PRODUCTS:
        out[slug(p["product_id"])].append(p)
    return out


NEW_ROWS = PRODUCTS[ORIGINAL_ROWS:]
NEW_SLUGS = {slug(p["product_id"]) for p in NEW_ROWS}


def test_original_rows_are_unchanged():
    original = PRODUCTS[:ORIGINAL_ROWS]
    assert len(original) == ORIGINAL_ROWS
    assert hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest() == (
        ORIGINAL_SHA256
    )
    assert not NEW_SLUGS & {slug(p["product_id"]) for p in original}  # no variant added to old


def test_vocabulary_has_65_more_entries():
    vocab = ingredient_vocabulary()
    assert len(NEW_SLUGS) == 65 and len(vocab) == 103 + 65
    assert NEW_NAMES <= vocab.keys() and len(NEW_NAMES) == 65
    assert {p["search_terms"][0] for p in NEW_ROWS} == NEW_NAMES
    assert not any(vocab[n].pantry_staple for n in NEW_NAMES)  # pantry_staples is unchanged


def test_every_new_ingredient_is_sold_at_both_stores():
    groups = by_ingredient()
    for s in NEW_SLUGS:
        stores = {p["store"] for p in groups[s]}
        assert stores == {x.value for x in ACTIVE_STORES}, s
        for store in stores:
            assert 1 <= sum(p["store"] == store for p in groups[s]) <= 3, (s, store)


def test_variants_match_across_stores_and_ids_follow_the_pattern():
    for s, rows in by_ingredient().items():
        if s not in NEW_SLUGS:
            continue
        for key in ("category", "pack_unit", "search_terms"):
            assert len({json.dumps(p[key]) for p in rows}) == 1, (s, key)
        for p in rows:
            prefix = "col" if p["store"] == "coles" else "wol"
            assert p["product_id"].startswith(f"{prefix}-{s}-") and p["product_id"][-2:].isdigit()
            assert p["data_source"] == "seeded" and p["price_age_days"] == 0
            assert isinstance(p["price"], str) and Decimal(p["price"]) > 0
        # the same variant numbers exist at both stores
        assert {(p["store"], p["product_id"][-2:]) for p in rows} == {
            (st, f"{n:02d}") for st in ("coles", "woolworths") for n in range(1, len(rows) // 2 + 1)
        }, s


def test_no_duplicate_product_ids():
    ids = [p["product_id"] for p in PRODUCTS]
    assert len(ids) == len(set(ids)) == ORIGINAL_ROWS + len(NEW_ROWS)


def test_every_new_special_has_a_higher_regular_price():
    specials = [p for p in NEW_ROWS if p["on_special"]]
    assert 6 <= len(specials) <= 8
    for p in specials:
        assert Decimal(p["regular_price"]) > Decimal(p["price"]), p["product_id"]
        assert p["special_weeks"] in (1, 2)
    assert all("regular_price" not in p and p["special_weeks"] is None
               for p in NEW_ROWS if not p["on_special"])  # fmt: skip


def test_store_prices_stay_close_item_by_item():
    pairs: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(dict)
    for p in NEW_ROWS:
        shelf = Decimal(p.get("regular_price") or p["price"])
        pairs[(slug(p["product_id"]), p["product_id"][-2:])][p["store"]] = shelf
    gaps = [abs(v["coles"] - v["woolworths"]) / min(v.values()) for v in pairs.values()]
    assert max(gaps) <= Decimal("0.09")  # about 0-8%; the cheapest items round to whole cents
    cheaper = [min(v, key=v.get) for v in pairs.values() if v["coles"] != v["woolworths"]]
    assert 0.35 < cheaper.count("coles") / len(cheaper) < 0.65  # neither store is cheaper overall


def test_new_vocabulary_names_pull_in_no_other_ingredient_beyond_the_listed_ones():
    source = SeededFilePriceSource()
    found = set()
    for name in ingredient_vocabulary():
        for store in ACTIVE_STORES:
            for p in source.search(name, store, 500):
                if p.search_terms[0] != name:
                    found.add((name, p.search_terms[0]))
    assert found == EXPECTED_COLLISIONS | NEW_COLLISIONS


def test_same_food_rejects_every_new_collision():
    ids = {p["search_terms"][0]: p["product_id"] for p in PRODUCTS}
    for generic, other in NEW_COLLISIONS:
        assert not same_food(generic, ids[other]), (generic, other)
        assert same_food(other, ids[other]) and same_food(generic, ids[generic])


def test_new_search_terms_belong_to_one_ingredient():
    owners: dict[str, set[str]] = defaultdict(set)
    for p in PRODUCTS:
        for t in p["search_terms"]:
            owners[t].add(slug(p["product_id"]))
    shared_originally = {"pasta", "bread"}  # penne/spaghetti, white/wholemeal bread
    assert {t for t, s in owners.items() if len(s) > 1} == shared_originally
    source = SeededFilePriceSource()
    for p in NEW_ROWS:
        for term in p["search_terms"][1:]:
            for store in (Store.COLES, Store.WOOLWORTHS):
                hit = {slug(x.product_id) for x in source.search(term, store, 500)}
                assert hit <= {slug(p["product_id"])}, (term, hit)
