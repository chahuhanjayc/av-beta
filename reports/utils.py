"""
reports/utils.py

Pure calculation functions for all financial reports.
All functions receive a Company object and date parameters.
All return plain Python dicts/lists — no ORM objects passed to templates directly.
"""

import re
from decimal import Decimal
from datetime import date, timedelta

from django.db.models import Sum, Q, DecimalField
from django.db.models.functions import TruncMonth, Coalesce
from django.utils import timezone

from ledger.models import Ledger
from vouchers.models import Voucher, VoucherItem

ZERO = Decimal("0.00")
MSME_INTEREST_RATE = Decimal("0.18")

# Keywords used to identify GST/tax ledgers by name
GST_KEYWORDS = ("CGST", "SGST", "IGST", "UTGST", "GST", "VAT", "TAX PAYABLE", "INPUT TAX")
GST_UQC_MAP = {
    "Nos": "NOS",
    "Kgs": "KGS",
    "Boxes": "BOX",
    "Dozen": "DOZ",
    "Meters": "MTR",
    "Pieces": "PCS",
}

# GSTIN regex pattern
_GSTIN_RE = re.compile(r'\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b')


# ─────────────────────────────────────────────────────────────────────────────
# SHARED HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _coerce(val):
    """Coerce None / float to Decimal safely."""
    if val is None:
        return ZERO
    return Decimal(str(val))


def _ledger_net(ledger, start_date=None, end_date=None):
    """Return (total_dr, total_cr) for a ledger within an optional date range."""
    qs = VoucherItem.objects.filter(ledger=ledger, voucher__status='APPROVED')
    if start_date:
        qs = qs.filter(voucher__date__gte=start_date)
    if end_date:
        qs = qs.filter(voucher__date__lte=end_date)
    agg = qs.aggregate(
        total_dr=Sum("amount", filter=Q(entry_type='DR')),
        total_cr=Sum("amount", filter=Q(entry_type='CR'))
    )
    return _coerce(agg["total_dr"]), _coerce(agg["total_cr"])


def _ledger_signed_balance(ledger, start_date=None, end_date=None):
    """
    Return the signed accounting balance for a ledger.

    Convention used across the app:
    - positive = credit balance
    - negative = debit balance
    """
    dr, cr = _ledger_net(ledger, start_date=start_date, end_date=end_date)
    return ledger.opening_balance + cr - dr


def _stock_value(company, as_of_date):
    """Return closing stock value as of a date using the inventory valuation report logic."""
    try:
        from inventory.models import StockItem
    except Exception:
        return ZERO

    total = ZERO
    for item in StockItem.objects.filter(company=company, is_active=True):
        total += item.closing_stock_value(end_date=as_of_date)
    return total.quantize(Decimal("0.01"))


def _is_gst_ledger(ledger):
    """
    Return True if the ledger represents a GST/tax component.
    """
    name_upper = ledger.name.upper()
    return ledger.account_group.nature == "Tax" or any(kw in name_upper for kw in GST_KEYWORDS)


def _voucher_party_ledger(voucher, *, entry_type, preferred_nature):
    fallback = None
    for item in voucher.items.all():
        if item.entry_type != entry_type:
            continue
        ledger = item.ledger
        if _is_gst_ledger(ledger):
            continue
        if fallback is None:
            fallback = ledger
        if ledger.account_group.nature == preferred_nature:
            return ledger
    return fallback


def _voucher_party_gstin(voucher, own_gstin):
    party = _voucher_party_ledger(voucher, entry_type="DR", preferred_nature="Asset")
    party_gstin = (party.gstin or "").strip().upper() if party and party.gstin else ""
    if party_gstin and party_gstin != own_gstin and _GSTIN_RE.fullmatch(party_gstin):
        return party_gstin, party.name
    if voucher.narration:
        match = _GSTIN_RE.search(voucher.narration.upper())
        if match and match.group(1) != own_gstin:
            return match.group(1), party.name if party else ""
    return None, party.name if party else ""


def _gst_rate(taxable_value, total_tax):
    if taxable_value <= ZERO or total_tax <= ZERO:
        return ZERO
    return ((total_tax / taxable_value) * Decimal("100")).quantize(Decimal("0.01"))


def _money(value):
    return value.quantize(Decimal("0.01"))


