from decimal import Decimal
from django.db.models import Sum, Q
from .models import Voucher, VoucherItem
from ocr.models import OCRSubmission
from ledger.models import Ledger
try:
    from tds.models import TDSEntry, TDSSection
except ImportError:
    TDSEntry, TDSSection = None, None

def get_compliance_issues(company):
    """
    Analyzes all vouchers and ledgers for a company to find compliance issues.
    Returns a list of dicts: {level: 'danger'|'warning', type: str, message: str, voucher: obj|None}
    """
    issues = []
    
    # 1. High Value Vouchers without GSTIN (Possible B2B reporting error)
    # Threshold: > 50,000 (standard for some e-way bill / reporting rules)
    high_val_vouchers = Voucher.objects.filter(
        company=company, 
        voucher_type__in=['Purchase', 'Sales']
    )
    
    for v in high_val_vouchers:
        # Find the primary party ledger (the one that is not Tax or Bank/Cash)
        # For simplicity, we check if any item in this voucher links to a ledger with no GSTIN
        total = v.total_debit()
        if total > Decimal("50000.00"):
            for item in v.items.all():
                if item.ledger.account_group.nature not in ['Tax', 'Asset'] and not item.ledger.gstin:
                    # Potential issue: High value transaction with a party that has no GSTIN
                    issues.append({
                        "level": "warning",
                        "type": "Missing GSTIN",
                        "message": f"Voucher {v.number} is > ₹50k but Ledger '{item.ledger.name}' has no GSTIN.",
                        "voucher": v,
                        "id": f"gst_{v.pk}_{item.ledger.pk}"
                    })
                    break

    # 2. Potential Missing TDS (Section 194C/194J etc.)
    # Strategy: Find ledgers with nature 'Expense' where total in FY > 30,000
    # and no corresponding TDS entry exists.
    if TDSEntry and TDSSection:
        expense_ledgers = Ledger.objects.filter(company=company, account_group__nature='Expense')
        for l in expense_ledgers:
            total_expense = VoucherItem.objects.filter(
                voucher__company=company,
                ledger=l,
                entry_type='DR'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            
            if total_expense > Decimal("30000.00"):
                # Check if TDS has been deducted for this company in this FY
                has_tds = TDSEntry.objects.filter(company=company, deductee_ledger__name=l.name).exists()
                # Also check by description if we can't find direct link
                if not has_tds:
                    issues.append({
                        "level": "danger",
                        "type": "TDS Alert",
                        "message": f"Total payments to '{l.name}' exceed ₹30k (₹{total_expense:,.2f}), but no TDS deduction found.",
                        "id": f"tds_{l.pk}"
                    })

    # 3. Missing Proof (Audit Trail)
    # Vouchers > 10,000 with no OCR attachment
    proof_threshold = Decimal("10000.00")
    no_proof_vouchers = Voucher.objects.filter(
        company=company,
        voucher_type__in=['Purchase', 'Sales']
    ).exclude(ocr_source__isnull=False)
    
    for v in no_proof_vouchers:
        if v.total_debit() > proof_threshold:
            issues.append({
                "level": "warning",
                "type": "Audit Risk",
                "message": f"High value voucher {v.number} (₹{v.total_debit():,.2f}) has no digital scan attached.",
                "voucher": v,
                "id": f"proof_{v.pk}"
            })

    return issues
