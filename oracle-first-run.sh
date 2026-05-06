#!/usr/bin/env bash
set -Eeuo pipefail

# First-run installer for Akshaya Vistara on an Oracle Cloud VM.
#
# Run from the repository root:
#   chmod +x oracle-first-run.sh
#   ./oracle-first-run.sh
#
# Required before migrations:
#   1. Create/edit .env with production values.
#   2. DATABASE_URL must point to PostgreSQL, not SQLite.
#   3. PostgreSQL and Redis must be reachable from this VM.
#
# Optional flags via environment variables:
#   SKIP_SYSTEM_PACKAGES=true    Do not install OS packages.
#   SKIP_DEPLOY_CHECK=true       Skip manage.py check --deploy.
#   SKIP_COLLECTSTATIC=true      Skip collectstatic.
#   SKIP_MIGRATIONS=true        Skip migrations.
#   CREATE_SUPERUSER=true       Run createsuperuser at the end.

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$APP_DIR"

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

run_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "ERROR: root access is required for system packages. Install sudo or run as root."
        exit 1
    fi
}

install_system_packages() {
    if [ "${SKIP_SYSTEM_PACKAGES:-false}" = "true" ]; then
        log "Skipping system package installation."
        return
    fi

    if command -v apt-get >/dev/null 2>&1; then
        log "Installing system packages with apt-get."
        run_as_root apt-get update
        run_as_root apt-get install -y --no-install-recommends \
            python3 python3-venv python3-pip python3-dev \
            build-essential gcc curl pkg-config \
            tesseract-ocr tesseract-ocr-eng poppler-utils \
            fontconfig fonts-dejavu-core \
            libcairo2 libpango-1.0-0 libpangoft2-1.0-0 libgdk-pixbuf-2.0-0 \
            shared-mime-info libpq-dev postgresql-client redis-tools
        return
    fi

    if command -v dnf >/dev/null 2>&1; then
        log "Installing system packages with dnf."
        run_as_root dnf install -y dnf-plugins-core || true
        if command -v rpm >/dev/null 2>&1; then
            RHEL_MAJOR="$(rpm -E '%{rhel}' 2>/dev/null || true)"
            if [ -n "$RHEL_MAJOR" ] && [ "$RHEL_MAJOR" != "%{rhel}" ]; then
                run_as_root dnf install -y "oracle-epel-release-el${RHEL_MAJOR}" || true
                run_as_root dnf config-manager --set-enabled "ol${RHEL_MAJOR}_developer_EPEL" || true
            fi
        fi
        run_as_root dnf install -y \
            python3 python3-pip python3-devel \
            gcc gcc-c++ make curl pkgconf-pkg-config \
            tesseract tesseract-langpack-eng poppler-utils \
            fontconfig dejavu-sans-fonts \
            cairo pango gdk-pixbuf2 shared-mime-info \
            libpq-devel postgresql redis
        return
    fi

    if command -v yum >/dev/null 2>&1; then
        log "Installing system packages with yum."
        run_as_root yum install -y epel-release || true
        run_as_root yum install -y \
            python3 python3-pip python3-devel \
            gcc gcc-c++ make curl pkgconfig \
            tesseract tesseract-langpack-eng poppler-utils \
            fontconfig dejavu-sans-fonts \
            cairo pango gdk-pixbuf2 shared-mime-info \
            libpq-devel postgresql redis
        return
    fi

    echo "ERROR: supported package manager not found. Install dependencies manually."
    exit 1
}

ensure_env_file() {
    if [ -f "$APP_DIR/.env" ]; then
        return
    fi

    if [ -f "$APP_DIR/.env.production.example" ]; then
        cp "$APP_DIR/.env.production.example" "$APP_DIR/.env"
        chmod 600 "$APP_DIR/.env"
        echo "Created .env from .env.production.example."
        echo "Edit .env with real Oracle/PostgreSQL/Redis/domain values, then rerun this script."
        exit 1
    fi

    echo "ERROR: .env is missing and .env.production.example was not found."
    exit 1
}

ensure_no_placeholder_env() {
    if grep -Eq 'replace-with|your-domain|your-postgres-host|your-redis-host|your-provider|example\.com' "$APP_DIR/.env"; then
        echo "ERROR: .env still contains placeholder production values."
        echo "Edit .env first, especially SECRET_KEY, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, DATABASE_URL, Redis, email, and WHATSAPP_WEBHOOK_TOKEN."
        exit 1
    fi
}

setup_python() {
    log "Creating/updating Python virtual environment."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
    "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
}

wait_for_database() {
    log "Checking database connectivity."
    "$VENV_DIR/bin/python" - <<'PY'
import os
import pathlib
import sys
import time

import environ
import psycopg2

env = environ.Env()
env.read_env(pathlib.Path(".env"))
database_url = os.environ.get("DATABASE_URL") or env("DATABASE_URL", default="")
if not database_url:
    print("ERROR: DATABASE_URL is not configured.")
    sys.exit(1)
if database_url.startswith("sqlite"):
    print("ERROR: DATABASE_URL points to SQLite. Use PostgreSQL for production.")
    sys.exit(1)

max_tries = int(os.environ.get("DB_WAIT_MAX_TRIES", "30"))
for attempt in range(1, max_tries + 1):
    try:
        conn = psycopg2.connect(database_url, connect_timeout=5)
        conn.close()
        print("Database connection OK.")
        sys.exit(0)
    except Exception as exc:
        print(f"Database not ready ({attempt}/{max_tries}): {exc}")
        time.sleep(2)

sys.exit(1)
PY
}

run_django_steps() {
    mkdir -p "$APP_DIR/staticfiles" "$APP_DIR/media" "$APP_DIR/logs"

    if [ "${SKIP_DEPLOY_CHECK:-false}" != "true" ]; then
        log "Running Django deploy checks."
        "$VENV_DIR/bin/python" manage.py check --deploy
    fi

    if [ "${SKIP_COLLECTSTATIC:-false}" != "true" ]; then
        log "Collecting static files."
        "$VENV_DIR/bin/python" manage.py collectstatic --noinput
    fi

    if [ "${SKIP_MIGRATIONS:-false}" != "true" ]; then
        wait_for_database
        log "Running database migrations."
        "$VENV_DIR/bin/python" manage.py migrate --noinput
    fi

    log "Running final Django system check."
    "$VENV_DIR/bin/python" manage.py check

    if [ "${CREATE_SUPERUSER:-false}" = "true" ]; then
        log "Creating Django superuser."
        "$VENV_DIR/bin/python" manage.py createsuperuser
    fi
}

install_system_packages
setup_python
ensure_env_file
ensure_no_placeholder_env
run_django_steps

cat <<EOF

First run complete.

Start the web app with:
  $VENV_DIR/bin/gunicorn akshaya_vistara.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120

Start the Celery worker with:
  $VENV_DIR/bin/celery -A akshaya_vistara worker --loglevel=info

EOF