def get_msme_payable_watch(company, as_of_date=None, limit=100):
    as_of_date = as_of_date or timezone.localdate()
    vouchers = (
        Voucher.objects.filter(
            company=company,
            voucher_type="Purchase",
            status="APPROVED",
            outstanding_amount__gt=0,
            items__ledger__is_msme=True,
            date__lte=as_of_date,
        )
        .distinct()
        .prefetch_related("items__ledger__account_group")
        .order_by("date", "id")
    )

    rows = []
    overdue_count = due_soon_count = open_count = 0
    overdue_amount = due_soon_amount = total_outstanding = ZERO
    interest_liability = ZERO
    for voucher in vouchers:
        vendor = next((item.ledger for item in voucher.items.all() if item.ledger.is_msme), None)
        credit_days = 45
        if vendor and vendor.credit_days is not None:
            credit_days = min(max(vendor.credit_days, 0), 45)
        statutory_due_date = voucher.date + timedelta(days=credit_days)
        due_date = min(voucher.due_date, statutory_due_date) if voucher.due_date else statutory_due_date
        days_outstanding = (as_of_date - voucher.date).days
        days_to_due = (due_date - as_of_date).days
        days_overdue = max((as_of_date - due_date).days, 0)
        amount = voucher.outstanding_amount or ZERO
        total_outstanding += amount

        status = "open"
        row_interest = ZERO
        if days_overdue > 0:
            status = "overdue"
            overdue_count += 1
            overdue_amount += amount
            row_interest = (amount * MSME_INTEREST_RATE * Decimal(days_overdue)) / Decimal("365")
            interest_liability += row_interest
        elif days_to_due <= 7:
            status = "due_soon"
            due_soon_count += 1
            due_soon_amount += amount
        else:
            open_count += 1

        rows.append({
            "voucher": voucher,
            "vendor": vendor,
            "due_date": due_date,
            "statutory_due_date": statutory_due_date,
            "days_outstanding": days_outstanding,
            "days_to_due": days_to_due,
            "days_overdue": days_overdue,
            "outstanding_amount": amount,
            "interest_liability": row_interest,
            "status": status,
        })

    rows.sort(key=lambda row: (row["due_date"], row["voucher"].pk))
    return {
        "rows": rows[:limit],
        "summary": {
            "total_count": len(rows),
            "open_count": open_count,
            "overdue_count": overdue_count,
            "due_soon_count": due_soon_count,
            "total_outstanding": total_outstanding,
            "overdue_amount": overdue_amount,
            "due_soon_amount": due_soon_amount,
            "interest_liability": interest_liability,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# PROFIT & LOSS
# ─────────────────────────────────────────────────────────────────────────────

def get_profit_loss(company, start_date, end_date):
    """
    Returns:
        income_items:  [{'name', 'amount'}]
        expense_items: [{'name', 'amount'}]
        total_income, total_expense, net_profit: Decimal
    """
    base_qs = VoucherItem.objects.filter(
        ledger__company=company,
        voucher__date__gte=start_date,
        voucher__date__lte=end_date,
        voucher__status='APPROVED',
    )

    income_rows = (
        base_qs.filter(ledger__account_group__nature="Income")
        .values("ledger__pk", "ledger__name")
        .annotate(
            total_dr=Sum("amount", filter=Q(entry_type='DR')),
            total_cr=Sum("amount", filter=Q(entry_type='CR'))
        )
        .order_by("ledger__name")
    )
    income_items, total_income = [], ZERO
    for row in income_rows:
        net = _coerce(row["total_cr"]) - _coerce(row["total_dr"])
        income_items.append({"name": row["ledger__name"], "amount": net})
        total_income += net

    expense_rows = (
        base_qs.filter(ledger__account_group__nature="Expense")
        .values("ledger__pk", "ledger__name")
        .annotate(
            total_dr=Sum("amount", filter=Q(entry_type='DR')),
            total_cr=Sum("amount", filter=Q(entry_type='CR'))
        )
        .order_by("ledger__name")
    )
    expense_items, total_expense = [], ZERO
    for row in expense_rows:
        net = _coerce(row["total_dr"]) - _coerce(row["total_cr"])
        expense_items.append({"name": row["ledger__name"], "amount": net})
        total_expense += net

    opening_stock = _stock_value(company, start_date - timedelta(days=1))
    closing_stock = _stock_value(company, end_date)

    if opening_stock != ZERO:
        expense_items.append({"name": "Opening Stock", "amount": opening_stock, "is_stock_adjustment": True})
        total_expense += opening_stock

    if closing_stock != ZERO:
        income_items.append({"name": "Closing Stock", "amount": closing_stock, "is_stock_adjustment": True})
        total_income += closing_stock

    return {
        "income_items":  income_items,
        "expense_items": expense_items,
        "total_income":  total_income,
        "total_expense": total_expense,
        "net_profit":    total_income - total_expense,
        "opening_stock": opening_stock,
        "closing_stock": closing_stock,
    }


# ─────────────────────────────────────────────────────────────────────────────
# BALANCE SHEET
# ─────────────────────────────────────────────────────────────────────────────

def get_balance_sheet(company, as_of_date):
    """
    Returns:
        asset_items, liability_items: [{'name', 'balance'}]
        total_assets, total_liabilities, difference: Decimal
    """
    asset_items, total_assets = [], ZERO
    liability_items, total_liabilities = [], ZERO

    balance_sheet_ledgers = Ledger.objects.filter(
        company=company,
        account_group__nature__in=("Asset", "Liability", "Equity", "Tax"),
    ).select_related("account_group").order_by("account_group__nature", "name")

    for ledger in balance_sheet_ledgers:
        signed_balance = _ledger_signed_balance(ledger, end_date=as_of_date)
        nature = ledger.account_group.nature

        if nature == "Asset":
            # Debit asset balances are shown on the asset side. A credit asset balance
            # is still shown here as negative so the balance equation remains explicit.
            balance = -signed_balance
            asset_items.append({"name": ledger.name, "balance": balance})
            total_assets += balance
        elif nature in ("Liability", "Equity"):
            liability_items.append({"name": ledger.name, "balance": signed_balance})
            total_liabilities += signed_balance
        elif nature == "Tax":
            if signed_balance < ZERO:
                balance = -signed_balance
                asset_items.append({"name": ledger.name, "balance": balance, "is_tax": True})
                total_assets += balance
            elif signed_balance > ZERO:
                liability_items.append({"name": ledger.name, "balance": signed_balance, "is_tax": True})
                total_liabilities += signed_balance

    # Retained Earnings (Net Profit) goes on Liabilities + Equity side
    inc_agg = VoucherItem.objects.filter(
        ledger__company=company, ledger__account_group__nature="Income",
        voucher__date__lte=as_of_date,
        voucher__status='APPROVED',
    ).aggregate(
        cr=Sum("amount", filter=Q(entry_type='CR')), 
        dr=Sum("amount", filter=Q(entry_type='DR'))
    )
    exp_agg = VoucherItem.objects.filter(
        ledger__company=company, ledger__account_group__nature="Expense",
        voucher__date__lte=as_of_date,
        voucher__status='APPROVED',
    ).aggregate(
        cr=Sum("amount", filter=Q(entry_type='CR')), 
        dr=Sum("amount", filter=Q(entry_type='DR'))
    )

    retained = (
        (_coerce(inc_agg["cr"]) - _coerce(inc_agg["dr"]))
        - (_coerce(exp_agg["dr"]) - _coerce(exp_agg["cr"]))
    )
    closing_stock = _stock_value(company, as_of_date)
    if closing_stock != ZERO:
        asset_items.append({
            "name": "Closing Stock",
            "balance": closing_stock,
            "is_stock_adjustment": True,
        })
        total_assets += closing_stock
        retained += closing_stock

    if retained != ZERO:
        liability_items.append({
            "name": "Retained Earnings (Net Profit)",
            "balance": retained,
            "is_retained": True,
        })
        total_liabilities += retained

    return {
        "asset_items":       asset_items,
        "liability_items":   liability_items,
        "total_assets":      total_assets,
        "total_liabilities": total_liabilities,
        "difference":        total_assets - total_liabilities,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TRIAL BALANCE
# ─────────────────────────────────────────────────────────────────────────────

def get_trial_balance(company, start_date, end_date):
    """
    Optimized Trial Balance:
    Uses conditional annotations to calculate all ledger totals in a single query.
    """
    ledgers = Ledger.objects.filter(company=company, is_active=True).order_by("account_group__nature", "name").annotate(
        pre_dr=Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='DR', voucher_items__voucher__date__lt=start_date, voucher_items__voucher__status='APPROVED')),
        pre_cr=Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='CR', voucher_items__voucher__date__lt=start_date, voucher_items__voucher__status='APPROVED')),
        per_dr=Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='DR', voucher_items__voucher__date__gte=start_date, voucher_items__voucher__date__lte=end_date, voucher_items__voucher__status='APPROVED')),
        per_cr=Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='CR', voucher_items__voucher__date__gte=start_date, voucher_items__voucher__date__lte=end_date, voucher_items__voucher__status='APPROVED')),
    )

    rows = []
    tot_open_dr = tot_open_cr = ZERO
    tot_per_dr  = tot_per_cr  = ZERO
    tot_clos_dr = tot_clos_cr = ZERO

    for ledger in ledgers:
        pre_dr = _coerce(ledger.pre_dr)
        pre_cr = _coerce(ledger.pre_cr)
        period_dr = _coerce(ledger.per_dr)
        period_cr = _coerce(ledger.per_cr)

        opening = ledger.opening_balance + pre_cr - pre_dr
        opening_cr = max(opening, ZERO)
        opening_dr = max(-opening, ZERO)
        closing = opening + period_cr - period_dr
        closing_cr = max(closing, ZERO)
        closing_dr = max(-closing, ZERO)

        tot_open_dr += opening_dr
        tot_open_cr += opening_cr
        tot_per_dr  += period_dr
        tot_per_cr  += period_cr
        tot_clos_dr += closing_dr
        tot_clos_cr += closing_cr

        rows.append({
            "name":       ledger.name,
            "group":      ledger.account_group.name,
            "opening_dr": opening_dr,
            "opening_cr": opening_cr,
            "period_dr":  period_dr,
            "period_cr":  period_cr,
            "closing_dr": closing_dr,
            "closing_cr": closing_cr,
        })

    is_balanced = abs(tot_per_dr - tot_per_cr) < Decimal("0.01")

    return {
        "rows":           rows,
        "tot_open_dr":    tot_open_dr,
        "tot_open_cr":    tot_open_cr,
        "tot_per_dr":     tot_per_dr,
        "tot_per_cr":     tot_per_cr,
        "tot_clos_dr":    tot_clos_dr,
        "tot_clos_cr":    tot_clos_cr,
        "is_balanced":    is_balanced,
        "difference":     abs(tot_per_dr - tot_per_cr),
    }


