# syntax=docker/dockerfile:1
# Built by Cloud Build (linux/amd64). Context is the repository root.

# ---- 1. the frontend -------------------------------------------------------------------------
FROM node:24-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 2. the API, with the built frontend -----------------------------------------------------
FROM python:3.12-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

# Same layout as the repository: the code finds db/ and frontend/dist relative to backend/.
WORKDIR /app/backend
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/grocery_planner ./grocery_planner
RUN uv sync --frozen --no-dev
COPY db /app/db
COPY --from=frontend /src/frontend/dist /app/frontend/dist

RUN useradd --system --uid 10001 --no-create-home app
USER app
ENV PATH="/app/backend/.venv/bin:$PATH" FRONTEND_DIST=/app/frontend/dist

# One worker on purpose: the run queue, the per-minute limiter and the sweep live in memory.
# --proxy-headers: Cloud Run's front end terminates TLS and sets X-Forwarded-*.
# Cloud Run sets $PORT (8080).
CMD ["sh", "-c", "exec uvicorn grocery_planner.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
