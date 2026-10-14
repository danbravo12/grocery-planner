from typing import Protocol

from psycopg.types.json import Jsonb

from grocery_planner.domain.ingredients import DishResult


class DishCacheRepo(Protocol):
    def get(self, dish_key: str, avoid_key: str) -> DishResult | None: ...

    def put(
        self, dish_key: str, avoid_key: str, dish_name: str, result: DishResult, model: str
    ) -> None: ...


class InMemoryDishCache:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], DishResult] = {}

    def get(self, dish_key: str, avoid_key: str) -> DishResult | None:
        return self.rows.get((dish_key, avoid_key))

    def put(
        self, dish_key: str, avoid_key: str, dish_name: str, result: DishResult, model: str
    ) -> None:
        self.rows[(dish_key, avoid_key)] = result


class PostgresDishCache:
    """dish_cache table. Values are stored as JSON; Decimals round-trip as strings."""

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 2) -> None:
        from psycopg_pool import ConnectionPool

        from grocery_planner.db import CONNECT_KWARGS

        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, kwargs=CONNECT_KWARGS, open=True
        )
        self._pool.wait()

    def close(self) -> None:
        self._pool.close()

    def get(self, dish_key: str, avoid_key: str) -> DishResult | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT ingredients FROM dish_cache WHERE dish_key = %s AND avoid_key = %s",
                (dish_key, avoid_key),
            ).fetchone()
        return DishResult.model_validate(row["ingredients"]) if row else None

    def put(
        self, dish_key: str, avoid_key: str, dish_name: str, result: DishResult, model: str
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO dish_cache (dish_key, avoid_key, dish_name, ingredients, model) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (dish_key, avoid_key) DO UPDATE SET dish_name = EXCLUDED.dish_name, "
                "ingredients = EXCLUDED.ingredients, model = EXCLUDED.model, created_at = now()",
                (dish_key, avoid_key, dish_name, Jsonb(result.model_dump(mode="json")), model),
            )
