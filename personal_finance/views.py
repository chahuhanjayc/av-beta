import csv
import io
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone

from .models import (
    PersonalBudget,
    PersonalCategory,
    PersonalExpense,
    PersonalExpenseTemplate,
    PersonalFinanceSettings,
    PersonalIncome,
)

DEFAULT_PAYMENT_METHODS = ["Cash", "UPI / GPay", "Bank Transfer", "Credit Card"]
UNIT_OPTIONS = ["pcs", "kg", "g", "L", "ml", "pack", "dozen", "box"]
CURRENCIES = [
    {"code": "INR", "symbol": "INR ", "name": "Indian Rupee"},
    {"code": "USD", "symbol": "$", "name": "US Dollar"},
    {"code": "EUR", "symbol": "EUR ", "name": "Euro"},
    {"code": "GBP", "symbol": "GBP ", "name": "British Pound"},
    {"code": "AED", "symbol": "AED ", "name": "UAE Dirham"},
    {"code": "SGD", "symbol": "SGD ", "name": "Singapore Dollar"},
]
CURRENCY_SYMBOLS = {item["code"]: item["symbol"] for item in CURRENCIES}
DEFAULT_CATEGORY_META = [
    ("Groceries", "bi-basket2-fill", "#0E9F6E"),
    ("Fast Food", "bi-cup-hot-fill", "#E3A008"),
    ("Fuel", "bi-fuel-pump-fill", "#1A56DB"),
    ("Utilities", "bi-lightning-charge-fill", "#6B7280"),
    ("Entertainment", "bi-controller", "#7E3AF2"),
    ("Health", "bi-heart-pulse-fill", "#F05252"),
    ("Transport", "bi-bus-front-fill", "#FF8C00"),
    ("Shopping", "bi-bag-fill", "#0694A2"),
    ("EMI / Finance", "bi-bank", "#C81E1E"),
    ("Other", "bi-three-dots", "#9CA3AF"),
]
LEGACY_SOURCES = {
    "spendsight": [
        Path(settings.BASE_DIR) / "SpendSight" / "user_data" / "data_admin.json",
        Path(settings.BASE_DIR) / "SpendSight" / "expenses.json",
    ],
    "og_source": [
        Path(settings.BASE_DIR) / "OG Source" / "user_data" / "data_admin.json",
        Path(settings.BASE_DIR) / "OG Source" / "expenses.json",
    ],
}


def _ensure_default_categories(user):
    if PersonalCategory.objects.filter(user=user).exists():
        return
    for name, icon, color in DEFAULT_CATEGORY_META:
        PersonalCategory.objects.create(user=user, name=name, icon=icon, color=color)


def _get_finance_settings(user):
    defaults = {"payment_methods": DEFAULT_PAYMENT_METHODS.copy()}
    settings_obj, _ = PersonalFinanceSettings.objects.get_or_create(user=user, defaults=defaults)
    if not settings_obj.payment_methods:
        settings_obj.payment_methods = DEFAULT_PAYMENT_METHODS.copy()
        settings_obj.save(update_fields=["payment_methods", "updated_at"])
    if settings_obj.billing_start_day < 1 or settings_obj.billing_start_day > 28:
        settings_obj.billing_start_day = 1
        settings_obj.save(update_fields=["billing_start_day", "updated_at"])
    return settings_obj


def _to_decimal(value, default="0"):
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return Decimal(default)


def _parse_date(value):
    if not value:
        return timezone.localdate()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return timezone.localdate()


def _category_meta(name):
    for category_name, icon, color in DEFAULT_CATEGORY_META:
        if category_name == name:
            return icon, color
    return "bi-tag", "#64748b"


def _get_or_create_category(user, name):
    icon, color = _category_meta(name)
    category, _ = PersonalCategory.objects.get_or_create(
        user=user,
        name=name,
        defaults={"icon": icon, "color": color},
    )
    return category


