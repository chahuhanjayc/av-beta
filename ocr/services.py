"""
ocr/services.py

High-level OCR inventory integration services.

Functions:
  match_line_items_to_stock(company, line_items)
      → Fuzzy-matches extracted OCR line items to existing StockItems.
        Uses rapidfuzz (falls back to difflib if unavailable).
        Confidence tiers:
          > 85  → "high"   (auto-select)
          60-85 → "medium" (show suggestions)
          < 60  → "low"    (prompt to create)

  quick_create_stock_item(company, data, user)
      → Creates a new StockItem from OCR-extracted data.
        Returns (StockItem, created: bool).
        Logs to AuditLog.

  build_stock_lines_from_confirmed(company, confirmed_items)
      → Converts confirmed_items list from POST data into
        [{"stock_item": StockItem, "quantity": Decimal, "rate": Decimal}, ...]
        for use by inventory.stock_utils.process_purchase_stock / process_sales_stock.
"""

import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Fuzzy matching helpers
# ─────────────────────────────────────────────────────────────────────────────

try:
    from rapidfuzz import fuzz, process as rfprocess
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False
    import difflib


def _fuzzy_score(query: str, candidate: str) -> float:
    """Return a 0-100 similarity score between two strings."""
    if _RAPIDFUZZ_AVAILABLE:
        # token_set_ratio handles word-order differences well for product names
        return fuzz.token_set_ratio(query.lower(), candidate.lower())
    else:
        return difflib.SequenceMatcher(
            None, query.lower(), candidate.lower()
        ).ratio() * 100


def _best_matches(query: str, choices: list[dict], limit: int = 3) -> list[dict]:
    """
    Return up to `limit` best-matching StockItem dicts from `choices`.
    Each choice dict: {"id": pk, "name": str, "unit": str, "purchase_price": str,
                       "selling_price": str, "hsn_code": str, "tax_rate_pct": str}
    Returns list of (score, choice_dict) sorted descending.
    """
    scored = []
    for item in choices:
        score = _fuzzy_score(query, item["name"])
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# Main matching function
# ─────────────────────────────────────────────────────────────────────────────

def match_line_items_to_stock(company, line_items: list) -> list:
    """
    Fuzzy-match each extracted line item against the company's active StockItems.

    Adds these keys to each item dict:
      match_confidence  → "high" | "medium" | "low"
      match_score       → 0-100 (best score found)
      matched_item_id   → StockItem.pk or None
      matched_item_name → StockItem.name or ""
      matched_item_unit → StockItem.unit or ""
      suggestions       → list of up to 3 candidate dicts (for medium confidence)
      hsn               → pre-filled from matched item if available
      tax_rate          → pre-filled from matched item if available
    """
    try:
        from inventory.models import StockItem, HSN_SAC, TaxRate
    except ImportError:
        for item in line_items:
            item.setdefault("match_confidence", "low")
            item.setdefault("match_score", 0)
            item.setdefault("matched_item_id", None)
            item.setdefault("matched_item_name", "")
            item.setdefault("matched_item_unit", "")
            item.setdefault("suggestions", [])
        return line_items

    # Fetch all active stock items for this company in one query
    stock_qs = StockItem.objects.filter(
        company=company, is_active=True
    ).select_related("hsn_sac", "tax_rate").values(
        "id", "name", "unit", "purchase_price", "selling_price",
        "hsn_sac__code", "tax_rate__rate",
    )
    candidates = [
        {
            "id":             s["id"],
            "name":           s["name"],
            "unit":           s["unit"],
            "purchase_price": str(s["purchase_price"]),
            "selling_price":  str(s["selling_price"]),
            "hsn_code":       s["hsn_sac__code"] or "",
            "tax_rate_pct":   str(s["tax_rate__rate"]) if s["tax_rate__rate"] else "",
        }
        for s in stock_qs
    ]

    enriched = []
    for item in line_items:
        name = item.get("name", "").strip()
        result = dict(item)  # copy

        if not name or not candidates:
            result.update({
                "match_confidence":  "low",
                "match_score":       0,
                "matched_item_id":   None,
                "matched_item_name": "",
                "matched_item_unit": "",
                "suggestions":       [],
            })
            enriched.append(result)
            continue

        top = _best_matches(name, candidates, limit=3)
        best_score, best_item = top[0] if top else (0, {})

        if best_score >= 85:
            confidence = "high"
        elif best_score >= 60:
            confidence = "medium"
        else:
            confidence = "low"

        result["match_confidence"]  = confidence
        result["match_score"]       = round(best_score, 1)
        result["suggestions"]       = [c for _, c in top]

        if confidence in ("high", "medium"):
            result["matched_item_id"]   = best_item["id"]
            result["matched_item_name"] = best_item["name"]
            result["matched_item_unit"] = best_item["unit"]
            # Pre-fill HSN and tax if not already extracted
            if not result.get("hsn"):
                result["hsn"] = best_item["hsn_code"]
            if not result.get("tax_rate"):
                result["tax_rate"] = best_item["tax_rate_pct"]
        else:
            result["matched_item_id"]   = None
            result["matched_item_name"] = ""
            result["matched_item_unit"] = ""

        enriched.append(result)

    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# Quick-create stock item
