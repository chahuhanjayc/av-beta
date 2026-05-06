"""orders/views.py — Purchase & Sales Orders"""

from datetime import date as _date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404

from core.decorators import admin_required, write_required
from .models import Order, OrderItem
from .forms import OrderForm, OrderItemFormSet


PAGE_SIZE = 30


def _make_formset(company, *args, **kwargs):
    fs = OrderItemFormSet(*args, **kwargs)
    for f in fs.forms:
        f.__init__ = None   # prevent re-init stripping company
    # Patch company into each sub-form
    for f in fs.forms:
        f.fields["stock_item"].queryset = __import__(
            "inventory.models", fromlist=["StockItem"]
        ).StockItem.objects.filter(company=company, is_active=True).order_by("name")
    return fs


# ── List ─────────────────────────────────────────────────────────────────────

@login_required
def order_list(request):
    company    = request.current_company
    order_type = request.GET.get("type", "")     # Purchase | Sales | ""
    status     = request.GET.get("status", "")
    q          = request.GET.get("q", "").strip()

    qs = Order.objects.filter(company=company).select_related("party_ledger")
    if order_type:
        qs = qs.filter(order_type=order_type)
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(party_ledger__name__icontains=q) | qs.filter(number__icontains=q)

    paginator = Paginator(qs, PAGE_SIZE)
    page_obj  = paginator.get_page(request.GET.get("page"))

    return render(request, "orders/order_list.html", {
        "page_obj":   page_obj,
        "order_type": order_type,
        "status":     status,
        "q":          q,
        "status_choices": Order.STATUS_CHOICES,
    })


# ── Create ────────────────────────────────────────────────────────────────────

@login_required
@write_required
def order_create(request):
    company = request.current_company
    if request.method == "POST":
        form    = OrderForm(request.POST, company=company)
        formset = OrderItemFormSet(request.POST)
        for f in formset.forms:
            f.fields["stock_item"].queryset = __import__(
                "inventory.models", fromlist=["StockItem"]
            ).StockItem.objects.filter(company=company, is_active=True).order_by("name")
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                order = form.save(commit=False)
                order.company = company
                order.save()
                formset.instance = order
                formset.save()
            messages.success(request, f"{order.order_type} Order {order.number} created.")
            return redirect("orders:order_detail", pk=order.pk)
    else:
        form    = OrderForm(company=company)
        formset = OrderItemFormSet()
        for f in formset.forms:
            f.fields["stock_item"].queryset = __import__(
                "inventory.models", fromlist=["StockItem"]
            ).StockItem.objects.filter(company=company, is_active=True).order_by("name")

    return render(request, "orders/order_form.html", {
        "form": form, "formset": formset, "title": "New Order",
    })


# ── Edit ──────────────────────────────────────────────────────────────────────

@login_required
@write_required
def order_edit(request, pk):
    company = request.current_company
    order   = get_object_or_404(Order, pk=pk, company=company)
    if not order.is_editable:
        messages.error(request, f"Order {order.number} cannot be edited in '{order.status}' status.")
        return redirect("orders:order_detail", pk=pk)

    if request.method == "POST":
        form    = OrderForm(request.POST, instance=order, company=company)
        formset = OrderItemFormSet(request.POST, instance=order)
        for f in formset.forms:
            f.fields["stock_item"].queryset = __import__(
                "inventory.models", fromlist=["StockItem"]
            ).StockItem.objects.filter(company=company, is_active=True).order_by("name")
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                form.save()
                formset.save()
            messages.success(request, f"Order {order.number} updated.")
            return redirect("orders:order_detail", pk=pk)
    else:
        form    = OrderForm(instance=order, company=company)
        formset = OrderItemFormSet(instance=order)
        for f in formset.forms:
            f.fields["stock_item"].queryset = __import__(
                "inventory.models", fromlist=["StockItem"]
            ).StockItem.objects.filter(company=company, is_active=True).order_by("name")

    return render(request, "orders/order_form.html", {
        "form": form, "formset": formset, "order": order,
        "title": f"Edit {order.order_type} Order {order.number}",
    })


# ── Detail ────────────────────────────────────────────────────────────────────

@login_required
def order_detail(request, pk):
    company = request.current_company
    order   = get_object_or_404(Order, pk=pk, company=company)
    items   = order.items.select_related("stock_item")
    return render(request, "orders/order_detail.html", {
        "order": order, "items": items,
    })


# ── Confirm ───────────────────────────────────────────────────────────────────

@login_required
@write_required
def order_confirm(request, pk):
    company = request.current_company
    order   = get_object_or_404(Order, pk=pk, company=company)
    if request.method == "POST":
        if order.status == Order.STATUS_DRAFT:
            order.status = Order.STATUS_CONFIRMED
            order.save(update_fields=["status", "updated_at"])
            messages.success(request, f"Order {order.number} confirmed.")
        else:
            messages.warning(request, "Only Draft orders can be confirmed.")
    return redirect("orders:order_detail", pk=pk)


# ── Cancel ────────────────────────────────────────────────────────────────────

