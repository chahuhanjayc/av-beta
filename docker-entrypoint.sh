#!/bin/sh
set -e

echo "Akshaya Vistara container starting"

RUN_DEPLOY_CHECKS="${RUN_DEPLOY_CHECKS:-true}"
RUN_COLLECTSTATIC="${RUN_COLLECTSTATIC:-true}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-false}"

if [ "$RUN_DEPLOY_CHECKS" != "false" ]; then
    echo "Running Django deployment checks..."
    python manage.py check --deploy
fi

if [ "$RUN_COLLECTSTATIC" != "false" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput
fi

if [ "$RUN_MIGRATIONS" = "true" ]; then
    if [ -z "$DATABASE_URL" ]; then
        echo "ERROR: RUN_MIGRATIONS=true but DATABASE_URL is not set."
        exit 1
    fi

    echo "Waiting for database..."
    MAX_TRIES="${DB_WAIT_MAX_TRIES:-30}"
    count=0
    until python -c "
import os
import psycopg2
import sys

try:
    conn = psycopg2.connect(os.environ['DATABASE_URL'], connect_timeout=3)
    conn.close()
except Exception as exc:
    print(f'Database not ready: {exc}')
    sys.exit(1)
"; do
        count=$((count + 1))
        if [ "$count" -ge "$MAX_TRIES" ]; then
            echo "ERROR: database was not ready after $MAX_TRIES attempts."
            exit 1
        fi
        sleep 2
    done

    echo "Running database migrations..."
    python manage.py migrate --noinput
else
    echo "Skipping database migrations. Set RUN_MIGRATIONS=true to enable them."
fi

echo "Startup checks complete. Starting process..."
exec "$@"