def _current_billing_period(day, ref_date=None):
    ref_date = ref_date or timezone.localdate()
    billing_day = max(1, min(int(day or 1), 28))
    if ref_date.day >= billing_day:
        start = ref_date.replace(day=billing_day)
    else:
        previous_month_end = ref_date.replace(day=1) - timedelta(days=1)
        start = previous_month_end.replace(day=billing_day)
    next_month_anchor = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month_anchor.replace(day=billing_day) - timedelta(days=1)
    return start, end


def _format_amount(amount, currency_symbol):
    return f"{currency_symbol}{amount:,.2f}"


def _month_options(count=6):
    today = timezone.localdate().replace(day=1)
    options = []
    year = today.year
    month = today.month
    for _ in range(count):
        options.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(options))


def _legacy_source_choices():
    return [
        {"value": "spendsight", "label": "SpendSight folder"},
        {"value": "og_source", "label": "OG Source folder"},
    ]


def _resolve_legacy_file(source_key):
    for candidate in LEGACY_SOURCES.get(source_key, []):
        if candidate.exists():
            return candidate
    return None


def _import_legacy_data(user, source_key):
    legacy_file = _resolve_legacy_file(source_key)
    if not legacy_file:
        raise FileNotFoundError("Legacy SpendSight data file was not found.")

    payload = json.loads(legacy_file.read_text(encoding="utf-8"))
    _ensure_default_categories(user)
    finance_settings = _get_finance_settings(user)

    imported = {"expenses": 0, "income": 0, "budgets": 0, "templates": 0, "categories": 0}

    finance_settings.currency_code = payload.get("currency_code") or finance_settings.currency_code
    finance_settings.billing_start_day = max(1, min(int(payload.get("billing_start_day") or 1), 28))
    payment_methods = [pm.strip() for pm in payload.get("payment_methods", []) if str(pm).strip()]
    finance_settings.payment_methods = payment_methods or finance_settings.payment_methods or DEFAULT_PAYMENT_METHODS.copy()
    finance_settings.save()

    seen_categories = set(PersonalCategory.objects.filter(user=user).values_list("name", flat=True))
    legacy_categories = set(payload.get("custom_categories", {}).keys())
    legacy_categories.update(expense.get("category") for expense in payload.get("expenses", []) if expense.get("category"))
    legacy_categories.update(payload.get("budget_limits", {}).keys())
    for category_name in sorted(legacy_categories):
        if category_name not in seen_categories:
            _get_or_create_category(user, category_name)
            imported["categories"] += 1

    for expense in payload.get("expenses", []):
        category = _get_or_create_category(user, expense.get("category") or "Other")
        amount = _to_decimal(expense.get("amount"))
        expense_date = _parse_date(expense.get("date"))
        item_name = (expense.get("subcategory") or "").strip()
        notes = (expense.get("notes") or expense.get("description") or "").strip()
        payment_method = (expense.get("payment_method") or "Cash").strip()
        quantity = expense.get("quantity")
        unit = (expense.get("unit") or "").strip()
        _, created = PersonalExpense.objects.get_or_create(
            user=user,
            date=expense_date,
            amount=amount,
            category=category,
            item_name=item_name,
            description=notes,
            payment_method=payment_method,
            defaults={
                "quantity": _to_decimal(quantity) if quantity not in (None, "") else None,
                "unit": unit,
            },
        )
        if created:
            imported["expenses"] += 1

    income_blob = payload.get("income") or {}
    salary_history = income_blob.get("salary_history") or []
    if not salary_history and income_blob.get("monthly_salary"):
        salary_history = [{
            "amount": income_blob.get("monthly_salary"),
            "effective_from": income_blob.get("salary_updated") or timezone.localdate().isoformat(),
        }]
    for salary in salary_history:
        _, created = PersonalIncome.objects.get_or_create(
            user=user,
            source="Salary",
            date=_parse_date(salary.get("effective_from")),
            amount=_to_decimal(salary.get("amount")),
            description="Imported from SpendSight salary history",
        )
        if created:
            imported["income"] += 1

    for extra in payload.get("extra_income", []):
        description = (extra.get("description") or extra.get("type") or "Extra Income").strip()
        _, created = PersonalIncome.objects.get_or_create(
            user=user,
            source=description[:100],
            date=_parse_date(extra.get("date") or extra.get("start_date")),
            amount=_to_decimal(extra.get("amount")),
            description=f"Imported from SpendSight ({extra.get('type', 'extra')})",
        )
        if created:
            imported["income"] += 1

    for category_name, limit in (payload.get("budget_limits") or {}).items():
        category = _get_or_create_category(user, category_name)
        PersonalBudget.objects.update_or_create(
            user=user,
            category=category,
            defaults={"monthly_limit": _to_decimal(limit)},
        )
        imported["budgets"] += 1

    for template in payload.get("templates", []):
        category = _get_or_create_category(user, template.get("category") or "Other")
        _, created = PersonalExpenseTemplate.objects.get_or_create(
            user=user,
            name=(template.get("name") or template.get("subcategory") or "Quick Template")[:100],
            defaults={
                "category": category,
                "item_name": (template.get("subcategory") or "")[:120],
                "amount": _to_decimal(template.get("amount")),
                "payment_method": (template.get("payment_method") or "Cash")[:50],
                "quantity": _to_decimal(template.get("quantity")) if template.get("quantity") not in (None, "") else None,
                "unit": (template.get("unit") or "")[:20],
            },
        )
        if created:
            imported["templates"] += 1

    return legacy_file, imported


