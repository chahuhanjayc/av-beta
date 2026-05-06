"""
inventory/stock_utils.py

Stock ledger processing helpers for Purchase and Sales vouchers.

Design:
  • Always call inside transaction.atomic() — never standalone.
  • Uses select_for_update() on StockItem to prevent race conditions.
  • WAC (Weighted Average Cost):
      new_wac = (running_value + qty × rate) / (running_qty + qty)
  • Purchase  → positive StockLedger qty (inward)
  • Sales     → negative StockLedger qty (outward)  at current WAC rate

Called by:
  - ocr/views.py → ocr_confirm  (Purchase from OCR bill)
  - vouchers/views.py → voucher_create / voucher_edit  (manual entry)
"""

import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

logger = logging.getLogger(__name__)


def _prevents_negative_stock(stock_item):
    try:
        company_prevents_negative = stock_item.company.inventory_settings.prevent_negative_stock
    except Exception:
        company_prevents_negative = False
    return stock_item.prevent_negative_stock or company_prevents_negative


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log_audit(company, user, action: str, notes: str):
    """Write to AuditLog if the core app is available."""
    try:
        from core.models import AuditLog
        AuditLog.objects.create(
            company=company,
            performed_by=user,
            action=action,
            notes=notes,
        )
    except Exception:
        pass  # AuditLog is best-effort


# ─────────────────────────────────────────────────────────────────────────────
# Purchase stock movements
# ─────────────────────────────────────────────────────────────────────────────

def process_purchase_stock(voucher, stock_lines, user=None):
    """
    Create StockLedger inward entries for a Purchase voucher.

    Args:
        voucher:     Voucher instance (already saved).
        stock_lines: list of dicts:
                     [{"stock_item": <StockItem>, "quantity": Decimal, "rate": Decimal}, ...]
        user:        request.user — used for AuditLog (optional).

    Must be called inside transaction.atomic().
    Deletes any existing StockLedger rows for this voucher first (idempotent).
    """
    from inventory.models import StockItem, StockLedger
    from inventory.valuation_utils import rebuild_valuation_for_items

    # Idempotent: wipe previous ledger rows for this voucher
    StockLedger.objects.filter(voucher=voucher, quantity__gt=0).delete()

    for line in stock_lines:
        stock_item = line["stock_item"]
        qty  = Decimal(str(line["quantity"]))
        rate = Decimal(str(line["rate"]))

        if qty <= 0:
            continue

        # Lock the StockItem row for the duration of this transaction
        StockItem.objects.select_for_update().filter(pk=stock_item.pk).get()

        StockLedger.objects.create(
            stock_item=stock_item,
            voucher=voucher,
            date=voucher.date,
            quantity=qty,       # positive = inward
            rate=rate,
        )

        if user:
            _log_audit(
                voucher.company, user,
                "Stock Inward",
                f"Purchase: {stock_item.name} × {qty} @ ₹{rate} "
                f"via Voucher {voucher.number}",
            )

    rebuild_valuation_for_items({line["stock_item"].pk for line in stock_lines if line.get("stock_item")})

    logger.debug(
        "process_purchase_stock: %d lines for voucher %s",
        len(stock_lines), voucher.number,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sales stock movements
# ─────────────────────────────────────────────────────────────────────────────

def process_sales_stock(voucher, stock_lines, user=None):
    """
    Create StockLedger outward entries for a Sales voucher.

    Rate used for outward movement is the item's current WAC (COGS).
    The rate stored in VoucherStockItem/stock_lines is the selling rate —
    we record the WAC rate in StockLedger so COGS is correctly tracked.

    Must be called inside transaction.atomic().
    Deletes any existing outward StockLedger rows for this voucher first.
    """
    from inventory.models import StockItem, StockLedger
    from inventory.valuation_utils import rebuild_valuation_for_items

    StockLedger.objects.filter(voucher=voucher, quantity__lt=0).delete()

    for line in stock_lines:
        stock_item = line["stock_item"]
        qty  = Decimal(str(line["quantity"]))

        if qty <= 0:
            continue

        # Lock row
        locked_item = StockItem.objects.select_for_update().get(pk=stock_item.pk)

        if _prevents_negative_stock(locked_item):
            available_qty = locked_item.closing_quantity()
            if available_qty - qty < 0:
                raise ValidationError(
                    f"Insufficient stock for {locked_item.name}. "
                    f"Required: {qty}, Available: {available_qty}."
                )

        # Use WAC as the rate for outward movement (COGS)
        wac_rate = locked_item.weighted_average_cost()

        StockLedger.objects.create(
            stock_item=locked_item,
            voucher=voucher,
            date=voucher.date,
            quantity=-qty,      # negative = outward
            rate=wac_rate,
        )

        if user:
            _log_audit(
                voucher.company, user,
                "Stock Outward",
                f"Sales: {locked_item.name} × {qty} @ WAC ₹{wac_rate} "
                f"via Voucher {voucher.number}",
            )

    rebuild_valuation_for_items({line["stock_item"].pk for line in stock_lines if line.get("stock_item")})

    logger.debug(
        "process_sales_stock: %d lines for voucher %s",
        len(stock_lines), voucher.number,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dispatcher — called from views with voucher type
# ─────────────────────────────────────────────────────────────────────────────

def process_stock_for_voucher(voucher, stock_lines, user=None):
    """
    Route to purchase or sales handler based on voucher_type.
    No-op for other voucher types.
    Must be called inside transaction.atomic().
    """
    if voucher.voucher_type == "Purchase":
        process_purchase_stock(voucher, stock_lines, user=user)
    elif voucher.voucher_type == "Sales":
        process_sales_stock(voucher, stock_lines, user=user)
    else:
        logger.debug(
            "process_stock_for_voucher: skipped for type=%s", voucher.voucher_type
        )
