"""Live smoke tests against the real Gemini API. Run with:  uv run pytest -m live -s
Uses the free-tier quota (about 3 requests)."""

import pytest
from pydantic import BaseModel

from grocery_planner.env import getenv
from grocery_planner.llm import GeminiClient, ToolSpec, Usage

pytestmark = pytest.mark.live


class Capital(BaseModel):
    country: str
    capital: str


class CityIn(BaseModel):
    city: str


class Population(BaseModel):
    city: str
    population: int


def population(city: str) -> dict:
    return {"city": city, "population": 5_300_000}  # fictional, from our "database"


@pytest.fixture(scope="module")
def client():
    if not getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    seen: list[Usage] = []
    c = GeminiClient(on_request=seen.append)
    c.seen = seen
    return c


def test_structured_output(client):
    r = client.generate("Answer briefly.", "What is the capital of Australia?", Capital)
    print("\nstructured:", r.parsed, r.usage, f"{r.latency_ms}ms", "model", r.model)
    assert r.parsed.capital == "Canberra" and r.usage.requests == 1 and not r.repair_used


def test_one_round_tool_call(client):
    tool = ToolSpec("get_population", "Look up a city's population", CityIn, population)
    r = client.generate_with_tools(
        "Use the tool to look up numbers; never guess them.",
        "What is the population of Sydney?",
        Population,
        [tool],
        max_tool_rounds=2,
    )
    print("\ntool call:", r.parsed, r.usage, f"{r.latency_ms}ms", r.tool_calls)
    assert r.parsed.population == 5_300_000
    assert [t.name for t in r.tool_calls] == ["get_population"] and r.usage.requests == 2