@login_required
def dashboard(request):
    _ensure_default_categories(request.user)
    finance_settings = _get_finance_settings(request.user)
    period_start, period_end = _current_billing_period(finance_settings.billing_start_day)
    currency_symbol = CURRENCY_SYMBOLS.get(finance_settings.currency_code, "INR ")

    expenses_in_period = PersonalExpense.objects.filter(user=request.user, date__range=(period_start, period_end))
    income_in_period = PersonalIncome.objects.filter(user=request.user, date__range=(period_start, period_end))
    total_expenses = expenses_in_period.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    total_income = income_in_period.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    recent_expenses = PersonalExpense.objects.filter(user=request.user).select_related("category")[:10]
    category_data = list(
        expenses_in_period.values("category__name", "category__color")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    )

    budget_status = []
    spends_by_category = {
        row["category__name"]: row["total"]
        for row in expenses_in_period.values("category__name").annotate(total=Sum("amount"))
    }
    for budget in PersonalBudget.objects.filter(user=request.user).select_related("category"):
        spent = spends_by_category.get(budget.category.name, Decimal("0.00")) or Decimal("0.00")
        limit = budget.monthly_limit or Decimal("0.00")
        pct = float((spent / limit) * 100) if limit else 0.0
        budget_status.append({
            "name": budget.category.name,
            "color": budget.category.color,
            "spent": spent,
            "limit": limit,
            "percent": min(round(pct, 1), 999.0),
            "over": spent > limit if limit else False,
        })

    return render(request, "personal_finance/dashboard.html", {
        "total_expenses": total_expenses,
        "total_income": total_income,
        "savings": total_income - total_expenses,
        "recent_expenses": recent_expenses,
        "category_data": category_data,
        "currency_symbol": currency_symbol,
        "period_start": period_start,
        "period_end": period_end,
        "budget_status": budget_status[:5],
    })


