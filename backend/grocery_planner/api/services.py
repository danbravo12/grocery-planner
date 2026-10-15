"""The real services, built from the environment: Postgres (or memory for local development),
the Gemini client, the budget and the tracer. Reuses the CLI's wiring so the API and the CLI
cannot drift apart."""

from types import SimpleNamespace

from grocery_planner.api.app import Services
from grocery_planner.api.settings import ApiSettings
from grocery_planner.cli import build_runner
from grocery_planner.tools.price_lookup import today_sydney


def build_services(settings: ApiSettings) -> Services:
    args = SimpleNamespace(
        memory=settings.use_memory, seed=False, ip_secret=settings.ip_hash_secret
    )
    runner = build_runner(args, today_sydney())
    return Services(runner=runner, budget=runner.budget)
