#!/usr/bin/env bash
# =============================================================================
# Container entrypoint. Used by api, worker, and beat.
#
# Responsibilities:
#   1. Wait until Postgres is reachable (with a hard timeout) so we don't
#      race against the db container on first boot.
#   2. Run `alembic upgrade head` so the schema is always current before
#      the process starts handling traffic. Idempotent — safe to re-run.
#   3. exec the original CMD so the real process becomes PID 1's child and
#      receives signals properly (SIGTERM from `docker stop`, etc.).
#
# We keep the logic intentionally simple — this script is the only place
# a contributor needs to read to understand container startup.
# =============================================================================

set -euo pipefail

# Resolve where we are. /app is the WORKDIR set in the Dockerfile.
cd /app

echo "[entrypoint] container starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[entrypoint] APP_ENV=${APP_ENV:-dev}"

# -----------------------------------------------------------------------------
# 1. Wait for Postgres (only if a Postgres URL is configured).
# -----------------------------------------------------------------------------
# In dev with SQLite, there's no network hop so we skip. In prod / docker
# with Postgres, we don't want to crash-loop on startup just because the
# db container isn't accepting connections yet.
DB_URL="${DATABASE_URL:-}"
DB_SYNC_URL="${DATABASE_URL_SYNC:-}"

run_alembic() {
    local url="$1"
    if [ -z "$url" ]; then
        echo "[entrypoint] no DATABASE_URL set; skipping alembic."
        return 0
    fi
    # Only wait for Postgres URLs. SQLite / aiosqlite have no host to ping.
    case "$url" in
        postgresql*|postgres*)
            echo "[entrypoint] waiting for postgres at $url ..."
            # Extract host:port with python (avoids sed/regex portability).
            python - <<'PY'
import os, re, socket, sys, time
url = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL") or ""
m = re.search(r"@([^:/]+):(\d+)", url)
if not m:
    print("[entrypoint] no host:port in DB url, skipping wait")
    sys.exit(0)
host, port = m.group(1), int(m.group(2))
deadline = time.time() + 60
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"[entrypoint] postgres reachable at {host}:{port}")
            sys.exit(0)
    except OSError:
        time.sleep(2)
print(f"[entrypoint] ERROR: postgres not reachable at {host}:{port} within 60s", file=sys.stderr)
sys.exit(1)
PY
            ;;
        sqlite*)
            echo "[entrypoint] SQLite URL detected; no network wait needed."
            ;;
        *)
            echo "[entrypoint] unknown DB scheme in $url; skipping wait."
            ;;
    esac
}

run_alembic "$DB_SYNC_URL"

# -----------------------------------------------------------------------------
# 2. Run migrations.
# -----------------------------------------------------------------------------
# We use the sync URL because alembic runs synchronously. The api/worker/beat
# all share the same DB schema, so any of them running migrations is fine.
if [ -n "$DB_SYNC_URL" ]; then
    echo "[entrypoint] running alembic upgrade head ..."
    # `cd backend` so alembic finds alembic.ini and the versions/ dir.
    (cd backend && alembic upgrade head)
    echo "[entrypoint] alembic done."
elif [ -n "$DB_URL" ]; then
    # Fallback: try with the async URL (alembic can read async URLs via psycopg).
    echo "[entrypoint] running alembic upgrade head with DATABASE_URL ..."
    (cd backend && alembic upgrade head)
    echo "[entrypoint] alembic done."
else
    echo "[entrypoint] no DB URL configured; assuming schema is managed elsewhere."
fi

# -----------------------------------------------------------------------------
# 3. Translate the simple CMD keyword into the real command line.
# -----------------------------------------------------------------------------
# We accept short names from docker-compose so the compose file stays
# readable: `command: ["api"]`, `command: ["worker"]`, `command: ["beat"]`.
# Expand them here, then exec the result.
cmd="${1:-api}"
shift || true

case "$cmd" in
    api)
        echo "[entrypoint] starting uvicorn (api) ..."
        # cd into backend so `app.main:app` resolves.
        cd /app/backend
        exec uvicorn app.main:app \
            --host 0.0.0.0 \
            --port 8000 \
            --proxy-headers \
            --forwarded-allow-ips='*' \
            "$@"
        ;;
    worker)
        echo "[entrypoint] starting celery worker ..."
        cd /app/backend
        exec celery -A app.core.celery_app worker \
            --loglevel="${CELERY_LOG_LEVEL:-info}" \
            --concurrency="${CELERY_WORKER_CONCURRENCY:-2}" \
            "$@"
        ;;
    beat)
        echo "[entrypoint] starting celery beat ..."
        cd /app/backend
        # Beat stores its last-run schedule in a file; persist it on a
        # shared location so restarts don't double-fire scheduled tasks.
        exec celery -A app.core.celery_app beat \
            --loglevel="${CELERY_LOG_LEVEL:-info}" \
            --schedule=/tmp/celerybeat-schedule \
            "$@"
        ;;
    *)
        # Unknown command: just run it. Useful for one-off `docker compose run`.
        echo "[entrypoint] running custom command: $cmd $*"
        exec "$cmd" "$@"
        ;;
esac
