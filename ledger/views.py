"""
ledger/views.py

Access control:
  list        → all authenticated users with company access
  create      → Admin, Accountant
  edit        → Admin, Accountant
  delete      → Admin only
  quick_add   → Admin, Accountant (AJAX endpoint for inline modal in voucher form)
"""

import json
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST

from core.decorators import admin_required, write_required
from .models import Ledger
from .forms import LedgerForm


@login_required
def ledger_list(request):
    company = request.current_company
    show_inactive = request.GET.get("show_inactive") == "1"
    qs = Ledger.objects.filter(company=company)
    if not show_inactive:
        qs = qs.filter(is_active=True)
    ledgers = qs.order_by("account_group__nature", "name")
    inactive_count = Ledger.objects.filter(company=company, is_active=False).count()
    return render(request, "ledger/ledger_list.html", {
        "ledgers": ledgers,
        "show_inactive": show_inactive,
        "inactive_count": inactive_count,
    })


@login_required
@write_required
def ledger_create(request):
    company = request.current_company
    form = LedgerForm(request.POST or None, company=company)

    if request.method == "POST" and form.is_valid():
        ledger = form.save(commit=False)
        ledger.company = company
        ledger.save()
        messages.success(request, f'Ledger "{ledger.name}" created successfully.')
        return redirect("ledger:list")

    return render(request, "ledger/ledger_form.html", {"form": form, "title": "Create Ledger"})


@login_required
@write_required
def ledger_edit(request, pk):
    company = request.current_company
    ledger  = get_object_or_404(Ledger, pk=pk, company=company)
    form    = LedgerForm(request.POST or None, instance=ledger, company=company)

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f'Ledger "{ledger.name}" updated.')
        return redirect("ledger:list")

    return render(request, "ledger/ledger_form.html",
                  {"form": form, "title": "Edit Ledger", "ledger": ledger})


@login_required
@admin_required
def ledger_deactivate(request, pk):
    """Soft-delete: marks ledger as inactive instead of hard-deleting."""
    company = request.current_company
    ledger  = get_object_or_404(Ledger, pk=pk, company=company)

    if request.method == "POST":
        ledger.is_active = False
        ledger.save(update_fields=["is_active"])
        messages.warning(request, f'Ledger "{ledger.name}" has been deactivated.')
        return redirect("ledger:list")

    return render(request, "ledger/ledger_confirm_deactivate.html", {"ledger": ledger})


@login_required
@admin_required
def ledger_reactivate(request, pk):
    """Re-activates a previously deactivated ledger."""
    company = request.current_company
    ledger  = get_object_or_404(Ledger, pk=pk, company=company)

    if request.method == "POST":
        ledger.is_active = True
        ledger.save(update_fields=["is_active"])
        messages.success(request, f'Ledger "{ledger.name}" has been reactivated.')
        return redirect("ledger:list")

    return render(request, "ledger/ledger_confirm_reactivate.html", {"ledger": ledger})


