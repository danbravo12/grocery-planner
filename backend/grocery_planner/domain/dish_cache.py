import re


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def make_dish_cache_key(dish_name: str, avoid: str | None) -> tuple[str, str]:
    """Normalised (dish_key, avoid_key). avoid_key is '' when there is no avoid."""
    parts = {_norm(a) for a in (avoid or "").split(",")} - {""}
    return _norm(dish_name), ",".join(sorted(parts))
