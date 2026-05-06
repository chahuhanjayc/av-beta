"""
vouchers/utils.py

Utility helpers for the vouchers app.

generate_upi_qr(voucher)
    Builds a standard UPI deep-link URI for a Sales voucher and returns it as a
    Base64-encoded PNG string suitable for embedding in an <img> tag:

        <img src="data:image/png;base64,{{ qr_code }}">

    Returns None gracefully when:
      - the company has no UPI ID configured
      - the qrcode library is not installed
      - any other error occurs during QR generation
"""

import base64
import io
from urllib.parse import quote


def generate_upi_qr(voucher):
    """
    Generate a Base64-encoded UPI QR code PNG for the given voucher.

    UPI URI format (NPCI specification):
        upi://pay?pa=<UPI_ID>&pn=<PAYEE_NAME>&am=<AMOUNT>&tn=<TXN_NOTE>&cu=INR

    Args:
        voucher: a Voucher model instance (should be of type "Sales").

    Returns:
        str  – Base64-encoded PNG image data, or
        None – if UPI ID is not set or any error occurs.
    """
    company = voucher.company

    # ── Guard: UPI ID must be configured ────────────────────────────────────
    upi_id = getattr(company, "upi_id", None)
    if not upi_id:
        return None

    try:
        import qrcode
        import qrcode.constants

        # ── Build the UPI payment URI ────────────────────────────────────────
        amount = voucher.total_debit()          # total invoice value
        payee_name = quote(company.name)        # URL-safe company name
        txn_note   = quote(voucher.number)      # e.g. "ABC2526-00001"

        upi_uri = (
            f"upi://pay"
            f"?pa={upi_id}"
            f"&pn={payee_name}"
            f"&am={amount:.2f}"
            f"&tn={txn_note}"
            f"&cu=INR"
        )

        # ── Generate QR code ─────────────────────────────────────────────────
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=4,
        )
        qr.add_data(upi_uri)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")

        # ── Encode to Base64 ─────────────────────────────────────────────────
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    except Exception:
        # Never crash a page because of QR generation — fail silently
        return None
