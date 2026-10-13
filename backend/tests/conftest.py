import pytest

from grocery_planner.db import db_url


def pytest_collection_modifyitems(config, items):
    try:
        db_url()
        have_db = True
    except RuntimeError:
        have_db = False
    if have_db:
        return
    skip = pytest.mark.skip(reason="SUPABASE_DB_URL not set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