# ─────────────────────────────────────────────────────────────────────────────
# GST REPORT  (GSTR-1 + GSTR-3B summary)
# ─────────────────────────────────────────────────────────────────────────────

def get_gst_report(company, start_date, end_date):
    """
    GSTR-1:  Outward supplies (Sales vouchers) — taxable value + output tax.
    GSTR-3B: Input Tax Credit from Purchases vs Output Tax Payable → Net liability.
    """
    own_gstin = (company.gstin or "").strip().upper()

    # ── GSTR-1: Sales ─────────────────────────────────────────────────────────
    sales_vouchers = (
        Voucher.objects.filter(
            company=company,
            voucher_type="Sales",
            date__gte=start_date,
            date__lte=end_date,
            status='APPROVED',
        )
        .prefetch_related(
            "items__ledger__account_group",
            "items__stock_item__hsn_sac",
            "items__stock_item__tax_rate",
        )
        .order_by("date")
    )

    gstr1_rows = []
    hsn_summary_map = {}
    tot_taxable_sales = tot_out_cgst = tot_out_sgst = tot_out_igst = ZERO

    for v in sales_vouchers:
        taxable_value = cgst = sgst = igst = other_gst = ZERO
        stock_income_lines = []
        voucher_number = v.number or str(v.pk)

        for item in v.items.all():
            if _is_gst_ledger(item.ledger):
                name_upper = item.ledger.name.upper()
                amt = item.amount if item.entry_type == 'CR' else -item.amount
                if "CGST" in name_upper:
                    cgst += amt
                elif "SGST" in name_upper or "UTGST" in name_upper:
                    sgst += amt
                elif "IGST" in name_upper:
                    igst += amt
                else:
                    other_gst += amt
            elif item.ledger.account_group.nature == "Income":
                taxable_value += item.amount if item.entry_type == 'CR' else -item.amount
                if item.stock_item_id:
                    stock_income_lines.append(item)

        total_tax = cgst + sgst + igst + other_gst

        buyer_gstin, buyer_name = _voucher_party_gstin(v, own_gstin)
        own_state = own_gstin[:2] if len(own_gstin) >= 2 else ""
        place_of_supply = (v.place_of_supply or "").strip() or (buyer_gstin[:2] if buyer_gstin else own_state) or "99"
        portal_supply_type = "INTER" if own_state and place_of_supply != own_state else "INTRA"

        gstr1_rows.append({
            "voucher_number": voucher_number,
            "date":           v.date,
            "narration":      (v.narration or "")[:60],
            "buyer_name":     buyer_name,
            "buyer_gstin":    buyer_gstin,
            "supply_type":    "B2B" if buyer_gstin else "B2C",
            "place_of_supply": place_of_supply,
            "portal_supply_type": portal_supply_type,
            "taxable_value":  taxable_value,
            "cgst":           cgst,
            "sgst":           sgst,
            "igst":           igst,
            "other_gst":      other_gst,
            "total_tax":      total_tax,
            "rate":           _gst_rate(taxable_value, total_tax),
            "invoice_value":  taxable_value + total_tax,
        })

        for item in stock_income_lines:
            line_taxable = item.amount if item.entry_type == 'CR' else -item.amount
            if line_taxable <= ZERO:
                continue

            stock_item = item.stock_item
            hsn = stock_item.hsn_sac
            hsn_code = hsn.code if hsn else ""
            description = (hsn.description if hsn and hsn.description else stock_item.name)[:255]
            uqc = GST_UQC_MAP.get(stock_item.unit, (stock_item.unit or "OTH").upper())
            share = line_taxable / taxable_value if taxable_value > ZERO else ZERO
            line_cgst = _money(cgst * share)
            line_sgst = _money(sgst * share)
            line_igst = _money(igst * share)
            line_other = _money(other_gst * share)
            line_total_tax = line_cgst + line_sgst + line_igst + line_other
            rate = _gst_rate(line_taxable, line_total_tax)
            key = (hsn_code, uqc, rate)

            if key not in hsn_summary_map:
                hsn_summary_map[key] = {
                    "hsn_code": hsn_code,
                    "description": description,
                    "uqc": uqc,
                    "rate": rate,
                    "quantity": ZERO,
                    "taxable_value": ZERO,
                    "cgst": ZERO,
                    "sgst": ZERO,
                    "igst": ZERO,
                    "other_gst": ZERO,
                    "total_tax": ZERO,
                    "total_value": ZERO,
                    "missing_hsn": not bool(hsn_code),
                }

            row = hsn_summary_map[key]
            row["quantity"] += item.quantity or ZERO
            row["taxable_value"] += line_taxable
            row["cgst"] += line_cgst
            row["sgst"] += line_sgst
            row["igst"] += line_igst
            row["other_gst"] += line_other
            row["total_tax"] += line_total_tax
            row["total_value"] += line_taxable + line_total_tax

        tot_taxable_sales += taxable_value
        tot_out_cgst += cgst
        tot_out_sgst += sgst
        tot_out_igst += igst

    tot_out_tax = tot_out_cgst + tot_out_sgst + tot_out_igst

    # ── GSTR-3B: ITC ─────────────────────────────────────────────────────────
    purchase_vouchers = (
        Voucher.objects.filter(
            company=company,
            voucher_type="Purchase",
            date__gte=start_date,
            date__lte=end_date,
            status='APPROVED',
        )
        .prefetch_related("items__ledger__account_group")
        .order_by("date")
    )

    itc_cgst = itc_sgst = itc_igst = itc_other = ZERO
    tot_taxable_purchases = ZERO

    for v in purchase_vouchers:
        for item in v.items.all():
            if _is_gst_ledger(item.ledger):
                name_upper = item.ledger.name.upper()
                amt = item.amount if item.entry_type == 'DR' else -item.amount
                if amt <= ZERO:
                    continue
                if "CGST" in name_upper:
                    itc_cgst += amt
                elif "SGST" in name_upper or "UTGST" in name_upper:
                    itc_sgst += amt
                elif "IGST" in name_upper:
                    itc_igst += amt
                else:
                    itc_other += amt
            elif item.ledger.account_group.nature == "Expense":
                amt = item.amount if item.entry_type == 'DR' else -item.amount
                if amt > ZERO:
                    tot_taxable_purchases += amt

    tot_itc = itc_cgst + itc_sgst + itc_igst + itc_other
    net_tax_payable = tot_out_tax - tot_itc

    b2b_rows = [r for r in gstr1_rows if r["supply_type"] == "B2B"]
    b2c_rows = [r for r in gstr1_rows if r["supply_type"] == "B2C"]
    b2cl_threshold = Decimal("100000.00") if end_date >= date(2024, 8, 1) else Decimal("250000.00")
    b2cl_rows = [
        r for r in b2c_rows
        if r["portal_supply_type"] == "INTER" and r["invoice_value"] > b2cl_threshold
    ]
    b2cs_rows = [
        r for r in b2c_rows
        if not (r["portal_supply_type"] == "INTER" and r["invoice_value"] > b2cl_threshold)
    ]
    for row in b2b_rows:
        row["gstr1_bucket"] = "B2B"
    for row in b2cl_rows:
        row["gstr1_bucket"] = "B2CL"
    for row in b2cs_rows:
        row["gstr1_bucket"] = "B2CS"
    doc_issue_summary = {
        "document_type": "Invoices for outward supply",
        "from_number": gstr1_rows[0]["voucher_number"] if gstr1_rows else "",
        "to_number": gstr1_rows[-1]["voucher_number"] if gstr1_rows else "",
        "total_number": len(gstr1_rows),
        "cancelled": 0,
        "net_issued": len(gstr1_rows),
    }
    hsn_summary_rows = sorted(
        hsn_summary_map.values(),
        key=lambda row: (row["missing_hsn"], row["hsn_code"] or "ZZZ", row["uqc"], row["rate"]),
    )
    missing_hsn_rows = [row for row in hsn_summary_rows if row["missing_hsn"]]

    return {
        # GSTR-1
        "gstr1_rows":            gstr1_rows,
        "b2b_rows":              b2b_rows,
        "b2c_rows":              b2c_rows,
        "b2cl_rows":             b2cl_rows,
        "b2cs_rows":             b2cs_rows,
        "b2cl_threshold":        b2cl_threshold,
        "doc_issue_summary":     doc_issue_summary,
        "hsn_summary_rows":      hsn_summary_rows,
        "missing_hsn_rows":      missing_hsn_rows,
        "tot_taxable_sales":     tot_taxable_sales,
        "tot_out_cgst":          tot_out_cgst,
        "tot_out_sgst":          tot_out_sgst,
        "tot_out_igst":          tot_out_igst,
        "tot_out_tax":           tot_out_tax,
        # GSTR-3B
        "itc_cgst":              itc_cgst,
        "itc_sgst":              itc_sgst,
        "itc_igst":              itc_igst,
        "itc_other":             itc_other,
        "tot_itc":               tot_itc,
        "tot_taxable_purchases": tot_taxable_purchases,
        "net_tax_payable":       net_tax_payable,
    }


