#!/usr/bin/env bash
set -Eeuo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BASE_DIR"

if [[ -f "$BASE_DIR/.env" ]]; then
  set -a
  source "$BASE_DIR/.env"
  set +a
fi

if [[ -z "$ADMIN_USERNAME" || -z "$ADMIN_PASSWORD" ]]; then
  echo "ADMIN_USERNAME and ADMIN_PASSWORD are required in .env"
  exit 1
fi

read -r -p "This will ERASE instance/water_billing.sqlite3 completely. Type RESET to continue: " CONFIRM
if [[ "$CONFIRM" != "RESET" ]]; then
  echo "Aborted. No database changes were made."
  exit 1
fi

echo "==> Stopping PM2 application (if running)"
pm2 stop water-pos-saif >/dev/null 2>&1 || true

mkdir -p "$BASE_DIR/instance" "$BASE_DIR/backups"

if [[ -f "$BASE_DIR/instance/water_billing.sqlite3" ]]; then
  TS="$(date +%Y%m%d_%H%M%S)"
  cp -p "$BASE_DIR/instance/water_billing.sqlite3" "$BASE_DIR/backups/pre_reset_$TS.sqlite3"
  echo "Backup saved: backups/pre_reset_$TS.sqlite3"
fi

echo "==> Removing active SQLite database"
rm -f "$BASE_DIR/instance/water_billing.sqlite3"

source "$BASE_DIR/venv/bin/activate"

echo "==> Creating fresh schema and seed data"
python "$BASE_DIR/init_schema.py"

echo "==> Creating requested new admin"
python "$BASE_DIR/deploy/create_admin.py"

echo "==> Removing the historical bootstrap user, if different"
python3 - "$BASE_DIR" <<'PY'
from pathlib import Path
import sqlite3
import sys

base = Path(sys.argv[1])
db_path = base / "instance" / "water_billing.sqlite3"
env_file = base / ".env"
target_username = ""

for line in env_file.read_text(encoding="utf-8").splitlines():
    if line.startswith("ADMIN_USERNAME="):
        target_username = line.split("=", 1)[1].strip()
        break

conn = sqlite3.connect(str(db_path))
try:
    conn.execute("PRAGMA foreign_keys = ON")
    if target_username and target_username != "zydan":
        conn.execute("DELETE FROM users WHERE username='zydan'")
        conn.commit()
finally:
    conn.close()

print("Fresh SQLite database and admin user are ready.")
PY

echo
echo "==> Reset complete"
echo "Admin username: $ADMIN_USERNAME"
echo "Start: pm2 start deploy/ecosystem.config.cjs"
