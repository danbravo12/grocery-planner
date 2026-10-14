"""Eval case files: evals/cases/*.yaml, each a list of cases."""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

CASES_DIR = Path(__file__).parent / "cases"
Agent = Literal["ingredient", "price", "planner", "e2e"]
AGENTS: tuple[Agent, ...] = ("ingredient", "price", "planner", "e2e")


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    agent: Agent
    input: dict[str, Any]
    checks: dict[str, Any]
    notes: str  # why this case exists


def load_cases(directory: Path = CASES_DIR) -> list[Case]:
    cases: list[Case] = []
    for path in sorted(directory.glob("*.yaml")):
        for raw in yaml.safe_load(path.read_text()) or []:
            cases.append(Case.model_validate(raw))
    ids = [c.id for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate case ids: {dupes}")
    return cases