# ─────────────────────────────────────────────────────────────────────────────

def quick_create_stock_item(company, data: dict, user=None):
    """
    Create a new StockItem from provided data.

    data keys (all optional except name):
      name, unit, hsn_code (str), tax_rate_pct (str/float),
      purchase_price (str), selling_price (str), opening_quantity (str)

    Returns (stock_item, created: bool).
    If an active item with the same name already exists, returns it (created=False).
    """
    from inventory.models import StockItem, HSN_SAC, TaxRate

    name = data.get("name", "").strip()
    if not name:
        raise ValueError("Stock item name is required.")

    # Return existing item if already exists (idempotent)
    existing = StockItem.objects.filter(
        company=company, name__iexact=name, is_active=True
    ).first()
    if existing:
        return existing, False

    # Resolve HSN/SAC
    hsn_obj = None
    hsn_code = data.get("hsn_code", "").strip()
    if hsn_code:
        hsn_obj, _ = HSN_SAC.objects.get_or_create(
            code=hsn_code,
            defaults={"description": ""},
        )

    # Resolve TaxRate
    tax_obj = None
    tax_pct_raw = data.get("tax_rate_pct", "")
    if tax_pct_raw:
        try:
            tax_pct = Decimal(str(tax_pct_raw))
            tax_obj, _ = TaxRate.objects.get_or_create(
                rate=tax_pct,
                defaults={"description": f"GST {tax_pct}%"},
            )
        except (InvalidOperation, Exception):
            pass

    def _dec(val, default="0.00"):
        try:
            return Decimal(str(val))
        except (InvalidOperation, TypeError):
            return Decimal(default)

    stock_item = StockItem.objects.create(
        company=company,
        name=name,
        unit=data.get("unit", "Nos"),
        hsn_sac=hsn_obj,
        tax_rate=tax_obj,
        purchase_price=_dec(data.get("purchase_price", "0.00")),
        selling_price=_dec(data.get("selling_price", "0.00")),
        opening_quantity=_dec(data.get("opening_quantity", "0.000"), "0.000"),
        low_stock_threshold=_dec(data.get("low_stock_threshold", "0.000"), "0.000"),
        is_active=True,
    )

    # Audit log
    try:
        from core.models import AuditLog
        AuditLog.objects.create(
            company=company,
            performed_by=user,
            action="Stock Item Created (OCR)",
            notes=(
                f"Quick-created '{stock_item.name}' "
                f"(unit={stock_item.unit}, purchase_price={stock_item.purchase_price}) "
                f"from OCR bill."
            ),
        )
    except Exception:
        pass

    logger.info("quick_create_stock_item: created '%s' for company %s", name, company)
    return stock_item, True


# ─────────────────────────────────────────────────────────────────────────────
# Convert confirmed POST items → stock_lines for stock_utils
# ─────────────────────────────────────────────────────────────────────────────

def build_stock_lines_from_confirmed(company, confirmed_items: list) -> list:
    """
    Convert confirmed_items (saved in parsed_json / POST data) to the format
    expected by inventory.stock_utils.process_purchase_stock / process_sales_stock.

    confirmed_items: list of dicts:
        [{"stock_item_id": int, "quantity": str, "rate": str, ...}, ...]

    Items with stock_item_id == 0 or None are skipped (unmatched lines).

    Returns:
        [{"stock_item": StockItem, "quantity": Decimal, "rate": Decimal}, ...]
    """
    from inventory.models import StockItem

    lines = []
    for item in (confirmed_items or []):
        sid = item.get("stock_item_id")
        if not sid:
            continue
        try:
            sid = int(sid)
            if sid <= 0:
                continue
            stock_item = StockItem.objects.get(pk=sid, company=company, is_active=True)
        except (ValueError, StockItem.DoesNotExist):
            logger.warning("build_stock_lines: stock item %s not found — skipped", sid)
            continue

        try:
            qty  = Decimal(str(item.get("quantity", "0")))
            rate = Decimal(str(item.get("rate", "0")))
        except (InvalidOperation, TypeError):
            continue

        if qty <= 0:
            continue

        lines.append({"stock_item": stock_item, "quantity": qty, "rate": rate})

    return lines
