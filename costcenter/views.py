"""
costcenter/views.py — Cost Centers & Budgeting views
"""

from datetime import date as _date, datetime
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404

from core.decorators import admin_required, write_required
from .models import CostCenter, BudgetHead
from .forms import CostCenterForm, BudgetHeadForm


# ── helpers ───────────────────────────────────────────────────────────────────

def _current_fy():
    today = _date.today()
    y = today.year if today.month >= 4 else today.year - 1
    return f"{y}-{str(y + 1)[-2:]}"


def _fy_date_range(fy_str):
    try:
        start_year = int(fy_str.split("-")[0])
        return _date(start_year, 4, 1), _date(start_year + 1, 3, 31)
    except Exception:
        today = _date.today()
        y = today.year if today.month >= 4 else today.year - 1
        return _date(y, 4, 1), _date(y + 1, 3, 31)


INCOME_GROUPS  = {"Direct Income", "Indirect Income", "Sales Accounts"}
EXPENSE_GROUPS = {"Direct Expense", "Indirect Expense", "Purchase Accounts"}


# ── Cost Center CRUD ─────────────────────────────────────────────────────────

@login_required
def cost_center_list(request):
    company     = request.current_company
    show        = request.GET.get("show", "active")
    cost_centers = CostCenter.objects.filter(company=company)
    if show == "inactive":
        cost_centers = cost_centers.filter(is_active=False)
    elif show != "all":
        cost_centers = cost_centers.filter(is_active=True)
    cost_centers = cost_centers.order_by("name")
    return render(request, "costcenter/cost_center_list.html", {
        "cost_centers": cost_centers, "show": show,
    })


@login_required
@write_required
def cost_center_create(request):
    company = request.current_company
    if request.method == "POST":
        form = CostCenterForm(request.POST, company=company)
        if form.is_valid():
            cc = form.save(commit=False)
            cc.company = company
            cc.save()
            messages.success(request, f"Cost center '{cc.name}' created.")
            return redirect("costcenter:cost_center_list")
    else:
        form = CostCenterForm(company=company)
    return render(request, "costcenter/cost_center_form.html", {
        "form": form, "title": "Add Cost Center",
    })


@login_required
@write_required
def cost_center_edit(request, pk):
    company = request.current_company
    cc = get_object_or_404(CostCenter, pk=pk, company=company)
    if request.method == "POST":
        form = CostCenterForm(request.POST, instance=cc, company=company)
        if form.is_valid():
            form.save()
            messages.success(request, f"Cost center '{cc.name}' updated.")
            return redirect("costcenter:cost_center_list")
    else:
        form = CostCenterForm(instance=cc, company=company)
    return render(request, "costcenter/cost_center_form.html", {
        "form": form, "cc": cc, "title": f"Edit — {cc.name}",
    })


@login_required
@admin_required
def cost_center_delete(request, pk):
    company = request.current_company
    cc = get_object_or_404(CostCenter, pk=pk, company=company)
    if request.method == "POST":
        if cc.voucher_items.exists():
            messages.error(
                request,
                f"Cannot delete '{cc.name}' — it has voucher items linked. Deactivate instead."
            )
            return redirect("costcenter:cost_center_list")
        cc.delete()
        messages.success(request, f"Cost center '{cc.name}' deleted.")
        return redirect("costcenter:cost_center_list")
    return render(request, "costcenter/cost_center_confirm_delete.html", {"cc": cc})


# ── Budget Head CRUD ─────────────────────────────────────────────────────────

@login_required
def budget_list(request):
    company = request.current_company
    fy      = request.GET.get("fy", "").strip()
    budgets = BudgetHead.objects.filter(company=company).select_related("ledger", "cost_center")
    if fy:
        budgets = budgets.filter(financial_year=fy)
    budgets = budgets.order_by("financial_year", "ledger__name", "period")
    all_fys = (
        BudgetHead.objects.filter(company=company)
        .values_list("financial_year", flat=True).distinct().order_by("-financial_year")
    )
    return render(request, "costcenter/budget_list.html", {
        "budgets": budgets, "all_fys": all_fys, "fy": fy,
    })


