from dataclasses import dataclass
from decimal import Decimal

from grocery_planner.domain import Store

# Stores the app compares. Code must use this, never iterate the Store enum: the enum may
# list stores that have no seed data yet (currently Aldi).
ACTIVE_STORES: tuple[Store, ...] = (Store.COLES, Store.WOOLWORTHS)


@dataclass(frozen=True)
class GuardrailConfig:
    """Limits that protect the free Gemini quota and a single visitor's share of it."""

    # Requests per model per Pacific day (Google resets at midnight Pacific).
    daily_limit: int = 350  # 70% of the free tier's 500 requests per day
    admission_limit: int = 298  # daily_limit minus a 15% buffer (52): new runs stop here
    requests_per_minute: int = 12  # per model, one process
    rate_limit_max_wait_seconds: float = 30.0
    visitor_runs_per_day: int = 5
    ip_runs_per_day: int = 20
    max_dishes: int = 20  # mains plus sides across all meals in one request

    @classmethod
    def from_free_tier(
        cls,
        requests_per_day: int = 500,
        daily_fraction: Decimal = Decimal("0.70"),
        buffer_fraction: Decimal = Decimal("0.15"),
        **kw,
    ) -> "GuardrailConfig":
        daily = int(requests_per_day * daily_fraction)
        return cls(daily_limit=daily, admission_limit=daily - int(daily * buffer_fraction), **kw)


GUARDRAILS = GuardrailConfig()
assert GUARDRAILS == GuardrailConfig.from_free_tier()  # the defaults above are 70% and 15%