# ─────────────────────────────────────────────────────────────────────────────
# RECEIVABLES AGING
# ─────────────────────────────────────────────────────────────────────────────

def get_receivables_aging(company, as_of_date):
    """
    Calculates outstanding receivables grouped by aging buckets.
    """
    sales_vouchers = (
        Voucher.objects.filter(
            company=company, voucher_type="Sales", date__lte=as_of_date,
            status='APPROVED',
        )
        .prefetch_related("items__ledger__account_group")
        .order_by("date")
    )

    buckets = {"current": [], "thirty": [], "sixty": [], "ninety": []}
    totals  = {"current": ZERO, "thirty": ZERO, "sixty": ZERO, "ninety": ZERO}

    for voucher in sales_vouchers:
        receivable_item = next(
            (
                item for item in voucher.items.all()
                if item.ledger.account_group.nature == "Asset" and item.entry_type == 'DR'
            ),
            None,
        )
        if not receivable_item:
            continue

        outstanding = _coerce(
            voucher.calculate_outstanding(as_of_date=as_of_date, approved_only=True)
        )
        if outstanding <= ZERO:
            continue

        due = voucher.due_date or (voucher.date + timedelta(days=30))
        days_overdue = max(0, (as_of_date - due).days)
        customer_name = (voucher.narration[:40] if voucher.narration else receivable_item.ledger.name)
        original = voucher.total_amount()
        settled = voucher.amount_settled(as_of_date=as_of_date, approved_only=True)
        if days_overdue > 90:
            priority = "critical"
        elif days_overdue > 60:
            priority = "high"
        elif days_overdue > 30:
            priority = "medium"
        else:
            priority = "normal"

        entry = {
            "voucher":       voucher,
            "customer_name": customer_name,
            "ledger_name":   receivable_item.ledger.name,
            "customer_email": receivable_item.ledger.email or "",
            "due_date":      due,
            "original":      original,
            "settled":       settled,
            "outstanding":   outstanding,
            "days_overdue":  days_overdue,
            "days_to_due":   (due - as_of_date).days,
            "priority":      priority,
        }

        if days_overdue <= 30:
            buckets["current"].append(entry); totals["current"] += outstanding
        elif days_overdue <= 60:
            buckets["thirty"].append(entry);  totals["thirty"]  += outstanding
        elif days_overdue <= 90:
            buckets["sixty"].append(entry);   totals["sixty"]   += outstanding
        else:
            buckets["ninety"].append(entry);  totals["ninety"]  += outstanding

    totals["grand"] = sum(totals[k] for k in ("current", "thirty", "sixty", "ninety"))
    return {"buckets": buckets, "totals": totals}


