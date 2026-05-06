from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security, deploy=True)
def production_readiness_checks(app_configs, **kwargs):
    issues = []

    if settings.DEBUG:
        issues.append(
            Error(
                "DEBUG must be disabled for production.",
                id="akshaya.E001",
            )
        )

    allowed_hosts = set(getattr(settings, "ALLOWED_HOSTS", []) or [])
    unsafe_hosts = {"*", "localhost", "127.0.0.1", "[::1]"}
    placeholder_hosts = {host for host in allowed_hosts if "your-domain" in host}
    if not allowed_hosts or allowed_hosts & unsafe_hosts or placeholder_hosts:
        issues.append(
            Error(
                "ALLOWED_HOSTS must contain only real production hostnames.",
                hint="Set ALLOWED_HOSTS in the environment.",
                id="akshaya.E002",
            )
        )

    secret_key = getattr(settings, "SECRET_KEY", "")
    placeholder_secret = any(
        marker in secret_key.lower()
        for marker in ("replace", "change", "placeholder", "your-secret")
    )
    if len(secret_key) < 50 or placeholder_secret:
        issues.append(
            Error(
                "SECRET_KEY is too short for production.",
                hint="Set a strong random SECRET_KEY in the environment.",
                id="akshaya.E003",
            )
        )

    webhook_token = getattr(settings, "WHATSAPP_WEBHOOK_TOKEN", "")
    if len(webhook_token) < 32:
        issues.append(
            Warning(
                "WHATSAPP_WEBHOOK_TOKEN is not configured with a strong token.",
                hint="Set a random token of at least 32 characters before enabling webhook approvals.",
                id="akshaya.W001",
            )
        )

    if getattr(settings, "ALLOW_PUBLIC_REGISTRATION", False):
        issues.append(
            Error(
                "Public self-registration must be disabled for production.",
                hint="Use admin-created users or invite-only onboarding.",
                id="akshaya.E008",
            )
        )

    if not getattr(settings, "REQUIRE_STAFF_MFA", False):
        issues.append(
            Warning(
                "Staff/admin MFA is not required.",
                hint="Set REQUIRE_STAFF_MFA=True before exposing staff or superuser accounts.",
                id="akshaya.W010",
            )
        )

    if not getattr(settings, "CSRF_TRUSTED_ORIGINS", []):
        issues.append(
            Warning(
                "CSRF_TRUSTED_ORIGINS is empty.",
                hint="Set it to the HTTPS origin(s) that serve this app.",
                id="akshaya.W002",
            )
        )
    elif any("your-domain" in origin for origin in settings.CSRF_TRUSTED_ORIGINS):
        issues.append(
            Error(
                "CSRF_TRUSTED_ORIGINS still contains placeholder domains.",
                hint="Set CSRF_TRUSTED_ORIGINS to the real HTTPS origin(s).",
                id="akshaya.E006",
            )
        )

    if not getattr(settings, "SECURE_PROXY_SSL_HEADER", None):
        issues.append(
            Warning(
                "SECURE_PROXY_SSL_HEADER is not configured.",
                hint="Set it when Django runs behind a TLS-terminating proxy.",
                id="akshaya.W003",
            )
        )

    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        issues.append(
            Error(
                "CELERY_TASK_ALWAYS_EAGER must be disabled for production.",
                id="akshaya.E004",
            )
        )

    default_db = settings.DATABASES.get("default", {})
    if default_db.get("ENGINE") == "django.db.backends.sqlite3":
        issues.append(
            Error(
                "SQLite must not be used for production accounting data.",
                hint="Set DATABASE_URL to a PostgreSQL database.",
                id="akshaya.E007",
            )
        )

    celery_broker = getattr(settings, "CELERY_BROKER_URL", "")
    if not celery_broker or "localhost" in celery_broker or "127.0.0.1" in celery_broker:
        issues.append(
            Warning(
                "CELERY_BROKER_URL is not configured for a production Redis service.",
                hint="Set CELERY_BROKER_URL to your Redis URL before enabling async OCR.",
                id="akshaya.W004",
            )
        )

    email_backend = getattr(settings, "EMAIL_BACKEND", "")
    email_host = getattr(settings, "EMAIL_HOST", "")
    if email_backend.endswith("smtp.EmailBackend") and not email_host:
        issues.append(
            Warning(
                "SMTP email is enabled but EMAIL_HOST is empty.",
                hint="Set EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, and DEFAULT_FROM_EMAIL.",
                id="akshaya.W005",
            )
        )
    elif "your-provider" in email_host:
        issues.append(
            Warning(
                "EMAIL_HOST still contains a placeholder provider.",
                hint="Set EMAIL_HOST to a real SMTP host.",
                id="akshaya.W007",
            )
        )

    default_from = getattr(settings, "DEFAULT_FROM_EMAIL", "")
    if "example.com" in default_from or "your-domain" in default_from:
        issues.append(
            Warning(
                "DEFAULT_FROM_EMAIL still uses a placeholder address.",
                hint="Set DEFAULT_FROM_EMAIL to a real sender domain.",
                id="akshaya.W006",
            )
        )

    gst_enabled = (
        getattr(settings, "E_INVOICE_ENABLED", False)
        or getattr(settings, "E_WAY_BILL_ENABLED", False)
    )
    if gst_enabled:
        if getattr(settings, "GST_API_PROVIDER", "") == "mock" and not getattr(settings, "GST_API_SANDBOX_MODE", True):
            issues.append(
                Error(
                    "GST_API_PROVIDER=mock cannot be used outside sandbox mode.",
                    hint="Use a real GST/e-invoice provider before enabling production filings.",
                    id="akshaya.E011",
                )
            )
        missing_gst = [
            name for name in ("GST_API_PROVIDER", "GST_API_BASE_URL", "GST_API_KEY", "GST_API_SECRET")
            if not getattr(settings, name, "") and getattr(settings, "GST_API_PROVIDER", "") != "mock"
        ]
        if missing_gst:
            issues.append(
                Error(
                    "GST/e-invoice integrations are enabled but API credentials are incomplete.",
                    hint=f"Set: {', '.join(missing_gst)}.",
                    id="akshaya.E009",
                )
            )
        if not getattr(settings, "GST_API_SANDBOX_MODE", True) and not getattr(settings, "GST_API_TAXPAYER_GSTIN", ""):
            issues.append(
                Error(
                    "GST production integrations need GST_API_TAXPAYER_GSTIN.",
                    hint="Set the GSTIN enrolled with the API/GSP provider.",
                    id="akshaya.E010",
                )
            )

    if getattr(settings, "PAYMENT_PROVIDER", "") and not getattr(settings, "PAYMENT_WEBHOOK_SECRET", ""):
        issues.append(
            Warning(
                "Payment provider is configured without a webhook secret.",
                hint="Set PAYMENT_WEBHOOK_SECRET before accepting payment webhooks.",
                id="akshaya.W011",
            )
        )

    if getattr(settings, "BANK_FEED_PROVIDER", ""):
        missing_bank = [
            name for name in ("BANK_FEED_BASE_URL", "BANK_FEED_API_KEY")
            if not getattr(settings, name, "")
        ]
        if missing_bank:
            issues.append(
                Warning(
                    "Connected banking provider is configured but credentials are incomplete.",
                    hint=f"Set: {', '.join(missing_bank)}.",
                    id="akshaya.W012",
                )
            )

    whatsapp_api_url = getattr(settings, "WHATSAPP_API_URL", "")
    whatsapp_api_token = getattr(settings, "WHATSAPP_API_TOKEN", "")
    if bool(whatsapp_api_url) != bool(whatsapp_api_token):
        issues.append(
            Warning(
                "WhatsApp outbound API configuration is incomplete.",
                hint="Set both WHATSAPP_API_URL and WHATSAPP_API_TOKEN before enabling client document chases.",
                id="akshaya.W013",
            )
        )

    media_root = getattr(settings, "MEDIA_ROOT", None)
    if not media_root:
        issues.append(
            Error(
                "MEDIA_ROOT is not configured.",
                hint="Set MEDIA_ROOT to a persistent filesystem path.",
                id="akshaya.E005",
            )
        )

    return issues


@register()
def local_sqlite_storage_checks(app_configs, **kwargs):
    issues = []
    db = settings.DATABASES.get("default", {})
    if db.get("ENGINE") != "django.db.backends.sqlite3":
        return issues

    raw_name = db.get("NAME")
    if not raw_name:
        return issues

    db_path = Path(raw_name)
    if not db_path.is_absolute():
        db_path = Path(settings.BASE_DIR) / db_path

    path_text = str(db_path).lower()
    if "onedrive" in path_text:
        issues.append(
            Warning(
                "SQLite database is stored inside a OneDrive-synced folder.",
                hint=(
                    "Move db.sqlite3 outside OneDrive or set DATABASE_URL to a "
                    "PostgreSQL database for production/accounting use."
                ),
                id="akshaya.W008",
            )
        )

    if db_path.with_name(db_path.name + "-journal").exists():
        issues.append(
            Warning(
                "A SQLite journal file is present next to the database.",
                hint=(
                    "Stop duplicate dev servers and verify the database was shut down "
                    "cleanly before running data repair or audit scripts."
                ),
                id="akshaya.W009",
            )
        )

    return issues
