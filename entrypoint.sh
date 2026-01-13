#!/usr/bin/env bash
set -euo pipefail

echo "Starting container entrypoint"

# Wait for dependent services (simple, configurable via env)
if [ -n "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL is set"
fi

# Run alembic migrations if present
if [ -f /app/alembic.ini ]; then
  echo "Running alembic migrations"
  alembic upgrade head || echo "alembic upgrade failed; continuing"
fi

echo "Starting Uvicorn"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
