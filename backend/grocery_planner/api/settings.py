import os
from dataclasses import dataclass, field

from grocery_planner.env import getenv


class ConfigError(RuntimeError):
    """The environment is not fit to start the API (it refuses to start)."""


def _flag(name: str) -> bool:
    return (getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ApiSettings:
    app_env: str = "dev"  # dev | eval | prod
    frontend_origins: tuple[str, ...] = ()  # the only origins CORS allows
    trusted_proxy: bool = False  # read the client IP from X-Forwarded-For
    sample_run_id: str | None = None
    ip_hash_secret: str | None = None
    use_memory: bool = False  # in-process store and seed file instead of Postgres (local dev)
    max_workers: int = 2
    max_queued: int = 10
    max_body_bytes: int = 32 * 1024
    heartbeat_seconds: float = 15.0
    poll_seconds: float = 0.5
    sweep_interval_seconds: float = 300.0
    prune_interval_seconds: float = 86400.0
    prune_keep_days: int = 7
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def docs_enabled(self) -> bool:
        return self.app_env == "dev"

    def validate(self) -> None:
        """Refuse to start in prod without what prod needs."""
        if self.is_prod:
            missing = [
                name
                for name, value in (
                    ("IP_HASH_SECRET", self.ip_hash_secret),
                    ("SAMPLE_RUN_ID", self.sample_run_id),
                )
                if not value
            ]
            if missing:
                raise ConfigError(
                    f"APP_ENV=prod requires {' and '.join(missing)} to be set; refusing to start"
                )

    @classmethod
    def from_env(cls) -> "ApiSettings":
        env = (getenv("APP_ENV") or "dev").strip().lower()
        origins = tuple(
            o.strip().rstrip("/")
            for o in (getenv("FRONTEND_ORIGINS") or "").split(",")
            if o.strip()
        )
        settings = cls(
            app_env=env,
            frontend_origins=origins,
            trusted_proxy=_flag("TRUSTED_PROXY"),
            sample_run_id=(getenv("SAMPLE_RUN_ID") or "").strip() or None,
            ip_hash_secret=(getenv("IP_HASH_SECRET") or "").strip() or None,
            use_memory=(os.environ.get("API_STORE") or getenv("API_STORE") or "").lower()
            == "memory",
        )
        settings.validate()
        return settings