# ─────────────────────────────────────────────────────────────────────────────
# CASH FLOW
# ─────────────────────────────────────────────────────────────────────────────

def get_monthly_cash_flow(company, months=12):
    """
    Monthly inflow/outflow logic.
    """
    cutoff = date.today().replace(day=1)
    m = cutoff.month - months
    y = cutoff.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    start = date(y, m, 1)

    inflow_qs = (
        VoucherItem.objects.filter(
            ledger__company=company,
            voucher__date__gte=start,
            voucher__voucher_type__in=["Receipt", "Sales"],
            voucher__status='APPROVED',
            entry_type='CR'
        )
        .annotate(month=TruncMonth("voucher__date"))
        .values("month")
        .annotate(total=Sum("amount"))
        .order_by("month")
    )
    outflow_qs = (
        VoucherItem.objects.filter(
            ledger__company=company,
            voucher__date__gte=start,
            voucher__voucher_type__in=["Payment", "Purchase"],
            voucher__status='APPROVED',
            entry_type='DR'
        )
        .annotate(month=TruncMonth("voucher__date"))
        .values("month")
        .annotate(total=Sum("amount"))
        .order_by("month")
    )

    inflow_map  = {row["month"].strftime("%Y-%m"): float(row["total"] or 0) for row in inflow_qs}
    outflow_map = {row["month"].strftime("%Y-%m"): float(row["total"] or 0) for row in outflow_qs}

    labels, inflow_data, outflow_data = [], [], []
    cursor = start
    while cursor <= cutoff:
        key = cursor.strftime("%Y-%m")
        labels.append(cursor.strftime("%b %y"))
        inflow_data.append(round(inflow_map.get(key, 0.0), 2))
        outflow_data.append(round(outflow_map.get(key, 0.0), 2))
        
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)

    return {"labels": labels, "inflow": inflow_data, "outflow": outflow_data}


