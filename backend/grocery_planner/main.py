"""ASGI entry point.

    uv run uvicorn grocery_planner.main:app            (real services from the environment)
    API_STORE=memory uv run uvicorn grocery_planner.main:app   (no database, local development)

`app` is built on first access, so importing this module (tests, tooling) connects to nothing.
The API itself is `grocery_planner.api.create_app`."""

from typing import Any

from grocery_planner.api import ApiSettings, create_app

_app: Any = None


def build_app() -> Any:
    from grocery_planner.api.services import build_services

    settings = ApiSettings.from_env()  # refuses to start in prod without its secrets
    return create_app(settings, build_services(settings))


def __getattr__(name: str) -> Any:
    global _app
    if name == "app":
        if _app is None:
            _app = build_app()
        return _app
    raise AttributeError(name)
