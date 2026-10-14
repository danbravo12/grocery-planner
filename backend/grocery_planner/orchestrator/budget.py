from typing import Protocol

# Worst case requests for one run: ingredient 1, price 2 (matching plus its retry round),
# planner 3 (answer plus two tool rounds), reprice 1, second planner 2.
ESTIMATED_REQUESTS = 1 + 2 + 3 + 1 + 2


class BudgetGuard(Protocol):
    """Daily request budget. Step 9 fills it in; for now only the call order is fixed."""

    def reserve(self, run_id: str, estimated_requests: int) -> None: ...

    def record(self, run_id: str, model: str | None, requests: int) -> None: ...

    def release(self, run_id: str) -> None: ...


class NoopBudgetGuard:
    def reserve(self, run_id: str, estimated_requests: int) -> None:
        pass

    def record(self, run_id: str, model: str | None, requests: int) -> None:
        pass

    def release(self, run_id: str) -> None:
        pass