# ─────────────────────────────────────────────────────────────────────────────
# PROJECT P&L
# ─────────────────────────────────────────────────────────────────────────────

def get_project_pnl(company, cost_center):
    """
    Project P&L: Revenue - Direct Cost - Allocated Cost.
    Allocated cost is derived from CostAllocationRules.
    """
    from costcenter.models import CostAllocationRule, AllocationPercentage

    # 1. Direct Revenue (nature=Income) tagged to this cost center
    qs = VoucherItem.objects.filter(voucher__company=company, cost_center=cost_center, voucher__status='APPROVED')
    revenue_rows = qs.filter(ledger__account_group__nature="Income").aggregate(
        total=Sum("amount", filter=Q(entry_type='CR')) - Sum("amount", filter=Q(entry_type='DR'))
    )
    revenue = _coerce(revenue_rows["total"])

    # 2. Direct Costs (nature=Expense) tagged to this cost center
    expense_rows = qs.filter(ledger__account_group__nature="Expense").aggregate(
        total=Sum("amount", filter=Q(entry_type='DR')) - Sum("amount", filter=Q(entry_type='CR'))
    )
    direct_cost = _coerce(expense_rows["total"])

    # 3. Allocated Costs (Indirect Expenses NOT tagged, or specific tagged ones to be split)
    allocated_cost = ZERO
    
    # 3a. Fixed Percentage Allocation
    from django.db.models.functions import Coalesce
    rules = CostAllocationRule.objects.filter(company=company)
    for rule in rules:
        if rule.method == 'PERCENTAGE':
            try:
                split = rule.percentages.get(cost_center=cost_center)
                percentage = split.percentage / Decimal('100.00')
                
                # Base amount to allocate
                if rule.ledger:
                    base_agg = VoucherItem.objects.filter(
                        ledger=rule.ledger,
                        cost_center__isnull=True,
                        voucher__status='APPROVED'
                    ).aggregate(
                        total=Coalesce(Sum("amount", filter=Q(entry_type='DR')), Decimal('0.00')) - 
                              Coalesce(Sum("amount", filter=Q(entry_type='CR')), Decimal('0.00'))
                    )
                else:
                    base_agg = VoucherItem.objects.filter(
                        ledger__account_group__nature="Expense",
                        cost_center__isnull=True,
                        voucher__status='APPROVED'
                    ).aggregate(
                        total=Coalesce(Sum("amount", filter=Q(entry_type='DR')), Decimal('0.00')) - 
                              Coalesce(Sum("amount", filter=Q(entry_type='CR')), Decimal('0.00'))
                    )
                
                allocated_cost += _coerce(base_agg["total"]) * percentage
            except AllocationPercentage.DoesNotExist:
                pass
        
        elif rule.method == 'REVENUE':
            # Allocate untagged expenses based on the share of this cost center's revenue in total company revenue
            total_company_revenue_agg = VoucherItem.objects.filter(
                ledger__account_group__nature="Income",
                voucher__status='APPROVED',
                voucher__company=company
            ).aggregate(
                total=Sum("amount", filter=Q(entry_type='CR')) - Sum("amount", filter=Q(entry_type='DR'))
            )
            total_rev = _coerce(total_company_revenue_agg["total"])
            
            if total_rev > ZERO:
                share = revenue / total_rev
                
                if rule.ledger:
                    untagged_exp_agg = VoucherItem.objects.filter(
                        ledger=rule.ledger,
                        cost_center__isnull=True,
                        voucher__status='APPROVED'
                    ).aggregate(
                        total=Sum("amount", filter=Q(entry_type='DR')) - Sum("amount", filter=Q(entry_type='CR'))
                    )
                else:
                    untagged_exp_agg = VoucherItem.objects.filter(
                        ledger__account_group__nature="Expense",
                        cost_center__isnull=True,
                        voucher__status='APPROVED'
                    ).aggregate(
                        total=Sum("amount", filter=Q(entry_type='DR')) - Sum("amount", filter=Q(entry_type='CR'))
                    )
                
                allocated_cost += _coerce(untagged_exp_agg["total"]) * share

    net_profit = revenue - direct_cost - allocated_cost

    return {
        "cost_center":    cost_center,
        "revenue":        revenue,
        "direct_cost":    direct_cost,
        "allocated_cost": allocated_cost,
        "net_profit":     net_profit,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIMPLIFIED REPORT UTILITIES (Phase 5 Cleanup)
# ─────────────────────────────────────────────────────────────────────────────

def get_profit_loss_simple(company):
    """Simplified Profit & Loss calculation (Income vs Expense)."""

    ledgers = Ledger.objects.filter(company=company, account_group__nature__in=['Income', 'Expense']).annotate(
        total_dr=Coalesce(Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='DR', voucher_items__voucher__company=company, voucher_items__voucher__status='APPROVED')), Decimal('0.00'), output_field=DecimalField()),
        total_cr=Coalesce(Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='CR', voucher_items__voucher__company=company, voucher_items__voucher__status='APPROVED')), Decimal('0.00'), output_field=DecimalField())
    ).order_by('account_group__nature', 'name')

    income_items, expense_items = [], []
    total_income = total_expense = Decimal('0.00')

    for l in ledgers:
        if l.account_group.nature == 'Income':
            amount = max(l.total_cr - l.total_dr, Decimal('0.00'))
            income_items.append({'ledger_id': l.id, 'ledger_name': l.name, 'amount': amount})
            total_income += amount
        else:
            amount = max(l.total_dr - l.total_cr, Decimal('0.00'))
            expense_items.append({'ledger_id': l.id, 'ledger_name': l.name, 'amount': amount})
            total_expense += amount

    closing_stock = _stock_value(company, date.today())
    if closing_stock != ZERO:
        income_items.append({'ledger_id': None, 'ledger_name': 'Closing Stock', 'amount': closing_stock})
        total_income += closing_stock

    return {
        'income_items': income_items,
        'expense_items': expense_items,
        'total_income': total_income,
        'total_expense': total_expense,
        'net_profit_loss': total_income - total_expense,
        'closing_stock': closing_stock,
    }


