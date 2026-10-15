"""Known avoid categories and the vocabulary items each one forbids.

A code guard behind the prompt: the model is told to respect each dish's avoid text, and if its
output still contains a forbidden vocabulary item the ingredient agent sends it back for the one
repair attempt. Only avoid text that mentions a known category is checked; anything else relies
on the model alone.
"""

import logging
import re

from grocery_planner.tools.vocabulary import VocabEntry

log = logging.getLogger(__name__)

# category -> words in the avoid text that select it (matched as whole words, lower case)
TRIGGERS: dict[str, tuple[str, ...]] = {
    "dairy": ("dairy", "lactose", "milk"),
    "gluten": ("gluten", "coeliac", "celiac", "wheat"),
    "nuts": ("nut", "nuts"),
    "peanuts": ("peanut", "peanuts"),
    "eggs": ("egg", "eggs"),
    "shellfish": ("shellfish", "seafood", "prawn", "prawns"),
    "fish": ("fish", "seafood"),
    "pork": ("pork",),
    "beef": ("beef",),
    "meat": ("meat", "vegetarian", "vegan"),
    "soy": ("soy", "soya"),
}

# category -> forbidden vocabulary names (exact catalogue names)
FORBIDDEN: dict[str, frozenset[str]] = {
    "dairy": frozenset(
        {
            "butter",
            "cheddar",
            "cream",
            "cream cheese",
            "feta",
            "ice cream",
            "milk",
            "mozzarella",
            "parmesan",
            "sour cream",
            "yoghurt",
        }
    ),  # fmt: skip
    "gluten": frozenset(
        {
            "bread",
            "bread rolls",
            "breadcrumbs",
            "burger buns",
            "chicken nuggets",
            "fish fingers",
            "flour",
            "garlic bread",
            "penne",
            "sourdough",
            "soy sauce",
            "spaghetti",
            "tortillas",
            "wholemeal bread",
        }
    ),  # fmt: skip
    "nuts": frozenset({"peanut butter"}),
    "peanuts": frozenset({"peanut butter"}),
    "eggs": frozenset({"eggs", "mayonnaise"}),
    "shellfish": frozenset({"prawns"}),
    "fish": frozenset({"fish fingers", "salmon", "tuna", "white fish"}),
    "pork": frozenset({"bacon", "ham", "pork chops", "sausages"}),
    "beef": frozenset(
        {
            "beef eye fillet",
            "beef mince",
            "beef stock",
            "diced beef",
            "rump steak",
        }
    ),  # fmt: skip
    "soy": frozenset({"soy sauce"}),
}
FORBIDDEN["meat"] = (
    FORBIDDEN["pork"]
    | FORBIDDEN["beef"]
    | FORBIDDEN["fish"]
    | FORBIDDEN["shellfish"]
    | frozenset(
        {
            "chicken breast",
            "chicken drumsticks",
            "chicken nuggets",
            "chicken stock",
            "chicken thigh",
            "lamb chops",
            "whole chicken",
        }
    )  # fmt: skip
)


def categories_in(avoid: str | None) -> list[str]:
    """The known categories an avoid text mentions, in table order."""
    text = (avoid or "").lower()
    words = set(re.findall(r"[a-z]+", text))
    return [c for c, triggers in TRIGGERS.items() if words & set(triggers)]


def avoid_violations(
    avoid: str | None, ingredient_names: list[str], vocab: dict[str, VocabEntry]
) -> list[str]:
    """One message per ingredient name that a known avoid category forbids. Unknown avoid text
    is not checked (and logged, since only the model enforces it)."""
    if not avoid or not avoid.strip():
        return []
    cats = categories_in(avoid)
    if not cats:
        log.info("avoid text %r matches no known category; relying on the model", avoid)
        return []
    known = {n.lower() for n in vocab}
    problems = []
    for name in ingredient_names:
        key = name.strip().lower()
        if key not in known:
            continue  # the table only names vocabulary items
        for cat in cats:
            if key in FORBIDDEN[cat]:
                problems.append(
                    f"'{name}' breaks the avoid text '{avoid}' ({cat}); "
                    "leave it out and substitute something suitable"
                )
                break
    return problems
