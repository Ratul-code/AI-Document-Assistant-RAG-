#!/usr/bin/env bash
# wipe_data.sh — **SQLite-only**. Delete all rows from every table in
# the dev SQLite DB, but keep the schema (tables, columns, indexes, FK
# constraints) intact.
#
# Usage:
#   ./scripts/wipe_data.sh                # wipe app.db (default)
#   ./scripts/wipe_data.sh auth_smoke.db  # wipe a different DB file
#
# For Postgres (Supabase) use the sibling script `wipe_data_pg.sh`,
# which uses `psql` + `TRUNCATE ... CASCADE`:
#
#   DATABASE_URL_SYNC=postgresql+psycopg://... ./scripts/wipe_data_pg.sh
#
# After this:
#   - All tables exist but are empty
#   - Restart the backend so it picks up the empty DB cleanly
#   - Existing user sessions/tokens become invalid (users are gone)

set -euo pipefail

# --- Locate the project root (parent of the scripts/ dir) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

DB_FILE="${1:-app.db}"

if [[ ! -f "$DB_FILE" ]]; then
  echo "Error: database file '$DB_FILE' not found in $PROJECT_ROOT" >&2
  echo "Available .db files:" >&2
  find . -maxdepth 2 -name "*.db" -not -path "*/.venv/*" 2>/dev/null >&2 || true
  exit 1
fi

# --- Sanity check: sqlite3 must be available ---
if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "Error: sqlite3 CLI not found. Install with: brew install sqlite" >&2
  exit 1
fi

# --- Refuse to touch anything inside .venv/ ---
case "$DB_FILE" in
  */.venv/*|.venv/*)
    echo "Error: refusing to wipe a DB inside .venv/" >&2
    exit 1
    ;;
esac

echo "About to wipe all rows from: $PROJECT_ROOT/$DB_FILE"
echo "Schema will be preserved (tables, columns, indexes stay)."
echo

# --- Optional: timestamped backup ---
# Backups go into a sibling .local/ directory (gitignored) instead
# of next to the source DB. The old behaviour dropped `app.db.backup-YYYYMMDD-HHMMSS`
# files into the project root — they accumulated over months and
# polluted the source tree. `.local/` is created on demand and never
# committed.
BACKUP_DIR="$PROJECT_ROOT/.local/backups"
mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/$(basename "$DB_FILE").backup-$(date +%Y%m%d-%H%M%S)"
cp "$DB_FILE" "$BACKUP"
echo "Backup saved to: $BACKUP"
echo

# --- The actual wipe ---
# List every table (sqlite_master excludes internal sqlite_* tables).
# Then DELETE each one inside a single transaction with FK checks off.
TABLES=$(sqlite3 "$DB_FILE" "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'alembic_%' ORDER BY name;")

if [[ -z "$TABLES" ]]; then
  echo "No tables found (other than sqlite_* / alembic_*). Nothing to wipe."
  exit 0
fi

echo "Tables to clear:"
echo "$TABLES" | sed 's/^/  - /'
echo

DELETE_SQL=""
while IFS= read -r table; do
  DELETE_SQL+="DELETE FROM ${table};
"
done <<< "$TABLES"

sqlite3 "$DB_FILE" <<SQL
PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;
${DELETE_SQL}
COMMIT;
PRAGMA foreign_keys = ON;
SQL

# --- Verify ---
echo "Row counts after wipe:"
while IFS= read -r table; do
  COUNT=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM ${table};")
  printf "  %-30s %s\n" "$table" "$COUNT"
done <<< "$TABLES"

echo
echo "Tables still present:"
sqlite3 "$DB_FILE" ".tables" | tr ' ' '\n' | grep -v '^$' | sed 's/^/  - /'

echo
echo "Done. Restart your backend to pick up the clean DB."
echo "  cd $PROJECT_ROOT"
echo "  uvicorn app.main:app --reload"