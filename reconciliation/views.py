from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from vouchers.models import Voucher, VoucherItem

@login_required
def delinquent_vendors_report(request):
    """
    Identifies vendors who have not filed GST, blocking our ITC.
    Finds Purchase vouchers where is_itc_claimed is False.
    """
    access = request.user.company_access.first()
    if not access:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("You do not have access to any company.")
    company = access.company

    pending_purchases = Voucher.objects.filter(        company=company,
        voucher_type='Purchase',
        is_itc_claimed=False,
        total_tax__gt=0
    ).order_by('date')

    # Group by vendor for summary
    vendor_summary = {}
    for v in pending_purchases:
        # Find vendor ledger (Liability nature)
        vendor_item = v.items.filter(ledger__account_group__nature='Liability').first()
        if vendor_item:
            vendor = vendor_item.ledger
            if vendor.id not in vendor_summary:
                vendor_summary[vendor.id] = {
                    'name': vendor.name,
                    'email': getattr(vendor, 'email', ''), # Assuming email field exists or adding it
                    'count': 0,
                    'total_pending_itc': 0,
                    'invoices': []
                }
            vendor_summary[vendor.id]['count'] += 1
            vendor_summary[vendor.id]['total_pending_itc'] += v.total_tax
            vendor_summary[vendor.id]['invoices'].append(v)

    return render(request, 'reconciliation/delinquent_vendors.html', {
        'vendor_summary': vendor_summary.values(),
    })