@login_required
def income_and_budget(request):
    _ensure_default_categories(request.user)
    finance_settings = _get_finance_settings(request.user)
    currency_symbol = CURRENCY_SYMBOLS.get(finance_settings.currency_code, "INR ")
    period_start, period_end = _current_billing_period(finance_settings.billing_start_day)

    if request.method == "POST":
        for key, value in request.POST.items():
            if not key.startswith("budget_"):
                continue
            category_id = key.split("_", 1)[1]
            PersonalBudget.objects.update_or_create(
                user=request.user,
                category_id=category_id,
                defaults={"monthly_limit": _to_decimal(value)},
            )
        messages.success(request, "Budgets updated successfully.")
        return redirect("personal_finance:income_and_budget")

    income_history = PersonalIncome.objects.filter(user=request.user)
    budgets = {b.category_id: b.monthly_limit for b in PersonalBudget.objects.filter(user=request.user)}
    spend_rows = {
        row["category_id"]: row["total"]
        for row in PersonalExpense.objects.filter(user=request.user, date__range=(period_start, period_end))
        .values("category_id")
        .annotate(total=Sum("amount"))
    }
    categories = []
    for category in PersonalCategory.objects.filter(user=request.user):
        limit = budgets.get(category.id, Decimal("0.00"))
        spent = spend_rows.get(category.id, Decimal("0.00")) or Decimal("0.00")
        categories.append({
            "id": category.id,
            "name": category.name,
            "icon": category.icon,
            "color": category.color,
            "budget": limit,
            "spent": spent,
            "remaining": limit - spent,
        })

    return render(request, "personal_finance/income_and_budget.html", {
        "income_history": income_history,
        "categories": categories,
        "today": timezone.localdate(),
        "currency_symbol": currency_symbol,
        "period_start": period_start,
        "period_end": period_end,
    })


@login_required
def expense_list(request):
    _ensure_default_categories(request.user)
    finance_settings = _get_finance_settings(request.user)
    expenses = PersonalExpense.objects.filter(user=request.user).select_related("category")
    selected_category = request.GET.get("category", "")
    search = request.GET.get("q", "").strip()

    if selected_category:
        expenses = expenses.filter(category__id=selected_category)
    if search:
        expenses = expenses.filter(Q(item_name__icontains=search) | Q(description__icontains=search))

    return render(request, "personal_finance/expense_list.html", {
        "expenses": expenses,
        "categories": PersonalCategory.objects.filter(user=request.user),
        "selected_category": selected_category,
        "search": search,
        "currency_symbol": CURRENCY_SYMBOLS.get(finance_settings.currency_code, "INR "),
    })


@login_required
def expense_create(request):
    _ensure_default_categories(request.user)
    finance_settings = _get_finance_settings(request.user)
    categories = PersonalCategory.objects.filter(user=request.user)
    payment_methods = finance_settings.payment_methods or DEFAULT_PAYMENT_METHODS

    if request.method == "POST":
        PersonalExpense.objects.create(
            user=request.user,
            category_id=request.POST.get("category"),
            amount=_to_decimal(request.POST.get("amount")),
            date=_parse_date(request.POST.get("date")),
            item_name=(request.POST.get("item_name") or "").strip(),
            description=(request.POST.get("description") or "").strip(),
            payment_method=(request.POST.get("payment_method") or "Cash").strip(),
            quantity=_to_decimal(request.POST.get("quantity")) if request.POST.get("quantity") else None,
            unit=(request.POST.get("unit") or "").strip(),
        )
        messages.success(request, "Expense recorded successfully.")
        return redirect("personal_finance:expense_list")

    return render(request, "personal_finance/expense_form.html", {
        "categories": categories,
        "payment_methods": payment_methods,
        "unit_options": UNIT_OPTIONS,
        "today": timezone.localdate(),
        "currency_symbol": CURRENCY_SYMBOLS.get(finance_settings.currency_code, "INR "),
        "mode": "create",
    })


@login_required
def expense_edit(request, pk):
    _ensure_default_categories(request.user)
    finance_settings = _get_finance_settings(request.user)
    expense = get_object_or_404(PersonalExpense, pk=pk, user=request.user)
    categories = PersonalCategory.objects.filter(user=request.user)
    payment_methods = finance_settings.payment_methods or DEFAULT_PAYMENT_METHODS

    if request.method == "POST":
        expense.category_id = request.POST.get("category")
        expense.amount = _to_decimal(request.POST.get("amount"))
        expense.date = _parse_date(request.POST.get("date"))
        expense.item_name = (request.POST.get("item_name") or "").strip()
        expense.description = (request.POST.get("description") or "").strip()
        expense.payment_method = (request.POST.get("payment_method") or "Cash").strip()
        expense.quantity = _to_decimal(request.POST.get("quantity")) if request.POST.get("quantity") else None
        expense.unit = (request.POST.get("unit") or "").strip()
        expense.save()
        messages.success(request, "Expense updated successfully.")
        return redirect("personal_finance:expense_list")

    return render(request, "personal_finance/expense_form.html", {
        "expense": expense,
        "categories": categories,
        "payment_methods": payment_methods,
        "unit_options": UNIT_OPTIONS,
        "today": expense.date,
        "currency_symbol": CURRENCY_SYMBOLS.get(finance_settings.currency_code, "INR "),
        "mode": "edit",
    })


