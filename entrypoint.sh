#!/usr/bin/env bash
set -euo pipefail

echo "Starting container entrypoint"

# Wait for dependent services (simple, configurable via env)
if [ -n "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL is set"
fi

# Run alembic migrations only when explicitly requested (opt-in), and fail fast.
# Migrations are normally applied by the dedicated one-off ECS task in CI, so
# every replica should NOT race `alembic upgrade head` on boot. A failed
# migration must abort the boot rather than serve traffic on a half-migrated schema.
if [ -f /app/alembic.ini ] && [ "${RUN_MIGRATIONS_ON_BOOT:-0}" = "1" ]; then
  echo "Running alembic migrations"
  alembic upgrade head
fi

echo "Starting Uvicorn"
if [ "${ENVIRONMENT:-production}" = "development" ] || [ "${UVICORN_RELOAD:-0}" = "1" ]; then
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
else
  # --proxy-headers + --forwarded-allow-ips "*" so the Cloudflare Tunnel's
  # X-Forwarded-For is trusted; without this every request presents the tunnel
  # origin IP and slowapi rate limiting becomes global instead of per-client.
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    --proxy-headers --forwarded-allow-ips "*"
fi
