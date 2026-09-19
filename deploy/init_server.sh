#!/usr/bin/env bash
set -Eeuo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BASE_DIR"

umask 077

echo "==> Water POS server initialization"
echo "Project: $BASE_DIR"

if [[ ! -f "$BASE_DIR/.env" ]]; then
  read -r -p "Admin username: " ADMIN_USERNAME
  while [[ -z "$ADMIN_USERNAME" ]]; do
    read -r -p "Admin username cannot be empty. Enter again: " ADMIN_USERNAME
  done

  read -r -s -p "Admin password: " ADMIN_PASSWORD
  echo
  while [[ -z "$ADMIN_PASSWORD" ]]; do
    read -r -s -p "Admin password cannot be empty. Enter again: " ADMIN_PASSWORD
    echo
  done

  SECRET_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"

  cat > "$BASE_DIR/.env" <<EOF
APP_ENV=production
PORT=5029
FLASK_DEBUG=0
FORCE_HTTPS=0
GUNICORN_WORKERS=1
GUNICORN_THREADS=2
GUNICORN_TIMEOUT=120
SECRET_KEY=$SECRET_KEY
ADMIN_USERNAME=$ADMIN_USERNAME
ADMIN_PASSWORD=$ADMIN_PASSWORD
EOF

  chmod 600 "$BASE_DIR/.env"
  echo "Created $BASE_DIR/.env"
else
  echo "Existing .env preserved"
fi

echo "==> Creating Python virtual environment"
if [[ ! -x "$BASE_DIR/venv/bin/python" ]]; then
  python3 -m venv "$BASE_DIR/venv"
fi

source "$BASE_DIR/venv/bin/activate"

echo "==> Installing Python requirements"
python -m pip install --upgrade pip
python -m pip install -r "$BASE_DIR/requirements.txt"

echo "==> Ensuring Playwright Chromium is installed"
python -m playwright install chromium

echo "==> Creating runtime directories"
mkdir -p "$BASE_DIR/instance" "$BASE_DIR/uploads" "$BASE_DIR/backups"

if [[ ! -f "$BASE_DIR/instance/water_billing.sqlite3" ]]; then
  echo "==> Initializing a new SQLite database"
  python "$BASE_DIR/init_schema.py"
  python "$BASE_DIR/deploy/create_admin.py"
else
  echo "Existing database detected; no database data was changed."
fi

echo
echo "==> Initialization complete"
echo "Start: pm2 start deploy/ecosystem.config.cjs"
echo "URL: https://saif.alattab.site"
