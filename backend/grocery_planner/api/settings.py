import os
from dataclasses import dataclass, field
from pathlib import Path

from grocery_planner.env import getenv


class ConfigError(RuntimeError):
    """The environment is not fit to start the API (it refuses to start)."""


REPO_ROOT = Path(__file__).resolve().parents[3]


def _dist() -> Path | None:
    """FRONTEND_DIST, else frontend/dist in the repo when it has been built."""
    chosen = (getenv("FRONTEND_DIST") or "").strip()
    path = Path(chosen) if chosen else REPO_ROOT / "frontend" / "dist"
    return path if (path / "index.html").is_file() else None


def _flag(name: str) -> bool:
    return (getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ApiSettings:
    app_env: str = "dev"  # dev | eval | prod
    frontend_origins: tuple[str, ...] = ()  # the only origins CORS allows
    trusted_proxy: bool = False  # read the client IP from X-Forwarded-For
    sample_run_id: str | None = None
    ip_hash_secret: str | None = None
    visitor_hash_secret: str | None = None  # HMAC key of the usage counter; keep it stable
    owner_token: str | None = None  # what /internal/owner needs to mark a browser as the owner's
    daily_audience: str | None = None  # the service URL the scheduler's token is addressed to
    daily_sa_email: str | None = None  # the only service account allowed to call /internal/daily
    frontend_dist: Path | None = None  # the built frontend, served from the same origin
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
                    ("VISITOR_HASH_SECRET", self.visitor_hash_secret),
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
            visitor_hash_secret=(getenv("VISITOR_HASH_SECRET") or "").strip() or None,
            owner_token=(getenv("OWNER_TOKEN") or "").strip() or None,
            daily_audience=(getenv("DAILY_JOB_AUDIENCE") or "").strip().rstrip("/") or None,
            daily_sa_email=(getenv("DAILY_JOB_SA_EMAIL") or "").strip() or None,
            frontend_dist=_dist(),
            use_memory=(os.environ.get("API_STORE") or getenv("API_STORE") or "").lower()
            == "memory",
        )
        settings.validate()
        return settings
