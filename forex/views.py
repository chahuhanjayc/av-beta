"""
forex/views.py — Multi-currency & Exchange Rate Management
"""

from datetime import date as _date
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404

from core.decorators import admin_required, write_required
from .models import Currency, ExchangeRate
from .forms import CurrencyForm, ExchangeRateForm


# ── Currency ──────────────────────────────────────────────────────────────────

@login_required
def currency_list(request):
    company    = request.current_company
    currencies = Currency.objects.filter(company=company).order_by("-is_base", "code")
    return render(request, "forex/currency_list.html", {"currencies": currencies})


@login_required
@write_required
def currency_create(request):
    company = request.current_company
    if request.method == "POST":
        form = CurrencyForm(request.POST)
        if form.is_valid():
            cur = form.save(commit=False)
            cur.company = company
            cur.full_clean()
            cur.save()
            messages.success(request, f"Currency {cur.code} added.")
            return redirect("forex:currency_list")
    else:
        form = CurrencyForm()
    return render(request, "forex/currency_form.html", {"form": form, "title": "Add Currency"})


@login_required
@write_required
def currency_edit(request, pk):
    company = request.current_company
    cur     = get_object_or_404(Currency, pk=pk, company=company)
    if request.method == "POST":
        form = CurrencyForm(request.POST, instance=cur)
        if form.is_valid():
            try:
                obj = form.save(commit=False)
                obj.full_clean()
                obj.save()
                messages.success(request, f"Currency {cur.code} updated.")
                return redirect("forex:currency_list")
            except Exception as e:
                form.add_error(None, str(e))
    else:
        form = CurrencyForm(instance=cur)
    return render(request, "forex/currency_form.html", {
        "form": form, "title": f"Edit {cur.code}", "cur": cur,
    })


# ── Exchange Rates ────────────────────────────────────────────────────────────

@login_required
def exchange_rate_list(request):
    company    = request.current_company
    currency_pk = request.GET.get("currency", "")
    currencies  = Currency.objects.filter(company=company, is_active=True, is_base=False).order_by("code")

    qs = ExchangeRate.objects.filter(currency__company=company).select_related("currency")
    if currency_pk:
        qs = qs.filter(currency_id=currency_pk)
    qs = qs.order_by("-date")[:200]

    return render(request, "forex/exchange_rate_list.html", {
        "rates":       qs,
        "currencies":  currencies,
        "currency_pk": currency_pk,
    })


@login_required
@write_required
def exchange_rate_create(request):
    company = request.current_company
    if request.method == "POST":
        form = ExchangeRateForm(request.POST, company=company)
        if form.is_valid():
            form.save()
            messages.success(request, "Exchange rate saved.")
            return redirect("forex:exchange_rate_list")
    else:
        form = ExchangeRateForm(company=company, initial={"date": _date.today(), "source": "Manual"})
    return render(request, "forex/exchange_rate_form.html", {
        "form": form, "title": "Add Exchange Rate",
    })


@login_required
@write_required
def exchange_rate_edit(request, pk):
    company = request.current_company
    rate    = get_object_or_404(ExchangeRate, pk=pk, currency__company=company)
    if request.method == "POST":
        form = ExchangeRateForm(request.POST, instance=rate, company=company)
        if form.is_valid():
            form.save()
            messages.success(request, "Exchange rate updated.")
            return redirect("forex:exchange_rate_list")
    else:
        form = ExchangeRateForm(instance=rate, company=company)
    return render(request, "forex/exchange_rate_form.html", {
        "form": form, "title": "Edit Exchange Rate", "rate": rate,
    })


@login_required
@admin_required
def exchange_rate_delete(request, pk):
    company = request.current_company
    rate    = get_object_or_404(ExchangeRate, pk=pk, currency__company=company)
    if request.method == "POST":
        rate.delete()
        messages.success(request, "Exchange rate deleted.")
        return redirect("forex:exchange_rate_list")
    return render(request, "forex/exchange_rate_confirm_delete.html", {"rate": rate})


# ── Forex Position ────────────────────────────────────────────────────────────

@login_required
def forex_position(request):
    """Show current exchange rates and a simple position summary."""
    company    = request.current_company
    currencies = Currency.objects.filter(company=company, is_active=True).order_by("-is_base", "code")
    today      = _date.today()

    positions = []
    for cur in currencies:
        if cur.is_base:
            positions.append({"currency": cur, "rate": None, "is_base": True})
        else:
            rate = ExchangeRate.get_rate(cur, today)
            latest_rate_obj = cur.exchange_rates.order_by("-date").first()
            positions.append({
                "currency":    cur,
                "rate":        rate,
                "rate_date":   latest_rate_obj.date if latest_rate_obj else None,
                "is_base":     False,
            })

    return render(request, "forex/forex_position.html", {
        "positions": positions, "today": today,
    })