@login_required
def ledger_statement(request, pk):
    """
    Party-wise Account Statement for a single ledger.

    Shows every VoucherItem for this ledger (within optional date range) with:
      - Date, Voucher #, Type, Narration
      - Debit / Credit amounts
      - Running balance (Dr/Cr)
      - Bill-wise column: which invoice this line settles (reference_voucher)

    Access: all authenticated users with company access (read-only).
    """
    from datetime import datetime
    from decimal import Decimal

    company = request.current_company
    ledger  = get_object_or_404(Ledger, pk=pk, company=company)

    # Track as recent item
    from core.utils.search_utils import add_recent_item
    from django.urls import reverse
    add_recent_item(request, 'ledgers', ledger.id, ledger.name, reverse('reports:ledger_detail', args=[ledger.id]))

    # ── Date filters ───────────────────────────────────────────────────────
    raw_start = request.GET.get("start_date", "").strip()
    raw_end   = request.GET.get("end_date", "").strip()
    parsed_start = parsed_end = None
    try:
        if raw_start:
            parsed_start = datetime.strptime(raw_start, "%Y-%m-%d").date()
        if raw_end:
            parsed_end   = datetime.strptime(raw_end,   "%Y-%m-%d").date()
    except ValueError:
        pass

    # ── Transactions ───────────────────────────────────────────────────────
    from vouchers.models import VoucherItem
    qs = (
        VoucherItem.objects
        .filter(ledger=ledger)
        .select_related("voucher", "reference_voucher")
        .order_by("voucher__date", "voucher__created_at", "id")
    )
    if parsed_start:
        qs = qs.filter(voucher__date__gte=parsed_start)
    if parsed_end:
        qs = qs.filter(voucher__date__lte=parsed_end)

    # ── Running balance ────────────────────────────────────────────────────
    # Opening balance = opening_balance field + all movements BEFORE start_date
    opening = ledger.opening_balance
    if parsed_start:
        pre_qs = VoucherItem.objects.filter(
            ledger=ledger, voucher__date__lt=parsed_start
        ).values_list("entry_type", "amount")
        for etype, amt in pre_qs:
            if etype == 'CR':
                opening += Decimal(str(amt or 0))
            else:
                opening -= Decimal(str(amt or 0))

    rows = []
    running = opening
    total_dr = total_cr = Decimal("0.00")

    for item in qs:
        if item.entry_type == 'CR':
            running += item.amount
            total_cr += item.amount
        else:
            running -= item.amount
            total_dr += item.amount
            
        rows.append({
            "item":    item,
            "running": running,
            "running_amount": abs(running),
            "running_side": "Cr" if running >= Decimal("0.00") else "Dr",
            "dr_side": running < Decimal("0.00"),   # True = Dr balance
        })

    closing = running

    return render(request, "ledger/ledger_statement.html", {
        "ledger":       ledger,
        "rows":         rows,
        "opening":      opening,
        "opening_amount": abs(opening),
        "opening_side": "Cr" if opening >= Decimal("0.00") else "Dr",
        "closing":      closing,
        "closing_amount": abs(closing),
        "closing_side": "Cr" if closing >= Decimal("0.00") else "Dr",
        "total_dr":     total_dr,
        "total_cr":     total_cr,
        "start_date":   raw_start,
        "end_date":     raw_end,
    })


@login_required
def ledger_suggestions(request):
    """
    Returns top 10 most used ledgers for the current company.
    Used for smart suggestions in voucher forms.
    """
    from django.db.models import Count
    company = request.current_company
    
    # Get top 10 ledgers by usage in voucher items
    top_ledgers = (
        Ledger.objects.filter(company=company, is_active=True)
        .annotate(usage_count=Count('voucher_items'))
        .order_by('-usage_count', 'name')[:10]
    )
    
    data = [
        {"id": l.id, "name": l.name, "group": l.account_group.name}
        for l in top_ledgers
    ]
    return JsonResponse({"suggestions": data})


@login_required
@write_required
@require_POST
def ledger_quick_add(request):
    """
    AJAX endpoint: creates a new ledger for the current company and returns JSON.
    """
    from .models import AccountGroup
    company = request.current_company

    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        data = request.POST.dict()

    # Map nature string to an AccountGroup
    nature = data.get("group")
    if nature:
        group, _ = AccountGroup.objects.get_or_create(
            company=company, name=nature, nature=nature
        )
        data["account_group"] = group.pk

    form = LedgerForm(data, company=company)
    if form.is_valid():
        ledger = form.save(commit=False)
        ledger.company = company
        if Ledger.objects.filter(company=company, name__iexact=ledger.name).exists():
            return JsonResponse({
                "success": False,
                "errors": {"name": [f'A ledger named "{ledger.name}" already exists.']},
            }, status=400)
        ledger.save()
        return JsonResponse({
            "success": True,
            "id":    ledger.pk,
            "name":  ledger.name,
            "group": ledger.account_group.name,
        })

    return JsonResponse({
        "success": False,
        "errors":  {
            field: [str(e) for e in errs]
            for field, errs in form.errors.items()
        },
    }, status=400)
