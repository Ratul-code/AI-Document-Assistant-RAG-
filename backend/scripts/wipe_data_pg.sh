#!/usr/bin/env bash
# wipe_data_pg.sh — delete all rows from every table in the dev/prod
# Postgres database, but keep the schema (tables, columns, indexes, FK
# constraints) intact.
#
# Usage:
#   DATABASE_URL_SYNC=postgresql+psycopg://... ./scripts/wipe_data_pg.sh
#   # or, if `psql` is on PATH and can read DATABASE_URL_SYNC:
#   ./scripts/wipe_data_pg.sh
#
# The script reads the URL from `$DATABASE_URL_SYNC` (the same env var
# the sync engine + Celery use) and rewrites it to the `psql` form
# (`postgresql://...` without the SQLAlchemy `+psycopg` driver tag).
#
# After this:
#   - All tables exist but are empty
#   - Existing user sessions/tokens become invalid (users are gone)
#   - Restart the backend so it picks up the empty DB cleanly
#
# Required tools: `psql` (Postgres client). Install via:
#   macOS:  brew install libpq && brew link --force libpq
#   Linux:  apt-get install postgresql-client
#
# WARNING: This is destructive and will wipe the database the URL points
# at. ALWAYS confirm the URL is the dev/staging one — never run this
# against production without thinking twice.

set -euo pipefail

# --- Locate the project root (parent of the scripts/ dir) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# --- Sanity check: psql must be available ---
if ! command -v psql >/dev/null 2>&1; then
  echo "Error: psql CLI not found. Install with: brew install libpq" >&2
  exit 1
fi

if [[ -z "${DATABASE_URL_SYNC:-}" ]]; then
  echo "Error: DATABASE_URL_SYNC is not set." >&2
  echo "Set it to your Postgres URL, e.g.:" >&2
  echo "  export DATABASE_URL_SYNC='postgresql://postgres:password@host:5432/postgres'" >&2
  exit 1
fi

# Strip the SQLAlchemy driver tag (`+psycopg`) so psql can consume the
# URL directly. Any other `+<driver>` suffix is also stripped.
PSQL_URL="${DATABASE_URL_SYNC//+psycopg/}"

echo "About to wipe all rows from: $PSQL_URL"
echo "Schema will be preserved (tables, columns, indexes, FK constraints stay)."
echo

# --- Optional: timestamped backup via pg_dump (no-op if `pg_dump` absent) ---
if command -v pg_dump >/dev/null 2>&1; then
  BACKUP_DIR="$PROJECT_ROOT/.local/backups"
  mkdir -p "$BACKUP_DIR"
  BACKUP="$BACKUP_DIR/pg.backup-$(date +%Y%m%d-%H%M%S).sql"
  echo "Dumping schema+data to: $BACKUP"
  pg_dump "$PSQL_URL" > "$BACKUP"
  echo
else
  echo "Skipping backup (pg_dump not installed)."
  echo
fi

# --- The actual wipe ---
# Pull every user table (excluding `alembic_version`) in one shot via
# the information schema, then TRUNCATE them all in a single statement
# with `RESTART IDENTITY CASCADE`. The `CASCADE` is critical because
# the tables reference each other via FKs — without it the TRUNCATE
# would fail on the first referenced table.
SQL='
DO $$
DECLARE
  tables text;
BEGIN
  SELECT string_agg(format(''%I'', table_name), '', '')
    INTO tables
    FROM information_schema.tables
   WHERE table_schema = ''public''
     AND table_name <> ''alembic_version'';
  IF tables IS NOT NULL THEN
    EXECUTE ''TRUNCATE TABLE '' || tables || '' RESTART IDENTITY CASCADE;'';
  END IF;
END $$;
'

echo "Truncating all public tables (alembic_version preserved)…"
psql "$PSQL_URL" -v ON_ERROR_STOP=1 -c "$SQL"

# --- Verify ---
echo
echo "Row counts after wipe:"
psql "$PSQL_URL" -c "
  SELECT table_name, 0 AS row_count
    FROM information_schema.tables
   WHERE table_schema = 'public'
     AND table_name <> 'alembic_version'
   ORDER BY table_name;
"

echo
echo "Done. Restart your backend to pick up the clean DB."
echo "  cd $PROJECT_ROOT"
echo "  uvicorn app.main:app --reload"