@login_required
@write_required
def order_cancel(request, pk):
    company = request.current_company
    order   = get_object_or_404(Order, pk=pk, company=company)
    if request.method == "POST":
        if order.status in (Order.STATUS_DRAFT, Order.STATUS_CONFIRMED):
            order.status = Order.STATUS_CANCELLED
            order.save(update_fields=["status", "updated_at"])
            messages.warning(request, f"Order {order.number} cancelled.")
        else:
            messages.error(request, "Cannot cancel a fulfilled order.")
    return redirect("orders:order_detail", pk=pk)


# ── Convert to Voucher ────────────────────────────────────────────────────────

@login_required
@write_required
def order_convert(request, pk):
    """
    Convert a Confirmed order to a Purchase/Sales Voucher.
    Creates the voucher, links it back, and advances order status to Fulfilled.
    """
    company = request.current_company
    order   = get_object_or_404(Order, pk=pk, company=company)

    if order.status not in (Order.STATUS_CONFIRMED, Order.STATUS_PARTIAL):
        messages.error(request, "Only Confirmed or Partially Fulfilled orders can be converted.")
        return redirect("orders:order_detail", pk=pk)

    if request.method != "POST":
        return render(request, "orders/order_convert_confirm.html", {"order": order})

    with transaction.atomic():
        from vouchers.models import Voucher, VoucherItem
        from ledger.models import Ledger as L

        # Determine the appropriate accounts-payable/receivable ledger
        voucher_type = order.order_type   # "Purchase" or "Sales"

        pending_items = [
            item for item in order.items.select_related("stock_item")
            if item.pending_qty > 0
        ]
        if not pending_items:
            messages.error(request, "This order has no pending quantity to convert.")
            return redirect("orders:order_detail", pk=pk)

        # Build double-entry:
        # Purchase: Dr Purchase Account / Cr Party (Creditor)
        # Sales:    Dr Party (Debtor) / Cr Sales Account
        total = sum((item.pending_qty * item.rate).quantize(Decimal("0.01")) for item in pending_items)

        if voucher_type == "Purchase":
            # Debit: first available Purchase/Expense ledger for company
            dr_ledger = L.objects.filter(
                company=company, is_active=True,
                account_group__name__in=["Purchase Accounts", "Direct Expense"]
            ).first() or L.objects.filter(
                company=company, is_active=True, account_group__nature="Expense"
            ).first()
            cr_ledger = order.party_ledger
            if not dr_ledger:
                messages.error(request, "No active Purchase/Direct Expense ledger found for this company.")
                return redirect("orders:order_detail", pk=pk)
        else:
            # Sales: Dr Party, Cr Sales
            dr_ledger = order.party_ledger
            cr_ledger = L.objects.filter(
                company=company, is_active=True,
                account_group__name__in=["Sales Accounts", "Direct Income"]
            ).first() or L.objects.filter(
                company=company, is_active=True, account_group__nature="Income"
            ).first()
            if not cr_ledger:
                messages.error(request, "No active Sales/Direct Income ledger found for this company.")
                return redirect("orders:order_detail", pk=pk)

        voucher = Voucher(
            company=company,
            voucher_type=voucher_type,
            date=_date.today(),
            narration=f"From {order.order_type} Order {order.number}",
        )
        voucher.save()   # auto-generates number

        VoucherItem.objects.create(
            voucher=voucher,
            ledger=dr_ledger,
            entry_type="DR",
            amount=total,
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=cr_ledger,
            entry_type="CR",
            amount=total,
        )
        from inventory.models import VoucherStockItem
        for item in pending_items:
            if item.stock_item_id:
                VoucherStockItem.objects.create(
                    voucher=voucher,
                    stock_item=item.stock_item,
                    quantity=item.pending_qty,
                    rate=item.rate,
                )

        voucher.create_tax_lines()
        voucher.validate_balance()
        voucher.approve(request.user)

        # Mark all items as fully fulfilled
        models_qty_update(order)
        order.fulfilled_voucher = voucher
        order.status = Order.STATUS_FULFILLED
        order.save(update_fields=["fulfilled_voucher", "status", "updated_at"])

    messages.success(request, f"Voucher {voucher.number} created from Order {order.number}.")
    return redirect("vouchers:detail", pk=voucher.pk)


def models_qty_update(order):
    """Helper: returns a dict suitable for bulk_update of fulfilled_qty."""
    from django.db.models import F
    OrderItem.objects.filter(order=order).update(fulfilled_qty=F("quantity"))
    return None


# ── Open Orders Report ────────────────────────────────────────────────────────

@login_required
def open_orders(request):
    company    = request.current_company
    order_type = request.GET.get("type", "")

    qs = Order.objects.filter(
        company=company,
        status__in=[Order.STATUS_CONFIRMED, Order.STATUS_PARTIAL]
    ).select_related("party_ledger").order_by("expected_date", "order_date")

    if order_type:
        qs = qs.filter(order_type=order_type)

    rows = []
    for order in qs:
        items = order.items.select_related("stock_item")
        rows.append({
            "order": order,
            "items": items,
            "overdue": order.expected_date and order.expected_date < _date.today(),
        })

    return render(request, "orders/open_orders.html", {
        "rows": rows, "order_type": order_type, "today": _date.today(),
    })
