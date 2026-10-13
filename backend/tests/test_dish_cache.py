from grocery_planner.domain.dish_cache import make_dish_cache_key


def test_normalises_dish_name():
    assert make_dish_cache_key("  Potato   SALAD ", None) == ("potato salad", "")


def test_no_avoid_is_empty_string():
    assert make_dish_cache_key("x", "")[1] == ""
    assert make_dish_cache_key("x", None)[1] == ""
    assert make_dish_cache_key("x", " , ,")[1] == ""


def test_avoid_is_split_sorted_and_rejoined():
    assert make_dish_cache_key("x", "Nuts,  DAIRY")[1] == "dairy,nuts"
    assert make_dish_cache_key("x", "dairy, nuts") == make_dish_cache_key("X", "nuts ,dairy")


def test_avoid_collapses_whitespace_and_duplicates():
    assert make_dish_cache_key("x", "tree   nuts, nuts, tree nuts")[1] == "nuts,tree nuts"


def test_different_avoid_gives_different_key():
    assert make_dish_cache_key("potato salad", "dairy") != make_dish_cache_key("potato salad", None)