@login_required
def expense_delete(request, pk):
    expense = get_object_or_404(PersonalExpense, pk=pk, user=request.user)
    if request.method == "POST":
        expense.delete()
        messages.success(request, "Expense deleted successfully.")
    return redirect("personal_finance:expense_list")


@login_required
def income_create(request):
    finance_settings = _get_finance_settings(request.user)
    if request.method == "POST":
        PersonalIncome.objects.create(
            user=request.user,
            amount=_to_decimal(request.POST.get("amount")),
            source=(request.POST.get("source") or "").strip(),
            date=_parse_date(request.POST.get("date")),
            description=(request.POST.get("description") or "").strip(),
        )
        messages.success(request, "Income recorded successfully.")
        return redirect("personal_finance:income_and_budget")

    return render(request, "personal_finance/income_form.html", {
        "today": timezone.localdate(),
        "currency_symbol": CURRENCY_SYMBOLS.get(finance_settings.currency_code, "INR "),
    })


@login_required
def income_delete(request, pk):
    income = get_object_or_404(PersonalIncome, pk=pk, user=request.user)
    if request.method == "POST":
        income.delete()
        messages.success(request, "Income entry deleted successfully.")
    return redirect("personal_finance:income_and_budget")


@login_required
def analytics(request):
    _ensure_default_categories(request.user)
    finance_settings = _get_finance_settings(request.user)
    currency_symbol = CURRENCY_SYMBOLS.get(finance_settings.currency_code, "INR ")
    month_value = request.GET.get("month")
    if month_value:
        try:
            selected_month = datetime.strptime(month_value, "%Y-%m").date().replace(day=1)
        except ValueError:
            selected_month = timezone.localdate().replace(day=1)
    else:
        selected_month = timezone.localdate().replace(day=1)
    next_month = (selected_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    expenses = PersonalExpense.objects.filter(user=request.user, date__gte=selected_month, date__lt=next_month)

    by_category = list(expenses.values("category__name", "category__color").annotate(total=Sum("amount")).order_by("-total"))
    by_payment = list(expenses.values("payment_method").annotate(total=Sum("amount")).order_by("-total"))

    trend_labels = []
    trend_amounts = []
    for month_start in _month_options():
        month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        total = PersonalExpense.objects.filter(user=request.user, date__gte=month_start, date__lt=month_end).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        trend_labels.append(month_start.strftime("%b %Y"))
        trend_amounts.append(float(total))

    total_spend = expenses.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    avg_ticket = (total_spend / expenses.count()) if expenses.exists() else Decimal("0.00")
    days_in_month = max((next_month - selected_month).days, 1)
    daily_average = (total_spend / days_in_month) if total_spend else Decimal("0.00")

    return render(request, "personal_finance/analytics.html", {
        "selected_month": selected_month.strftime("%Y-%m"),
        "month_options": _month_options(12),
        "total_spend": total_spend,
        "avg_ticket": avg_ticket,
        "daily_average": daily_average,
        "category_chart_data": {
            "labels": [row["category__name"] for row in by_category],
            "amounts": [float(row["total"]) for row in by_category],
            "colors": [row["category__color"] for row in by_category],
        },
        "payment_chart_data": {
            "labels": [row["payment_method"] or "Unknown" for row in by_payment],
            "amounts": [float(row["total"]) for row in by_payment],
        },
        "trend_chart_data": {"labels": trend_labels, "amounts": trend_amounts},
        "currency_symbol": currency_symbol,
    })


@login_required
def purchase_audit(request):
    _ensure_default_categories(request.user)
    finance_settings = _get_finance_settings(request.user)
    currency_symbol = CURRENCY_SYMBOLS.get(finance_settings.currency_code, "INR ")
    selected_category = request.GET.get("category", "All")
    expenses = PersonalExpense.objects.filter(user=request.user).select_related("category")
    if selected_category != "All":
        expenses = expenses.filter(category__name=selected_category)

    grouped = defaultdict(list)
    for expense in expenses:
        key = expense.item_name or expense.description or expense.category.name
        grouped[(expense.category.name, key)].append(expense)

    audit_rows = []
    attention_count = 0
    usage_ready_count = 0
    total_spent = Decimal("0.00")
    for (category_name, key), items in grouped.items():
        items = sorted(items, key=lambda expense: expense.date)
        purchase_count = len(items)
        if purchase_count < 2:
            avg_gap = None
        else:
            gaps = [(items[index].date - items[index - 1].date).days for index in range(1, purchase_count)]
            avg_gap = round(sum(gaps) / len(gaps), 1)
        last_purchase = items[-1]
        days_since = (timezone.localdate() - last_purchase.date).days
        total_quantity = sum(float(item.quantity or 0) for item in items)
        quantity_display = f"{last_purchase.quantity} {last_purchase.unit}".strip() if last_purchase.quantity else "-"
        total_amount = sum(item.amount for item in items)
        total_spent += total_amount
        needs_attention = bool(avg_gap and days_since > avg_gap * 1.5)
        if needs_attention:
            attention_count += 1
        if total_quantity:
            usage_ready_count += 1
        audit_rows.append({
            "category": category_name,
            "name": key,
            "purchase_count": purchase_count,
            "last_purchase": last_purchase.date,
            "days_since": days_since,
            "avg_gap": avg_gap,
            "latest_quantity": quantity_display,
            "total_quantity": total_quantity,
            "total_amount": total_amount,
            "needs_attention": needs_attention,
            "status": "Review" if needs_attention else "Healthy",
            "insight": "Buying later than usual." if needs_attention else "Pattern looks steady.",
        })
    audit_rows.sort(key=lambda row: (not row["needs_attention"], row["name"].lower()))

    return render(request, "personal_finance/purchase_audit.html", {
        "audit_rows": audit_rows,
        "category_options": ["All"] + list(PersonalCategory.objects.filter(user=request.user).values_list("name", flat=True)),
        "selected_category": selected_category,
        "currency_symbol": currency_symbol,
        "summary": {
            "tracked_items": len(audit_rows),
            "attention_count": attention_count,
            "usage_ready_count": usage_ready_count,
            "total_spent": total_spent,
        },
    })


@login_required
def template_manager(request):
    _ensure_default_categories(request.user)
    finance_settings = _get_finance_settings(request.user)
    if request.method == "POST":
        PersonalExpenseTemplate.objects.create(
            user=request.user,
            name=(request.POST.get("name") or "").strip()[:100],
            category_id=request.POST.get("category"),
            item_name=(request.POST.get("item_name") or "").strip()[:120],
            amount=_to_decimal(request.POST.get("amount")),
            payment_method=(request.POST.get("payment_method") or "Cash").strip(),
            quantity=_to_decimal(request.POST.get("quantity")) if request.POST.get("quantity") else None,
            unit=(request.POST.get("unit") or "").strip(),
        )
        messages.success(request, "Quick template saved successfully.")
        return redirect("personal_finance:template_manager")

    return render(request, "personal_finance/template_manager.html", {
        "templates": PersonalExpenseTemplate.objects.filter(user=request.user).select_related("category"),
        "categories": PersonalCategory.objects.filter(user=request.user),
        "payment_methods": finance_settings.payment_methods or DEFAULT_PAYMENT_METHODS,
        "unit_options": UNIT_OPTIONS,
        "currency_symbol": CURRENCY_SYMBOLS.get(finance_settings.currency_code, "INR "),
    })


@login_required
def template_use(request, pk):
    template = get_object_or_404(PersonalExpenseTemplate, pk=pk, user=request.user)
    PersonalExpense.objects.create(
        user=request.user,
        amount=template.amount,
        date=timezone.localdate(),
        category=template.category,
        item_name=template.item_name,
        payment_method=template.payment_method,
        quantity=template.quantity,
        unit=template.unit,
    )
    messages.success(request, f"Expense added from template '{template.name}'.")
    return redirect("personal_finance:expense_list")


@login_required
def template_delete(request, pk):
    template = get_object_or_404(PersonalExpenseTemplate, pk=pk, user=request.user)
    if request.method == "POST":
        template.delete()
        messages.success(request, "Template deleted successfully.")
    return redirect("personal_finance:template_manager")


@login_required
def settings_view(request):
    _ensure_default_categories(request.user)
    finance_settings = _get_finance_settings(request.user)
    currency_symbol = CURRENCY_SYMBOLS.get(finance_settings.currency_code, "INR ")

    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "import_legacy":
            source_key = request.POST.get("legacy_source", "spendsight")
            try:
                legacy_file, imported = _import_legacy_data(request.user, source_key)
            except FileNotFoundError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    f"Imported SpendSight data from {legacy_file.name}: {imported['expenses']} expenses, {imported['income']} income rows, {imported['budgets']} budgets, {imported['templates']} templates.",
                )
            return redirect("personal_finance:settings")

        finance_settings.billing_start_day = max(1, min(int(request.POST.get("billing_start_day") or 1), 28))
        requested_currency = request.POST.get("currency_code") or "INR"
        finance_settings.currency_code = requested_currency if requested_currency in CURRENCY_SYMBOLS else "INR"
        payment_methods = [value.strip() for value in request.POST.getlist("payment_methods") if value.strip()]
        finance_settings.payment_methods = payment_methods or DEFAULT_PAYMENT_METHODS.copy()
        finance_settings.save()
        messages.success(request, "SpendSight settings updated successfully.")
        return redirect("personal_finance:settings")

    period_start, period_end = _current_billing_period(finance_settings.billing_start_day)
    return render(request, "personal_finance/settings.html", {
        "finance_settings": finance_settings,
        "currencies": CURRENCIES,
        "currency_symbol": currency_symbol,
        "payment_methods": finance_settings.payment_methods or DEFAULT_PAYMENT_METHODS,
        "legacy_sources": _legacy_source_choices(),
        "billing_preview": f"{period_start:%d %b %Y} to {period_end:%d %b %Y}",
    })


