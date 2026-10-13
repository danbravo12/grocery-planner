import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]


def getenv(name: str, default: str | None = None) -> str | None:
    """Env var, falling back to backend/.env (existing variables win). Never log values."""
    load_dotenv(BACKEND_DIR / ".env")
    return os.environ.get(name, default)