def get_balance_sheet_simple(company):
    """Simplified Balance Sheet calculation."""

    ledgers = Ledger.objects.filter(company=company, account_group__nature__in=['Asset', 'Liability', 'Equity', 'Tax']).annotate(
        total_dr=Coalesce(Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='DR', voucher_items__voucher__company=company, voucher_items__voucher__status='APPROVED')), Decimal('0.00'), output_field=DecimalField()),
        total_cr=Coalesce(Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='CR', voucher_items__voucher__company=company, voucher_items__voucher__status='APPROVED')), Decimal('0.00'), output_field=DecimalField())
    ).order_by('account_group__nature', 'name')

    report_data = {
        'Asset': {'items': [], 'total': Decimal('0.00')},
        'Liability': {'items': [], 'total': Decimal('0.00')},
        'Equity': {'items': [], 'total': Decimal('0.00')},
        'Tax': {'items': [], 'total': Decimal('0.00')}
    }

    for l in ledgers:
        nature = l.account_group.nature
        signed_balance = l.opening_balance + l.total_cr - l.total_dr

        if nature == 'Asset':
            balance = -signed_balance
            report_data['Asset']['items'].append({'ledger_id': l.id, 'ledger_name': l.name, 'balance': balance})
            report_data['Asset']['total'] += balance
        elif nature in ['Liability', 'Equity']:
            report_data[nature]['items'].append({'ledger_id': l.id, 'ledger_name': l.name, 'balance': signed_balance})
            report_data[nature]['total'] += signed_balance
        elif nature == 'Tax':
            if signed_balance < ZERO:
                balance = -signed_balance
                report_data['Asset']['items'].append({'ledger_id': l.id, 'ledger_name': l.name, 'balance': balance})
                report_data['Asset']['total'] += balance
            elif signed_balance > ZERO:
                report_data['Liability']['items'].append({'ledger_id': l.id, 'ledger_name': l.name, 'balance': signed_balance})
                report_data['Liability']['total'] += signed_balance

    closing_stock = _stock_value(company, date.today())
    if closing_stock != ZERO:
        report_data['Asset']['items'].append({'ledger_id': None, 'ledger_name': 'Closing Stock', 'balance': closing_stock})
        report_data['Asset']['total'] += closing_stock

    return {
        'assets': report_data['Asset']['items'],
        'liabilities': report_data['Liability']['items'],
        'equity': report_data['Equity']['items'],
        'total_assets': report_data['Asset']['total'],
        'total_liabilities': report_data['Liability']['total'],
        'total_equity': report_data['Equity']['total'],
        'closing_stock': closing_stock,
    }


def get_trial_balance_simple(company):
    """Simplified Trial Balance calculation."""

    ledgers = Ledger.objects.filter(company=company).annotate(
        total_dr=Coalesce(Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='DR', voucher_items__voucher__company=company, voucher_items__voucher__status='APPROVED')), Decimal('0.00'), output_field=DecimalField()),
        total_cr=Coalesce(Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='CR', voucher_items__voucher__company=company, voucher_items__voucher__status='APPROVED')), Decimal('0.00'), output_field=DecimalField())
    ).order_by('account_group__nature', 'name')

    rows = []
    for l in ledgers:
        net_balance = l.opening_balance + l.total_cr - l.total_dr
        rows.append({
            'ledger_id': l.id,
            'ledger_name': l.name,
            'group': l.account_group.name,
            'opening_balance': l.opening_balance,
            'total_debit': l.total_dr,
            'total_credit': l.total_cr,
            'closing_balance': abs(net_balance),
            'balance_type': "Cr" if net_balance >= 0 else "Dr",
        })
    return rows


