import re


PHONE_DIGIT_RE = re.compile(r"\D+")


def normalize_phone_number(value, *, default_country_code="91"):
    """
    Store phone numbers in E.164-like form so webhook lookups are stable.
    Indian 10-digit numbers are treated as +91 numbers by default.
    """
    raw = (value or "").strip()
    if not raw:
        return None

    digits = PHONE_DIGIT_RE.sub("", raw)
    if raw.startswith("+"):
        normalized_digits = digits
    elif len(digits) == 10:
        normalized_digits = f"{default_country_code}{digits}"
    elif len(digits) == 11 and digits.startswith("0"):
        normalized_digits = f"{default_country_code}{digits[1:]}"
    else:
        normalized_digits = digits

    if len(normalized_digits) < 10 or len(normalized_digits) > 15:
        raise ValueError("Enter a valid WhatsApp phone number.")

    return f"+{normalized_digits}"
