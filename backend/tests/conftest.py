import pytest

from grocery_planner.db import db_url


def pytest_collection_modifyitems(config, items):
    try:
        db_url()
        have_db = True
    except RuntimeError:
        have_db = False
    markexpr = config.getoption("-m") or ""
    live_requested = "live" in markexpr and "not live" not in markexpr
    skip_db = pytest.mark.skip(reason="SUPABASE_DB_URL not set")
    skip_live = pytest.mark.skip(reason="live tests only run with -m live")
    for item in items:
        if "integration" in item.keywords and not have_db:
            item.add_marker(skip_db)
        if "live" in item.keywords and not live_requested:
            item.add_marker(skip_live)
