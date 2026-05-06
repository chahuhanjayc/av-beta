import argparse
import os
import sys


UNSAFE_HOSTS = {"*", "localhost", "127.0.0.1", "[::1]"}


def parse_env_file(path):
    if not os.path.exists(path):
        raise ValueError(f"{path} not found.")

    with open(path, "rb") as handle:
        data = handle.read()

    if b"\x00" in data:
        raise ValueError("embedded null character found.")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"file is not valid UTF-8: {exc}") from exc

    values = {}
    invalid_lines = []
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            invalid_lines.append((line_no, raw_line))
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    if invalid_lines:
        details = ", ".join(f"line {line_no}" for line_no, _ in invalid_lines)
        raise ValueError(f"invalid key=value format at {details}.")

    return values


def split_list(value):
    return {item.strip() for item in value.split(",") if item.strip()}


def check_production_values(values):
    errors = []
    warnings = []

    if values.get("DEBUG", "").lower() not in {"false", "0", "no"}:
        errors.append("DEBUG must be False for production.")

    secret_key = values.get("SECRET_KEY", "")
    if len(secret_key) < 50 or "replace" in secret_key.lower() or "change" in secret_key.lower():
        errors.append("SECRET_KEY must be a real 50+ character secret.")

    allowed_hosts = split_list(values.get("ALLOWED_HOSTS", ""))
    if not allowed_hosts:
        errors.append("ALLOWED_HOSTS is required.")
    elif allowed_hosts & UNSAFE_HOSTS:
        errors.append("ALLOWED_HOSTS contains unsafe development hosts.")
    elif any("your-domain" in host for host in allowed_hosts):
        errors.append("ALLOWED_HOSTS still contains placeholder domains.")

    csrf_origins = split_list(values.get("CSRF_TRUSTED_ORIGINS", ""))
    if not csrf_origins or not all(origin.startswith("https://") for origin in csrf_origins):
        errors.append("CSRF_TRUSTED_ORIGINS must contain production HTTPS origins.")
    elif any("your-domain" in origin for origin in csrf_origins):
        errors.append("CSRF_TRUSTED_ORIGINS still contains placeholder domains.")

    if not values.get("SECURE_PROXY_SSL_HEADER_NAME"):
        warnings.append("SECURE_PROXY_SSL_HEADER_NAME is not set.")

    broker = values.get("CELERY_BROKER_URL", "")
    if not broker or "localhost" in broker or "127.0.0.1" in broker:
        warnings.append("CELERY_BROKER_URL does not point to a production Redis service.")

    if values.get("CELERY_TASK_ALWAYS_EAGER", "").lower() in {"true", "1", "yes"}:
        errors.append("CELERY_TASK_ALWAYS_EAGER must be False for production.")

    if values.get("ALLOW_PUBLIC_REGISTRATION", "").lower() in {"true", "1", "yes"}:
        errors.append("ALLOW_PUBLIC_REGISTRATION must be False for production.")

    if values.get("REQUIRE_STAFF_MFA", "").lower() not in {"true", "1", "yes"}:
        warnings.append("REQUIRE_STAFF_MFA should be True for production staff/admin accounts.")

    if not values.get("EMAIL_HOST"):
        warnings.append("EMAIL_HOST is not set.")
    elif "your-provider" in values.get("EMAIL_HOST", ""):
        warnings.append("EMAIL_HOST still contains a placeholder provider.")

    default_from = values.get("DEFAULT_FROM_EMAIL", "")
    if not default_from or "example.com" in default_from or "your-domain" in default_from:
        warnings.append("DEFAULT_FROM_EMAIL is missing or still uses a placeholder domain.")

    token = values.get("WHATSAPP_WEBHOOK_TOKEN", "")
    if token and len(token) < 32:
        warnings.append("WHATSAPP_WEBHOOK_TOKEN should be at least 32 characters.")
    if bool(values.get("WHATSAPP_API_URL")) != bool(values.get("WHATSAPP_API_TOKEN")):
        warnings.append("WHATSAPP outbound API config is incomplete; set both WHATSAPP_API_URL and WHATSAPP_API_TOKEN.")

    gst_enabled = values.get("E_INVOICE_ENABLED", "").lower() in {"true", "1", "yes"} or values.get("E_WAY_BILL_ENABLED", "").lower() in {"true", "1", "yes"}
    if gst_enabled:
        provider = values.get("GST_API_PROVIDER", "")
        if provider == "mock" and values.get("GST_API_SANDBOX_MODE", "true").lower() in {"false", "0", "no"}:
            errors.append("GST_API_PROVIDER=mock cannot be used when GST_API_SANDBOX_MODE is False.")
        missing_gst = []
        if provider != "mock":
            missing_gst = [
                key for key in ("GST_API_PROVIDER", "GST_API_BASE_URL", "GST_API_KEY", "GST_API_SECRET")
                if not values.get(key)
            ]
        if missing_gst:
            errors.append(f"GST/e-invoice enabled but missing: {', '.join(missing_gst)}.")
        if values.get("GST_API_SANDBOX_MODE", "true").lower() in {"false", "0", "no"} and not values.get("GST_API_TAXPAYER_GSTIN"):
            errors.append("GST_API_TAXPAYER_GSTIN is required when GST_API_SANDBOX_MODE is False.")

    if values.get("PAYMENT_PROVIDER") and not values.get("PAYMENT_WEBHOOK_SECRET"):
        warnings.append("PAYMENT_PROVIDER is set but PAYMENT_WEBHOOK_SECRET is missing.")

    if values.get("BANK_FEED_PROVIDER"):
        missing_bank = [key for key in ("BANK_FEED_BASE_URL", "BANK_FEED_API_KEY") if not values.get(key)]
        if missing_bank:
            warnings.append(f"BANK_FEED_PROVIDER is set but missing: {', '.join(missing_bank)}.")

    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description="Validate Akshaya Vistara env files.")
    parser.add_argument("path", nargs="?", default=".env", help="Path to env file.")
    parser.add_argument(
        "--production",
        action="store_true",
        help="Also validate production readiness values.",
    )
    args = parser.parse_args()

    try:
        values = parse_env_file(args.path)
    except ValueError as exc:
        print(f"Validation FAILED: {exc}")
        return 1

    print(f"Syntax OK: {args.path} ({len(values)} key-value pairs).")

    if args.production:
        errors, warnings = check_production_values(values)
        for warning in warnings:
            print(f"WARNING: {warning}")
        for error in errors:
            print(f"ERROR: {error}")
        if errors:
            print("Production validation FAILED.")
            return 1

    print("Validation SUCCESS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
