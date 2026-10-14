"""What a request would cost on Google's PAID tier. The free tier costs $0, so this number is
only a "what would this cost if it were paid" figure for the traces and the run record."""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from grocery_planner.llm.types import Usage

log = logging.getLogger(__name__)

# USD per 1M tokens, standard paid tier, text input. Output includes thinking tokens.
# Source: https://ai.google.dev/gemini-api/docs/pricing (the page said "Last updated 2026-10-13").
PRICES_CHECKED_ON = date(2026, 10, 14)
PAID_TIER_PRICES: dict[str, tuple[Decimal, Decimal]] = {  # model -> (input, output)
    "gemini-3.1-flash-lite": (Decimal("0.25"), Decimal("1.50")),
    "gemini-3.5-flash-lite": (Decimal("0.30"), Decimal("2.50")),
}
_warned: set[str] = set()


@dataclass(frozen=True)
class Cost:
    input: Decimal
    output: Decimal  # includes thinking tokens

    @property
    def total(self) -> Decimal:
        return self.input + self.output


def would_cost(model: str | None, usage: Usage) -> Cost | None:
    """Hypothetical paid-tier cost of one request's tokens, or None for an unpriced model."""
    prices = PAID_TIER_PRICES.get(model or "")
    if prices is None:
        if model and model not in _warned:
            _warned.add(model)
            log.warning("no paid-tier price known for model %s; cost left out", model)
        return None
    per_million = Decimal(1_000_000)
    return Cost(
        input=Decimal(usage.input_tokens) * prices[0] / per_million,
        output=Decimal(usage.output_tokens + usage.thinking_tokens) * prices[1] / per_million,
    )