@login_required
@write_required
def budget_create(request):
    company = request.current_company
    if request.method == "POST":
        form = BudgetHeadForm(request.POST, company=company)
        if form.is_valid():
            bh = form.save(commit=False)
            bh.company = company
            bh.save()
            messages.success(request, f"Budget head created for '{bh.ledger.name}'.")
            return redirect("costcenter:budget_list")
    else:
        form = BudgetHeadForm(company=company)
    return render(request, "costcenter/budget_form.html", {"form": form, "title": "Add Budget Head"})


@login_required
@write_required
def budget_edit(request, pk):
    company = request.current_company
    bh = get_object_or_404(BudgetHead, pk=pk, company=company)
    if request.method == "POST":
        form = BudgetHeadForm(request.POST, instance=bh, company=company)
        if form.is_valid():
            form.save()
            messages.success(request, "Budget head updated.")
            return redirect("costcenter:budget_list")
    else:
        form = BudgetHeadForm(instance=bh, company=company)
    return render(request, "costcenter/budget_form.html", {
        "form": form, "bh": bh, "title": "Edit Budget Head",
    })


@login_required
@admin_required
def budget_delete(request, pk):
    company = request.current_company
    bh = get_object_or_404(BudgetHead, pk=pk, company=company)
    if request.method == "POST":
        bh.delete()
        messages.success(request, "Budget head deleted.")
        return redirect("costcenter:budget_list")
    return render(request, "costcenter/budget_confirm_delete.html", {"bh": bh})


# ── Budget Variance Report ───────────────────────────────────────────────────

@login_required
def budget_variance(request):
    company = request.current_company
    fy      = request.GET.get("fy", _current_fy()).strip()
    cc_pk   = request.GET.get("cc", "").strip()

    start_date, end_date = _fy_date_range(fy)

    budgets_qs = BudgetHead.objects.filter(
        company=company, financial_year=fy, period="Annual"
    ).select_related("ledger", "cost_center")
    if cc_pk:
        budgets_qs = budgets_qs.filter(cost_center_id=cc_pk)

    from vouchers.models import VoucherItem
    rows = []
    total_budget   = Decimal("0.00")
    total_actual   = Decimal("0.00")

    for bh in budgets_qs:
        vi_qs = VoucherItem.objects.filter(
            ledger=bh.ledger,
            voucher__company=company,
            voucher__status="APPROVED",
            voucher__date__gte=start_date,
            voucher__date__lte=end_date,
        )
        if bh.cost_center_id:
            vi_qs = vi_qs.filter(cost_center=bh.cost_center)

        agg       = vi_qs.aggregate(
            total_dr=Sum("amount", filter=Q(entry_type="DR")),
            total_cr=Sum("amount", filter=Q(entry_type="CR")),
        )
        actual_dr = agg["total_dr"] or Decimal("0.00")
        actual_cr = agg["total_cr"] or Decimal("0.00")
        group     = bh.ledger.account_group.name
        nature    = bh.ledger.account_group.nature

        if nature == "Expense" or group in EXPENSE_GROUPS:
            actual = actual_dr - actual_cr
        elif nature == "Income" or group in INCOME_GROUPS:
            actual = actual_cr - actual_dr
        else:
            actual = actual_dr - actual_cr

        budget   = bh.budgeted_amount
        variance = actual - budget
        pct      = (actual / budget * 100).quantize(Decimal("0.1")) if budget else None

        rows.append({
            "bh": bh, "ledger": bh.ledger.name,
            "cc": bh.cost_center.name if bh.cost_center_id else "—",
            "budget": budget, "actual": actual, "variance": variance,
            "pct": pct, "over": variance > 0,
        })
        total_budget += budget
        total_actual += actual

    all_fys = (
        BudgetHead.objects.filter(company=company)
        .values_list("financial_year", flat=True).distinct().order_by("-financial_year")
    )
    cost_centers = CostCenter.objects.filter(company=company, is_active=True).order_by("name")

    return render(request, "costcenter/budget_variance.html", {
        "rows": rows, "fy": fy, "cc_pk": cc_pk,
        "all_fys": all_fys, "cost_centers": cost_centers,
        "total_budget": total_budget, "total_actual": total_actual,
        "total_variance": total_actual - total_budget,
        "start_date": start_date, "end_date": end_date,
    })


# ── Cost Center P&L Report ───────────────────────────────────────────────────

