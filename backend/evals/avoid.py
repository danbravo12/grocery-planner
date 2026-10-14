"""What "avoid X" forbids, as deterministic word lists (no judgement involved).

An ingredient name violates an avoid when, after removing the allowed phrases (coconut milk is
not dairy, peanut butter is not butter), it still contains a forbidden word.
"""

import re

AVOIDS: dict[str, dict[str, list[str]]] = {
    "dairy": {
        "words": [
            "milk",
            "cream",
            "butter",
            "buttermilk",
            "cheese",
            "cheddar",
            "mozzarella",
            "parmesan",
            "feta",
            "ricotta",
            "yoghurt",
            "yogurt",
            "ghee",
            "custard",
            "whey",
            "ice cream",
        ],  # fmt: skip
        "allowed": [
            "coconut milk",
            "coconut cream",
            "peanut butter",
            "almond milk",
            "oat milk",
            "soy milk",
            "soya milk",
            "cocoa butter",
            "dairy-free",
            "dairy free",
            "plant-based",
            "butternut",
        ],  # fmt: skip
    },
    "nuts": {
        "words": [
            "nut",
            "nuts",
            "peanut",
            "peanuts",
            "almond",
            "almonds",
            "cashew",
            "cashews",
            "walnut",
            "walnuts",
            "pecan",
            "pecans",
            "pistachio",
            "pistachios",
            "hazelnut",
            "hazelnuts",
            "macadamia",
            "macadamias",
            "pine nut",
            "pine nuts",
            "satay",
        ],  # fmt: skip
        "allowed": [
            "coconut",
            "nutmeg",
            "butternut",
        ],
    },
    "gluten": {
        "words": [
            "wheat",
            "flour",
            "bread",
            "breadcrumbs",
            "pasta",
            "spaghetti",
            "penne",
            "noodle",
            "noodles",
            "couscous",
            "barley",
            "rye",
            "semolina",
            "lasagne",
            "pastry",
            "rolls",
            "buns",
            "sourdough",
            "tortillas",
            "tortilla",
            "soy sauce",
            "nuggets",
            "fish fingers",
            "garlic bread",
        ],  # fmt: skip
        "allowed": ["rice noodles", "rice flour", "corn flour"],
    },
}


FREE_MARKERS = {"dairy": ("dairy-free", "dairy free"), "nuts": (),
                "gluten": ("gluten-free", "gluten free")}  # fmt: skip


def violates(avoid: str, ingredient_name: str) -> bool:
    spec = AVOIDS[avoid]
    name = ingredient_name.lower()
    if any(marker in name for marker in FREE_MARKERS[avoid]):
        return False  # "gluten-free pasta" is the substitution we want
    for phrase in spec["allowed"]:
        name = name.replace(phrase, " ")
    return any(re.search(rf"\b{re.escape(w)}\b", name) for w in spec["words"])


def avoids_in(text: str | None) -> list[str]:
    """Which known avoids a request's free-text avoid field mentions."""
    t = (text or "").lower()
    return [a for a in AVOIDS if a in t]
