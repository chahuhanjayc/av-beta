import json
from decimal import Decimal
from django.db.models import Q
from vouchers.models import Voucher

def reconcile_gstr2b(company, json_data):
    """
    GSTR-2B Reconciliation Engine.
    Matches JSON portal data against Purchase Vouchers.
    
    Criteria:
    - GSTIN (Vendor)
    - Invoice Number
    - Tax Amount
    """
    results = {
        'matched': [],
        'missing_in_books': [], # In portal, not in books
        'missing_in_portal': [] # In books, not in portal
    }
    
    # Pre-fetch all purchase vouchers with tax for this company
    vouchers = Voucher.objects.filter(
        company=company, 
        voucher_type='Purchase',
        total_tax__gt=0
    ).prefetch_related('items__ledger')
    
    matched_voucher_ids = set()
    
    # Process Portal Data
    # Expected format: [{"ctin": "GSTIN", "inum": "INV-123", "tax_amt": 500.00}, ...]
    data = json.loads(json_data) if isinstance(json_data, str) else json_data
    
    for record in data:
        p_gstin = record.get('ctin', '').upper()
        p_inum = record.get('inum', '')
        p_tax = Decimal(str(record.get('tax_amt', 0)))
        
        match_found = False
        for v in vouchers:
            # Match by Invoice Number
            if v.number.strip() == p_inum.strip():
                # Verify GSTIN of the vendor ledger
                vendor_gstin = ""
                for item in v.items.all():
                    if item.ledger.gstin:
                        vendor_gstin = item.ledger.gstin.upper()
                        break
                
                if vendor_gstin == p_gstin:
                    # Verify Tax Amount (with 1 Rupee tolerance)
                    if abs(v.total_tax - p_tax) <= Decimal('1.00'):
                        results['matched'].append({
                            'inum': p_inum,
                            'gstin': p_gstin,
                            'amount': p_tax,
                            'voucher_id': v.id,
                            'status': 'MATCHED'
                        })
                        matched_voucher_ids.add(v.id)
                        match_found = True
                        break
        
        if not match_found:
            results['missing_in_books'].append({
                'inum': p_inum,
                'gstin': p_gstin,
                'amount': p_tax,
                'status': 'MISSING_IN_BOOKS'
            })
            
    # Identify Missing in Portal
    for v in vouchers:
        if v.id not in matched_voucher_ids:
            v_gstin = ""
            for item in v.items.all():
                if item.ledger.gstin:
                    v_gstin = item.ledger.gstin.upper()
                    break
            
            results['missing_in_portal'].append({
                'inum': v.number,
                'gstin': v_gstin,
                'amount': v.total_tax,
                'voucher_id': v.id,
                'status': 'MISSING_IN_PORTAL'
            })
            
    return results

def run_sample_verification(company):
    """Verify with sample JSON as per Step 5."""
    sample_json = [
        {"ctin": "24AAAAA0000A1Z5", "inum": "PUR-TEST-001", "tax_amt": 180.00},
        {"ctin": "27BBBBB1111B1Z2", "inum": "PORTAL-ONLY-099", "tax_amt": 1250.00}
    ]
    return reconcile_gstr2b(company, sample_json)
