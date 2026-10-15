"""Re-read the saved price fixtures at a new anchor date, with no model call:

uv run python -m tests.fixtures.regenerate 2026-10-14
"""

import sys
from datetime import date

from tests.fixtures import load_price_fixture, reprice_fixture, save_price_fixture

if __name__ == "__main__":
    target = date.fromisoformat(sys.argv[1])
    for name in "abc":
        print("rewrote", save_price_fixture(reprice_fixture(load_price_fixture(name), target)).name)
