# ─────────────────────────────────────────────────────────────────────────────
# Akshaya Vistara — Production Dockerfile
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Prevent .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Force Python to include the /app directory in its search path
ENV PYTHONPATH=/app

# Set working directory inside the container
WORKDIR /app

# ── System dependencies ──────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr \
      tesseract-ocr-eng \
      poppler-utils \
      fontconfig \
      fonts-dejavu-core \
      libcairo2 \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      libgdk-pixbuf-2.0-0 \
      shared-mime-info \
      libpq-dev \
      gcc \
      curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ──────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ── Copy project source ──────────────────────────────────────────────────────
COPY . .

# ── Permissions & Script Prep (CRITICAL) ─────────────────────────────────────
# We do this while still ROOT so the script can be made executable
RUN chmod +x docker-entrypoint.sh \
 && mkdir -p /app/staticfiles /app/media /app/logs

# ── Collect static files ─────────────────────────────────────────────────────
# We use a dummy SECRET_KEY and local PYTHONPATH for the build step
RUN SECRET_KEY=build-time-placeholder \
    DATABASE_URL=sqlite:////tmp/build_db.sqlite3 \
    DEBUG=False \
    ALLOWED_HOSTS=build.local \
    CSRF_TRUSTED_ORIGINS=https://build.local \
    WHATSAPP_WEBHOOK_TOKEN=build-time-placeholder-token-123456 \
    CELERY_BROKER_URL=redis://redis:6379/0 \
    EMAIL_HOST=build.local \
    DEFAULT_FROM_EMAIL=no-reply@build.local \
    PYTHONPATH=. python3 manage.py collectstatic --noinput --settings=akshaya_vistara.settings

# ── Non-root user for security ───────────────────────────────────────────────
RUN addgroup --system appgroup && adduser --system --group appuser
RUN chown -R appuser:appgroup /app
USER appuser

# ── Expose port 8000 ─────────────────────────────────────────────────────────
EXPOSE 8000

# ── Entrypoint ───────────────────────────────────────────────────────────────
# We use the relative path (./) inside /app
ENTRYPOINT ["/bin/sh", "./docker-entrypoint.sh"]

CMD ["gunicorn", "akshaya_vistara.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