@login_required
def cost_center_report(request):
    company    = request.current_company
    start_date = request.GET.get("start_date", "").strip()
    end_date   = request.GET.get("end_date", "").strip()

    fy_start, fy_end = _fy_date_range(_current_fy())
    try:
        parsed_start = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else fy_start
        parsed_end   = datetime.strptime(end_date,   "%Y-%m-%d").date() if end_date   else fy_end
    except ValueError:
        parsed_start, parsed_end = fy_start, fy_end

    start_date = start_date or str(fy_start)
    end_date   = end_date   or str(fy_end)

    from vouchers.models import VoucherItem
    cost_centers = CostCenter.objects.filter(company=company, is_active=True).order_by("name")
    cc_rows      = []
    grand_income  = Decimal("0.00")
    grand_expense = Decimal("0.00")

    for cc in cost_centers:
        vi_qs = VoucherItem.objects.filter(
            cost_center=cc,
            voucher__company=company,
            voucher__status="APPROVED",
            voucher__date__gte=parsed_start,
            voucher__date__lte=parsed_end,
        ).select_related("ledger")

        income  = Decimal("0.00")
        expense = Decimal("0.00")
        ledger_lines = {}

        for vi in vi_qs:
            nature = vi.ledger.account_group.nature
            group_name = vi.ledger.account_group.name
            if nature == "Income" or group_name in INCOME_GROUPS:
                if vi.entry_type == 'CR': income += vi.amount
                else: income -= vi.amount
            elif nature == "Expense" or group_name in EXPENSE_GROUPS:
                if vi.entry_type == 'DR': expense += vi.amount
                else: expense -= vi.amount
            
            key = vi.ledger.name
            if key not in ledger_lines:
                ledger_lines[key] = {"name": key, "group": nature, "debit": Decimal("0"), "credit": Decimal("0")}
            
            if vi.entry_type == 'DR':
                ledger_lines[key]["debit"]  += vi.amount
            else:
                ledger_lines[key]["credit"] += vi.amount

        cc_rows.append({
            "cc": cc, "income": income, "expense": expense,
            "net": income - expense,
            "ledger_lines": sorted(ledger_lines.values(), key=lambda x: x["name"]),
        })
        grand_income  += income
        grand_expense += expense

    # Untagged items
    untagged_qs = VoucherItem.objects.filter(
        cost_center__isnull=True,
        voucher__company=company,
        voucher__status="APPROVED",
        voucher__date__gte=parsed_start,
        voucher__date__lte=parsed_end,
    ).select_related("ledger__account_group")
    
    untagged_income  = sum(
        (vi.amount if vi.entry_type == 'CR' else -vi.amount) 
        for vi in untagged_qs
        if vi.ledger.account_group.nature == "Income"
        or vi.ledger.account_group.name in INCOME_GROUPS
    )
    untagged_expense = sum(
        (vi.amount if vi.entry_type == 'DR' else -vi.amount) 
        for vi in untagged_qs
        if vi.ledger.account_group.nature == "Expense"
        or vi.ledger.account_group.name in EXPENSE_GROUPS
    )

    return render(request, "costcenter/cost_center_report.html", {
        "cc_rows": cc_rows,
        "grand_income": grand_income, "grand_expense": grand_expense,
        "grand_net": grand_income - grand_expense,
        "untagged_income": untagged_income, "untagged_expense": untagged_expense,
        "start_date": start_date, "end_date": end_date,
    })


# ── AJAX: cost center autocomplete for voucher form ──────────────────────────

from django.views.decorators.http import require_POST
import json

@login_required
@write_required
@require_POST
def quick_add(request):
    """AJAX endpoint to create a Cost Center on-the-fly."""
    company = request.current_company
    try:
        data = json.loads(request.body)
        name = data.get("name", "").strip()
        if not name:
            return JsonResponse({"success": False, "error": "Name is required."}, status=400)
        
        cc = CostCenter.objects.filter(company=company, name__iexact=name).first()
        created = False
        if not cc:
            cc = CostCenter.objects.create(company=company, name=name, is_active=True)
            created = True
        return JsonResponse({
            "success": True,
            "id": cc.pk,
            "name": cc.name,
            "created": created
        })
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)

@login_required
def cc_autocomplete(request):
    company = request.current_company
    q       = request.GET.get("q", "").strip()
    qs = CostCenter.objects.filter(company=company, is_active=True)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q))
    return JsonResponse({"results": [{"id": cc.pk, "text": str(cc)} for cc in qs[:30]]})
