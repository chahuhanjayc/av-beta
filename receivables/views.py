import json
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db import transaction
from vouchers.models import Voucher, VoucherItem
from core.models import Company

@login_required
def bulk_settlement_view(request):
    access = request.user.company_access.first()
    if not access:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("You do not have access to any company.")
    company = access.company
    
    if request.method == 'POST':
        # AJAX mapping: payment_id -> invoice_id
        try:
            data = json.loads(request.body)
            payment_id = data.get('payment_id')
            invoice_id = data.get('invoice_id')
            
            with transaction.atomic():
                payment = Voucher.objects.select_for_update().get(pk=payment_id, company=company)
                invoice = Voucher.objects.select_for_update().get(pk=invoice_id, company=company)
                
                # Link the payment to the invoice
                # In our system, Bill-to-Bill is handled by Voucher.reference_voucher field
                # or individual VoucherItems having a reference_voucher.
                # The prompt Step 1 update model for Bill-to-Bill used Voucher.reference_voucher.
                
                payment.reference_voucher = invoice
                payment.save() # This triggers sync_outstanding in Voucher.save()
                
                # Also ensure VoucherItems that are party lines get the reference
                # (Standard bill-to-bill usually tracks at the item level)
                # We'll update the first item that doesn't have a reference
                item = payment.items.filter(reference_voucher__isnull=True).first()
                if item:
                    item.reference_voucher = invoice
                    item.save() # This also triggers sync_outstanding on reference_voucher
                
            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

    # GET: Load the split screen
    on_account = Voucher.objects.filter(
        company=company,
        voucher_type__in=['Receipt', 'Payment'],
        reference_voucher__isnull=True
    ).order_by('date')

    unpaid_invoices = Voucher.objects.filter(
        company=company,
        voucher_type__in=['Sales', 'Purchase'],
        outstanding_amount__gt=0
    ).order_by('date')

    return render(request, 'receivables/bulk_settlement.html', {
        'on_account': on_account,
        'unpaid_invoices': unpaid_invoices,
    })
