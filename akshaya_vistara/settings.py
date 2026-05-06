"""
akshaya_vistara/settings.py
Production-ready Django settings for akshaya_vistara.
"""

import environ
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Base directory
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
)

environ.Env.read_env(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Core settings
# ---------------------------------------------------------------------------
# In production, this MUST be set in the Environment Variables.
SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
RENDER_EXTERNAL_HOSTNAME = env("RENDER_EXTERNAL_HOSTNAME", default="")
if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
WHATSAPP_WEBHOOK_TOKEN = env("WHATSAPP_WEBHOOK_TOKEN", default="")
WHATSAPP_API_URL = env("WHATSAPP_API_URL", default="")
WHATSAPP_API_TOKEN = env("WHATSAPP_API_TOKEN", default="")
ALLOW_PUBLIC_REGISTRATION = env.bool("ALLOW_PUBLIC_REGISTRATION", default=False)
REGISTRATION_INVITE_CODE = env("REGISTRATION_INVITE_CODE", default="")
REQUIRE_STAFF_MFA = env.bool("REQUIRE_STAFF_MFA", default=not DEBUG)
BACKUP_ENCRYPTION_KEY = env("BACKUP_ENCRYPTION_KEY", default="")
BACKUP_ENCRYPTION_PASSPHRASE = env("BACKUP_ENCRYPTION_PASSPHRASE", default="")
BACKUP_ENCRYPTION_REQUIRED = env.bool("BACKUP_ENCRYPTION_REQUIRED", default=True)
BACKUP_ENCRYPTION_DEFAULT = env.bool(
    "BACKUP_ENCRYPTION_DEFAULT",
    default=bool(BACKUP_ENCRYPTION_KEY or BACKUP_ENCRYPTION_PASSPHRASE),
)
BACKUP_RETENTION_COUNT = env.int("BACKUP_RETENTION_COUNT", default=10)
BACKUP_MAX_AGE_HOURS = env.int("BACKUP_MAX_AGE_HOURS", default=24)
BACKUP_MIN_RETAINED_MANIFESTS = env.int("BACKUP_MIN_RETAINED_MANIFESTS", default=3)
RESTORE_DRILL_MAX_AGE_DAYS = env.int("RESTORE_DRILL_MAX_AGE_DAYS", default=30)
BACKUP_SCHEDULE_ENABLED = env.bool("BACKUP_SCHEDULE_ENABLED", default=False)
BACKUP_SCHEDULE_INTERVAL_HOURS = env.int("BACKUP_SCHEDULE_INTERVAL_HOURS", default=24)
BACKUP_SCHEDULE_MAX_AGE_HOURS = env.int("BACKUP_SCHEDULE_MAX_AGE_HOURS", default=26)
BACKUP_OFFSITE_DIR = env("BACKUP_OFFSITE_DIR", default="")
BACKUP_OFFSITE_REQUIRED = env.bool("BACKUP_OFFSITE_REQUIRED", default=True)
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
if RENDER_EXTERNAL_HOSTNAME:
    render_origin = f"https://{RENDER_EXTERNAL_HOSTNAME}"
    if render_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(render_origin)

SECURE_PROXY_SSL_HEADER_NAME = env("SECURE_PROXY_SSL_HEADER_NAME", default="")
SECURE_PROXY_SSL_HEADER_VALUE = env("SECURE_PROXY_SSL_HEADER_VALUE", default="https")
if SECURE_PROXY_SSL_HEADER_NAME:
    SECURE_PROXY_SSL_HEADER = (
        SECURE_PROXY_SSL_HEADER_NAME,
        SECURE_PROXY_SSL_HEADER_VALUE,
    )

# ---------------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    # Django built-ins
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "crispy_forms",
    "crispy_bootstrap5",
    # Local apps
    "core",
    "accounts",
    "ledger",
    "vouchers",
    "ocr",
    "reports",
    "inventory",
    "costcenter",
    "orders",
    "payroll",
    "fixedassets",
    "tds",
    "forex",
    "clients",
    "audit",
    "reconciliation",
    "receivables",
    "purchase",
    "sales",
    "portal",
    "migration",
    "gstr2b",
    "personal_finance",
    "integrations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.CurrentCompanyMiddleware",
    "clients.middleware.SubscriptionMiddleware",
]

ROOT_URLCONF = "akshaya_vistara.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Custom: makes current_company available in all templates
                "core.context_processors.current_company",
            ],
        },
    },
]

WSGI_APPLICATION = "akshaya_vistara.wsgi.application"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASES = {
    "default": env.db(default="sqlite:///db.sqlite3"),
}
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"].setdefault("timeout", 20)

# ---------------------------------------------------------------------------
# Custom user model
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static and media files
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = Path(env("STATIC_ROOT", default=str(BASE_DIR / "staticfiles")))
STATICFILES_STORAGE = (
    "django.contrib.staticfiles.storage.StaticFilesStorage"
    if DEBUG
    else "whitenoise.storage.CompressedManifestStaticFilesStorage"
)

MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", default=str(BASE_DIR / "media")))
FILE_UPLOAD_MAX_MEMORY_SIZE = env.int("FILE_UPLOAD_MAX_MEMORY_SIZE", default=10 * 1024 * 1024)
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int("DATA_UPLOAD_MAX_MEMORY_SIZE", default=25 * 1024 * 1024)
DATA_UPLOAD_MAX_NUMBER_FIELDS = env.int("DATA_UPLOAD_MAX_NUMBER_FIELDS", default=5000)

# ---------------------------------------------------------------------------
# Default primary key
# ---------------------------------------------------------------------------
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Celery (async OCR and background tasks)
# ---------------------------------------------------------------------------
# Broker URL — override in .env for production Redis:
#   CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=CELERY_BROKER_URL)
CELERY_ACCEPT_CONTENT     = ["json"]
CELERY_TASK_SERIALIZER    = "json"
CELERY_RESULT_SERIALIZER  = "json"
CELERY_TIMEZONE           = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT    = 300   # 5-minute hard limit per task
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_WORKER_PREFETCH_MULTIPLIER = env.int("CELERY_WORKER_PREFETCH_MULTIPLIER", default=1)

# ── Development / testing without Redis ──────────────────────────────────────
# Set CELERY_TASK_ALWAYS_EAGER=True in .env to run tasks synchronously
# (simulates async behaviour inline — no worker process needed).
CELERY_TASK_ALWAYS_EAGER          = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES      = True
CELERY_BEAT_SCHEDULE = {}
if BACKUP_SCHEDULE_ENABLED:
    CELERY_BEAT_SCHEDULE["scheduled-operational-backup"] = {
        "task": "core.tasks.scheduled_operational_backup",
        "schedule": max(1, BACKUP_SCHEDULE_INTERVAL_HOURS) * 60 * 60,
        "options": {"expires": 6 * 60 * 60},
    }

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default=(
        "django.core.mail.backends.console.EmailBackend"
        if DEBUG
        else "django.core.mail.backends.smtp.EmailBackend"
    ),
)
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=20)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Akshaya Vistara <no-reply@example.com>")
SERVER_EMAIL = env("SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)

# ---------------------------------------------------------------------------
# External integrations
# ---------------------------------------------------------------------------
GST_API_PROVIDER = env("GST_API_PROVIDER", default="")
GST_API_BASE_URL = env("GST_API_BASE_URL", default="")
GST_API_KEY = env("GST_API_KEY", default="")
GST_API_SECRET = env("GST_API_SECRET", default="")
GST_API_AUTH_PATH = env("GST_API_AUTH_PATH", default="/auth")
GST_API_GSTIN_LOOKUP_PATH = env("GST_API_GSTIN_LOOKUP_PATH", default="/gstin")
GST_API_E_INVOICE_PATH = env("GST_API_E_INVOICE_PATH", default="/e-invoice")
GST_API_E_WAY_BILL_PATH = env("GST_API_E_WAY_BILL_PATH", default="/e-way-bill")
GST_API_TIMEOUT_SECONDS = env.int("GST_API_TIMEOUT_SECONDS", default=20)
GST_API_TAXPAYER_GSTIN = env("GST_API_TAXPAYER_GSTIN", default="")
GST_API_USERNAME = env("GST_API_USERNAME", default="")
GST_API_PASSWORD = env("GST_API_PASSWORD", default="")
GST_API_SANDBOX_MODE = env.bool("GST_API_SANDBOX_MODE", default=True)
E_INVOICE_ENABLED = env.bool("E_INVOICE_ENABLED", default=False)
E_WAY_BILL_ENABLED = env.bool("E_WAY_BILL_ENABLED", default=False)
BANK_FEED_PROVIDER = env("BANK_FEED_PROVIDER", default="")
BANK_FEED_BASE_URL = env("BANK_FEED_BASE_URL", default="")
BANK_FEED_API_KEY = env("BANK_FEED_API_KEY", default="")
BANK_FEED_API_SECRET = env("BANK_FEED_API_SECRET", default="")
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER", default="")
PAYMENT_API_KEY = env("PAYMENT_API_KEY", default="")
PAYMENT_WEBHOOK_SECRET = env("PAYMENT_WEBHOOK_SECRET", default="")

# ---------------------------------------------------------------------------
# Optional Redis cache
# ---------------------------------------------------------------------------
CACHE_URL = env("CACHE_URL", default="")
if CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": CACHE_URL,
        }
    }

# ---------------------------------------------------------------------------
# Crispy forms
# ---------------------------------------------------------------------------
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

# ---------------------------------------------------------------------------
# Auth redirects
# ---------------------------------------------------------------------------
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/core/select-company/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
SESSION_COOKIE_AGE = 86400  # 24 hours (reduced from 7 days)
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = env("LOG_LEVEL", default="WARNING")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "akshaya_vistara": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "core": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "vouchers": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "ocr": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "portal": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
}

# ---------------------------------------------------------------------------
# Security (production overrides)
# ---------------------------------------------------------------------------
if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
