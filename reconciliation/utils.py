import json
import logging
from decimal import Decimal
from datetime import datetime, date
from django.db import models, transaction
from django.db.models import Q
from vouchers.models import Voucher, VoucherItem
from .models import ReconciliationEntry

logger = logging.getLogger(__name__)

def realize_itc(voucher):
    """
    Creates a Journal Voucher to move GST from Suspense to Claimable.
    """
    if voucher.is_itc_claimed:
        return

    with transaction.atomic():
        # Identify suspense lines
        suspense_items = voucher.items.filter(ledger__name__icontains="Suspense")
        if not suspense_items.exists():
            return

        from vouchers.models import Voucher, VoucherItem
        from ledger.models import Ledger, AccountGroup

        # Create Journal Voucher
        jv = Voucher.objects.create(
            company=voucher.company,
            voucher_type="Journal",
            date=date.today(),
            narration=f"ITC Realized for Invoice {voucher.number} via GSTR-2B Recon"
        )

        for s_item in suspense_items:
            # Create matching 'Claimable' ledger
            claimable_name = s_item.ledger.name.replace(" Suspense", "")
            claimable_ledger, _ = Ledger.objects.get_or_create(
                company=voucher.company,
                name=claimable_name,
                defaults={"account_group": s_item.ledger.account_group}
            )

            # DR Claimable (Realize ITC)
            VoucherItem.objects.create(
                voucher=jv, ledger=claimable_ledger,
                entry_type='DR', amount=s_item.amount
            )
            # CR Suspense (Clear Suspense)
            VoucherItem.objects.create(
                voucher=jv, ledger=s_item.ledger,
                entry_type='CR', amount=s_item.amount
            )

        # Flag original voucher
        voucher.is_itc_claimed = True
        voucher.save(update_fields=['is_itc_claimed'])
        logger.info("ITC realized for voucher %s", voucher.number)

def run_reconciliation(company):
    """
    Match reconciliation entries against system vouchers.
    Criteria:
    - Amount (+/- 1)
    - Date
    - Reference Number (Invoice No or Ref)
    """
    entries = ReconciliationEntry.objects.filter(company=company, status__in=['MISSING_IN_BOOKS', 'MISMATCH'])
    
    for entry in entries:
        min_amt = entry.amount - Decimal("1.00")
        max_amt = entry.amount + Decimal("1.00")
        
        vouchers = Voucher.objects.filter(
            company=company,
            date=entry.date,
            number__icontains=entry.reference_number
        )
        
        match_found = False
        for v in vouchers:
            v_amt = v.total_debit() if v.voucher_type == "Sales" else v.total_credit()
            if min_amt <= v_amt <= max_amt:
                entry.status = 'MATCHED'
                entry.matched_voucher = v
                entry.save()
                match_found = True
                
                # Realize ITC if Purchase
                if v.voucher_type == "Purchase" and entry.source_type == 'GST':
                    realize_itc(v)
                break
        
        if not match_found:
            v_amt_only = Voucher.objects.filter(
                company=company,
                date=entry.date
            )
            for v in v_amt_only:
                v_amt = v.total_debit() if v.voucher_type == "Sales" else v.total_credit()
                if min_amt <= v_amt <= max_amt:
                    entry.status = 'MISMATCH'
                    entry.matched_voucher = v
                    entry.save()
                    match_found = True
                    # Optional: Realize ITC on mismatch? Usually requires manual review.
                    break
        
        if not match_found:
            entry.status = 'MISSING_IN_BOOKS'
            entry.save()

def match_gstr2b(company):
    """
    Match GSTR-2B entries against Purchase Vouchers.
    Criteria:
    1. Supplier GSTIN (via Ledger)
    2. Invoice Number (case-insensitive)
    3. Tax Amount (+/- 1 Rupee)
    """
    from .models import GSTR2BEntry
    
    unmatched_entries = GSTR2BEntry.objects.filter(company=company, matched=False)
    matches_found = 0
    
    for entry in unmatched_entries:
        # Search for vouchers that match gstin and invoice number
        vouchers = Voucher.objects.filter(
            company=company,
            voucher_type='Purchase',
            items__ledger__gstin=entry.gstin,
            number__icontains=entry.invoice_number
        ).distinct()
        
        for v in vouchers:
            # Check if tax amount matches within tolerance
            min_tax = entry.tax_amount - Decimal("1.00")
            max_tax = entry.tax_amount + Decimal("1.00")
            
            if min_tax <= v.total_tax <= max_tax:
                entry.matched = True
                entry.matched_voucher = v
                entry.save()
                
                # Realize ITC
                realize_itc(v)
                matches_found += 1
                break
                
    return matches_found

def match_bank_entries(company):
    """
    Match bank entries against Vouchers.
    Criteria:
    1. Exact Amount
    2. Date +/- 2 days
    """
    from .models import BankEntry
    from vouchers.models import Voucher
    from datetime import timedelta
    from django.db.models import Sum
    
    unmatched_entries = BankEntry.objects.filter(company=company, matched=False)
    matches_found = 0
    
    for entry in unmatched_entries:
        min_date = entry.date - timedelta(days=2)
        max_date = entry.date + timedelta(days=2)
        
        # Look for Payment, Receipt or Contra vouchers (bank-related)
        # Contra is also used for bank-to-bank or cash-to-bank
        vouchers = Voucher.objects.filter(
            company=company,
            voucher_type__in=['Payment', 'Receipt', 'Contra'],
            date__range=(min_date, max_date)
        )
        
        for v in vouchers:
            # Check amount. Since vouchers are balanced, total_debit() == total_credit()
            if v.total_debit() == abs(entry.amount):
                entry.matched = True
                entry.matched_voucher = v
                entry.save()
                matches_found += 1
                break
                
    return matches_found

def import_bank_from_csv(company, csv_file):
    """
    Imports bank entries from a CSV file.
    Expected headers: date, amount, description
    """
    import csv
    import io
    from datetime import datetime
    from .models import BankEntry

    if hasattr(csv_file, 'read'):
        # It's a file object
        content = csv_file.read()
        if isinstance(content, bytes):
            content = content.decode('utf-8')
        reader = csv.DictReader(io.StringIO(content))
        return _process_rows(company, reader)
    else:
        # It's likely a path
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)
            return _process_rows(company, reader)

def _process_rows(company, reader):
    from .models import BankEntry
    count = 0
    for row in reader:
        try:
            # Clean amount: handle commas or currency symbols if any
            amount_str = row['amount'].replace(',', '').strip()
            amount = Decimal(amount_str)
            
            # Handle date formats
            try:
                dt = datetime.strptime(row['date'], '%Y-%m-%d').date()
            except ValueError:
                dt = datetime.strptime(row['date'], '%d/%m/%Y').date()

            BankEntry.objects.create(
                company=company,
                date=dt,
                amount=amount,
                description=row.get('description', '')
            )
            count += 1
        except (ValueError, KeyError) as e:
            logger.warning("Skipping bank import row %s: %s", row, e)
            continue
    return count

def import_gstr2b_json(company, json_data):
    """
    Dummy importer for GSTR-2B JSON.
    """
    import json
    data = json.loads(json_data)
    for inv in data.get('invoices', []):
        ReconciliationEntry.objects.create(
            company=company,
            source_type='GST',
            date=datetime.strptime(inv['date'], '%Y-%m-%d').date(),
            reference_number=inv['invoice_no'],
            amount=Decimal(inv['total_amount'])
        )
