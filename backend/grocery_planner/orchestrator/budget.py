from typing import Literal, Protocol

# Worst case requests for one run: ingredient 1, price 2 (matching plus its retry round),
# planner 3 (answer plus two tool rounds), reprice 1, second planner 2.
ESTIMATED_REQUESTS = 1 + 2 + 3 + 1 + 2


class BudgetUnavailable(Exception):
    """A run could not be admitted. day_budget: today's admission limit is used up (the day is
    stopped). busy: only other runs' reservations are in the way right now."""

    def __init__(self, reason: Literal["day_budget", "busy"]) -> None:
        super().__init__(reason)
        self.reason = reason


class BudgetGuard(Protocol):
    """The daily request budget as the orchestrator and the model client see it."""

    def reserve(self, run_id: str, estimated_requests: int) -> None:
        """Reserve a run's worst case before it starts. Raises BudgetUnavailable."""
        ...

    def record(self, run_id: str, model: str | None, requests: int) -> None: ...

    def release(self, run_id: str) -> None:
        """Return whatever the run did not use. Safe to call twice."""
        ...

    def before_request(self, model: str) -> None:
        """Called before every send. Raises BudgetExhausted when the model is used up."""
        ...

    def fallback_allowed(self) -> bool: ...

    def quota_exhausted(self, model: str, kind: str) -> None:
        """Google said this model is out of quota (per_day) or has no credit (billing) today."""
        ...


class NoopBudgetGuard:
    def reserve(self, run_id: str, estimated_requests: int) -> None:
        pass

    def record(self, run_id: str, model: str | None, requests: int) -> None:
        pass

    def release(self, run_id: str) -> None:
        pass

    def before_request(self, model: str) -> None:
        pass

    def fallback_allowed(self) -> bool:
        return True

    def quota_exhausted(self, model: str, kind: str) -> None:
        pass