def get_dashboard_summary(company):
    """Aggregates key metrics for the dashboard."""
    pl = get_profit_loss_simple(company)
    tb = get_trial_balance_simple(company)

    cash_proxy = sum(
        item['closing_balance'] if item['balance_type'] == 'Dr' else -item['closing_balance']
        for item in tb if item['group'] in ['Bank Accounts', 'Cash-in-hand']
    )

    return {
        'total_revenue': pl['total_income'],
        'total_expense': pl['total_expense'],
        'net_profit':    pl['net_profit_loss'],
        'total_assets_liquidity': cash_proxy
    }


def get_group_detail(company, group_name):
    """Returns all ledgers in a group with their specific balances."""

    ledgers = Ledger.objects.filter(company=company, account_group__name=group_name).annotate(
        total_dr=Coalesce(Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='DR', voucher_items__voucher__company=company, voucher_items__voucher__status='APPROVED')), Decimal('0.00'), output_field=DecimalField()),
        total_cr=Coalesce(Sum('voucher_items__amount', filter=Q(voucher_items__entry_type='CR', voucher_items__voucher__company=company, voucher_items__voucher__status='APPROVED')), Decimal('0.00'), output_field=DecimalField())
    ).order_by('name')

    rows = []
    total_dr = total_cr = Decimal('0.00')
    for l in ledgers:
        net_balance = l.opening_balance + l.total_cr - l.total_dr
        rows.append({
            'ledger_id': l.id,
            'name': l.name,
            'opening': l.opening_balance,
            'debit': l.total_dr,
            'credit': l.total_cr,
            'closing': abs(net_balance),
            'type': "Cr" if net_balance >= 0 else "Dr",
        })
        total_dr += l.total_dr
        total_cr += l.total_cr

    return {'rows': rows, 'total_dr': total_dr, 'total_cr': total_cr}


def get_ledger_history(company, ledger_id, start_date=None, end_date=None):
    """Returns chronological transaction history for a single ledger with date filtering."""
    from django.shortcuts import get_object_or_404
    ledger = get_object_or_404(Ledger, id=ledger_id, company=company)
    
    # 1. Opening Balance (Balance before start_date)
    opening_bal = ledger.opening_balance
    if start_date:
        pre_movements = VoucherItem.objects.filter(
            ledger=ledger, 
            voucher__company=company,
            voucher__date__lt=start_date,
            voucher__status='APPROVED'
        ).aggregate(
            dr=Sum('amount', filter=Q(entry_type='DR')), 
            cr=Sum('amount', filter=Q(entry_type='CR'))
        )
        opening_bal += (_coerce(pre_movements['cr']) - _coerce(pre_movements['dr']))

    # 2. History (Within date range)
    qs = VoucherItem.objects.filter(ledger=ledger, voucher__company=company, voucher__status='APPROVED')
    if start_date:
        qs = qs.filter(voucher__date__gte=start_date)
    if end_date:
        qs = qs.filter(voucher__date__lte=end_date)
    
    items = qs.select_related('voucher').prefetch_related('voucher__items__ledger').order_by('voucher__date', 'voucher__id')

    history = []
    running_balance = opening_bal
    total_debit = total_credit = Decimal('0.00')

    for item in items:
        opposite_lines = [
            f"{line.ledger.name} ({line.entry_type} {line.amount})"
            for line in item.voucher.items.all()
            if line.pk != item.pk
        ]
        particulars = ", ".join(opposite_lines) or item.narration or item.voucher.narration

        if item.entry_type == 'CR':
            running_balance += item.amount
            total_credit += item.amount
            history_item = {'debit': ZERO, 'credit': item.amount}
        else:
            running_balance -= item.amount
            total_debit += item.amount
            history_item = {'debit': item.amount, 'credit': ZERO}
        
        history.append({
            'date': item.voucher.date,
            'voucher_id': item.voucher.id,
            'voucher_number': item.voucher.number,
            'voucher_type': item.voucher.voucher_type,
            'narration': item.narration or item.voucher.narration,
            'particulars': particulars,
            **history_item,
            'running_balance': abs(running_balance),
            'type': "Cr" if running_balance >= 0 else "Dr",
        })

    return {
        'ledger': ledger,
        'history': history,
        'opening_balance': opening_bal,
        'closing_balance': abs(running_balance),
        'closing_type': "Cr" if running_balance >= 0 else "Dr",
        'total_debit': total_debit,
        'total_credit': total_credit,
    }


def get_day_book(company, start_date=None, end_date=None):
    """
    Chronological transaction register with running balance.
    """
    qs = Voucher.objects.filter(company=company, status='APPROVED').order_by("date", "id")
    qs = qs.prefetch_related("items__ledger")

    all_rows = []
    running_bal = Decimal("0.00")
    for v in qs:
        total_amt = v.items.filter(entry_type='DR').aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
        
        party = "—"
        first_item = v.items.first()
        if first_item:
            party = first_item.ledger.name

        running_bal += total_amt
        all_rows.append({
            "id":      v.id,
            "date":    v.date,
            "number":  v.number,
            "type":    v.voucher_type,
            "party":   party,
            "amount":  total_amt,
            "balance": running_bal,
        })

    filtered = [r for r in all_rows if (not start_date or r['date'] >= start_date) and (not end_date or r['date'] <= end_date)]
    return sorted(filtered, key=lambda x: (x['date'], x['id']), reverse=True)
