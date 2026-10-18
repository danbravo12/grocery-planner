"""Time catalogue searches against the real database, from wherever this runs.

    python -m grocery_planner.tools.bench_search [calls]     (default 50, after one warm-up)

It uses the same PostgresPriceSource the API uses, so the number is what a run's searches cost
from this host. Run it on Cloud Run (a one-off job) to see the deployed latency."""

import statistics
import sys
import time

from grocery_planner.db import db_url
from grocery_planner.domain.catalogue import Store
from grocery_planner.tools.price_lookup import PostgresPriceSource

TERMS = ["chicken breast", "beef mince", "eggs", "rice", "lamb chops", "pork chops", "milk"]


def main(calls: int) -> None:
    started = time.perf_counter()
    source = PostgresPriceSource(db_url())
    print(f"pool opened in {(time.perf_counter() - started) * 1000:.0f} ms")
    try:
        source.search(TERMS[0], Store.COLES, 5)  # warm-up
        times = []
        for i in range(calls):
            t = time.perf_counter()
            source.search(TERMS[i % len(TERMS)], list(Store)[i % 2], 5)
            times.append((time.perf_counter() - t) * 1000)
    finally:
        source.close()
    times.sort()
    p95 = times[min(len(times) - 1, round(0.95 * len(times)) - 1)]
    print(
        f"{calls} searches: p50 {statistics.median(times):.0f} ms, p95 {p95:.0f} ms, "
        f"max {times[-1]:.0f} ms"
    )


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 50)