@login_required
def export_csv(request):
    """Export all personal expenses as a CSV file."""
    expenses = (
        PersonalExpense.objects.filter(user=request.user)
        .select_related("category")
        .order_by("-date")
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Category", "Item", "Description", "Payment Method", "Quantity", "Unit", "Amount"])
    for expense in expenses:
        writer.writerow([
            expense.date.isoformat(),
            expense.category.name,
            expense.item_name,
            expense.description,
            expense.payment_method,
            expense.quantity if expense.quantity is not None else "",
            expense.unit,
            str(expense.amount),
        ])
    response = HttpResponse(output.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = "attachment; filename=spendsight_expenses.csv"
    return response


@login_required
def export_pdf(request):
    """Export all personal expenses as a PDF file using weasyprint."""
    try:
        from weasyprint import HTML
    except ImportError:
        messages.error(request, "PDF export requires weasyprint. Please install it.")
        return redirect("personal_finance:expense_list")

    finance_settings = _get_finance_settings(request.user)
    currency_symbol = CURRENCY_SYMBOLS.get(finance_settings.currency_code, "INR ")
    expenses = (
        PersonalExpense.objects.filter(user=request.user)
        .select_related("category")
        .order_by("-date")
    )
    total = expenses.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    html_string = render_to_string("personal_finance/export_pdf.html", {
        "expenses": expenses,
        "total": total,
        "currency_symbol": currency_symbol,
        "generated_date": date.today().strftime("%B %d, %Y"),
        "user": request.user,
    })
    pdf_bytes = HTML(string=html_string, base_url=request.build_absolute_uri("/")).write_pdf()
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = "attachment; filename=spendsight_expenses.pdf"
    return response
