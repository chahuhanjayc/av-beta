"""
inventory/views.py

Views:
  stock_item_list      — Paginated list of stock items for the company
  stock_item_create    — Create a new stock item
  stock_item_edit      — Edit an existing stock item
  stock_item_deactivate— Soft-delete (Admin only)
  stock_summary        — Stock Summary report (opening, inward, outward, closing)
  stock_valuation      — Closing stock value at WAC per item
  low_stock_alert      — Items below their low_stock_threshold
  item_autocomplete    — AJAX: returns JSON list of stock items (for search)
  item_price_lookup    — AJAX: returns purchase/selling price for an item
  godown_list          — List all godowns for the company
  godown_create        — Create a new godown
  godown_edit          — Edit an existing godown
  godown_delete        — Delete a godown (if unused)
  batch_list           — List all batches for a stock item
  batch_create         — Create a new batch
  batch_edit           — Edit an existing batch
  batch_delete         — Delete a batch
  godown_stock         — AJAX: stock-by-godown breakdown for an item
"""

import json
from datetime import date as _date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST

from core.decorators import admin_required, write_required
from .models import StockItem, StockLedger, Godown, Batch
from .forms import StockItemForm, GodownForm, BatchForm


PAGE_SIZE = 30


def _request_data(request):
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, ValueError, TypeError):
        return request.POST.dict()


def _clean_text(value):
    return " ".join(str(value or "").split()).strip()


def _decimal_or_zero(value, places="0.00"):
    try:
        return Decimal(str(value or places))
    except Exception:
        return Decimal(places)


def _batch_label(batch):
    godown_name = batch.godown.name if batch.godown_id else "No godown"
    return f"{batch.stock_item.name} | {batch.batch_number} | {godown_name}"


# ─────────────────────────────────────────────────────────────────────────────
# LIST
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def stock_item_list(request):
    company = request.current_company
    q       = request.GET.get("q", "").strip()
    show    = request.GET.get("show", "active")  # active | inactive | all

    qs = StockItem.objects.filter(company=company).select_related("hsn_sac", "tax_rate")

    if show == "inactive":
        qs = qs.filter(is_active=False)
    elif show == "all":
        pass
    else:
        qs = qs.filter(is_active=True)

    if q:
        qs = qs.filter(name__icontains=q)

    paginator = Paginator(qs, PAGE_SIZE)
    page_obj  = paginator.get_page(request.GET.get("page"))

    # Annotate each item with live closing qty (today)
    today = _date.today()
    items_with_qty = []
    for item in page_obj:
        closing = item.closing_quantity(end_date=today)
        items_with_qty.append({
            "item":    item,
            "closing": closing,
            "low":     item.is_low_stock(end_date=today),
        })

    return render(request, "inventory/stock_item_list.html", {
        "page_obj":       page_obj,
        "items_with_qty": items_with_qty,
        "q":              q,
        "show":           show,
        "total_count":    qs.count(),
    })


# ─────────────────────────────────────────────────────────────────────────────
# CREATE
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@write_required
def stock_item_create(request):
    company = request.current_company

    if request.method == "POST":
        form = StockItemForm(request.POST, company=company)
        if form.is_valid():
            item = form.save(commit=False)
            item.company = company
            item.save()
            messages.success(request, f"Stock item '{item.name}' created successfully.")
            return redirect("inventory:list")
    else:
        form = StockItemForm(company=company)

    return render(request, "inventory/stock_item_form.html", {
        "form":  form,
        "title": "Add Stock Item",
    })


@login_required
@write_required
def stock_item_bulk_ocr(request):
    """
    Step 1: Upload PDF/Image.
    Step 2: Parse table items.
    Step 3: Show review table.
    Step 4: Save all confirmed items.
    """
    company = request.current_company
    
    if request.method == "POST" and "file" in request.FILES:
        # ─── Case A: File Upload (AJAX or Sync) ───
        file_obj = request.FILES["file"]
        from ocr import ocr_utils
        try:
            pdf_bytes = ocr_utils.read_file_safely(file_obj)
            # Use invoice parser as it's the most robust table extractor
            result = ocr_utils.process_pdf(pdf_bytes, doc_type="invoice")
            items = result.get("line_items", [])
            
            # Map items to match StockItem fields
            for it in items:
                it['name'] = it.get('name', 'New Item').strip()
                it['hsn'] = it.get('hsn', '').strip()
                it['purchase_price'] = it.get('rate', '0.00').replace(',', '')
                # Default selling price +20%
                try:
                    pp = Decimal(it['purchase_price'])
                    it['selling_price'] = str((pp * Decimal('1.20')).quantize(Decimal('0.01')))
                except:
                    it['selling_price'] = '0.00'
                
            return render(request, "inventory/stock_item_bulk_ocr.html", {
                "items": items,
                "title": "Review Scanned Items",
                "filename": file_obj.name
            })
        except Exception as e:
            messages.error(request, f"OCR Failed: {e}")
            return redirect("inventory:create")

    elif request.method == "POST" and "confirm_save" in request.POST:
        # ─── Case B: Bulk Save ───
        item_names = request.POST.getlist("item_name")
        hsns = request.POST.getlist("hsn")
        purchase_prices = request.POST.getlist("purchase_price")
        selling_prices = request.POST.getlist("selling_price")
        tax_rates = request.POST.getlist("tax_rate")
        
        created_count = 0
        from core.models import GSTHSN, GSTTaxRate
        
        with transaction.atomic():
            for i in range(len(item_names)):
                name = item_names[i].strip()
                if not name: continue
                if StockItem.objects.filter(company=company, name__iexact=name).exists():
                    continue
                
                hsn_code = hsns[i].strip()
                tax_pct = tax_rates[i].strip().replace('%', '')
                
                hsn_obj = None
                if hsn_code:
                    hsn_obj, _ = GSTHSN.objects.get_or_create(code=hsn_code, defaults={'description': 'Auto-created'})
                
                tax_obj = None
                if tax_pct:
                    try:
                        tax_obj = GSTTaxRate.objects.filter(rate=Decimal(tax_pct)).first()
                    except: pass

                StockItem.objects.create(
                    company=company,
                    name=name,
                    unit="Pcs",
                    hsn_sac=hsn_obj,
                    tax_rate=tax_obj,
                    purchase_price=Decimal(purchase_prices[i] or '0'),
                    selling_price=Decimal(selling_prices[i] or '0'),
                    opening_quantity=0
                )
                created_count += 1
        
        messages.success(request, f"Successfully created {created_count} stock items.")
        return redirect("inventory:list")

    return render(request, "inventory/stock_item_bulk_ocr_upload.html", {
        "title": "Bulk Scan Stock Items"
    })


# ─────────────────────────────────────────────────────────────────────────────
# EDIT
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@write_required
def stock_item_edit(request, pk):
    company = request.current_company
    item    = get_object_or_404(StockItem, pk=pk, company=company)

    # Track as recent item
    from core.utils.search_utils import add_recent_item
    from django.urls import reverse
    add_recent_item(request, 'items', item.id, item.name, reverse('inventory:edit', args=[item.id]))

    if request.method == "POST":
        form = StockItemForm(request.POST, instance=item, company=company)
        if form.is_valid():
            form.save()
            messages.success(request, f"Stock item '{item.name}' updated.")
            return redirect("inventory:list")
    else:
        form = StockItemForm(instance=item, company=company)

    return render(request, "inventory/stock_item_form.html", {
        "form":  form,
        "item":  item,
        "title": f"Edit — {item.name}",
    })


# ─────────────────────────────────────────────────────────────────────────────
# DEACTIVATE (soft-delete)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@admin_required
def stock_item_deactivate(request, pk):
    company = request.current_company
    item    = get_object_or_404(StockItem, pk=pk, company=company)

    if request.method == "POST":
        item.is_active = False
        item.save(update_fields=["is_active", "updated_at"])
        messages.warning(request, f"'{item.name}' deactivated.")
        return redirect("inventory:list")

    return render(request, "inventory/stock_item_confirm_deactivate.html", {"item": item})


# ─────────────────────────────────────────────────────────────────────────────
# STOCK SUMMARY REPORT
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def stock_summary(request):
    """
    Enhanced Stock Summary: shows balances grouped by Godown.
    Item | Godown | Opening | Inward | Outward | Closing
    """
    company = request.current_company
    godown_id = request.GET.get("godown")
    
    from django.db.models import Sum, Q
    
    # Base Query
    ledgers = StockLedger.objects.filter(stock_item__company=company).select_related('stock_item', 'godown')
    
    if godown_id:
        ledgers = ledgers.filter(godown_id=godown_id)

    # Aggregation
    items = StockItem.objects.filter(company=company, is_active=True).order_by('name')
    godowns = Godown.objects.filter(company=company, is_active=True).order_by('name')
    
    rows = []
    for item in items:
        # Filter godowns that actually have movement for this item
        active_godowns = godowns
        if godown_id:
            active_godowns = godowns.filter(id=godown_id)
            
        for gd in active_godowns:
            qs = StockLedger.objects.filter(stock_item=item, godown=gd)
            
            inward = qs.filter(quantity__gt=0).aggregate(s=Sum('quantity'))['s'] or Decimal('0')
            outward = abs(qs.filter(quantity__lt=0).aggregate(s=Sum('quantity'))['s'] or Decimal('0'))
            closing = inward - outward # Simple foundational logic
            
            if inward > 0 or outward > 0:
                rows.append({
                    'item': item,
                    'godown': gd,
                    'inward': inward,
                    'outward': outward,
                    'closing': closing,
                })

    return render(request, "inventory/stock_summary.html", {
        "rows": rows,
        "godowns": godowns,
        "selected_godown": godown_id,
    })


# ─────────────────────────────────────────────────────────────────────────────
# STOCK VALUATION REPORT
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def stock_valuation(request):
    """
    Closing stock value at Weighted Average Cost per item.
    Shows: Item | Unit | Closing Qty | WAC Rate | Stock Value
    """
    company  = request.current_company
    as_of    = request.GET.get("as_of", "").strip()

    from datetime import datetime
    parsed_as_of = None
    try:
        if as_of:
            parsed_as_of = datetime.strptime(as_of, "%Y-%m-%d").date()
    except ValueError:
        parsed_as_of = None

    if not parsed_as_of:
        parsed_as_of = _date.today()
        as_of        = parsed_as_of.strftime("%Y-%m-%d")

    items = StockItem.objects.filter(
        company=company, is_active=True
    ).select_related("hsn_sac", "tax_rate").order_by("name")

    rows              = []
    total_stock_value = Decimal("0.00")

    for item in items:
        closing = item.closing_quantity(end_date=parsed_as_of)
        wac     = item.weighted_average_cost()
        value   = (closing * wac).quantize(Decimal("0.01"))
        rows.append({
            "item":    item,
            "closing": closing,
            "wac":     wac,
            "value":   value,
        })
        total_stock_value += value

    return render(request, "inventory/stock_valuation.html", {
        "rows":              rows,
        "as_of":             as_of,
        "total_stock_value": total_stock_value,
    })


# ─────────────────────────────────────────────────────────────────────────────
# LOW STOCK ALERT
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def low_stock_alert(request):
    """
    Items with closing qty < low_stock_threshold (and threshold > 0).
    """
    company = request.current_company
    today   = _date.today()

    # Only items where a threshold is configured
    items = StockItem.objects.filter(
        company=company, is_active=True, low_stock_threshold__gt=0
    ).select_related("hsn_sac", "tax_rate").order_by("name")

    low_items = []
    for item in items:
        closing = item.closing_quantity(end_date=today)
        if closing < item.low_stock_threshold:
            shortfall = item.low_stock_threshold - closing
            low_items.append({
                "item":      item,
                "closing":   closing,
                "threshold": item.low_stock_threshold,
                "shortfall": shortfall,
            })

    return render(request, "inventory/low_stock_alert.html", {
        "low_items": low_items,
        "today":     today,
    })


# ─────────────────────────────────────────────────────────────────────────────
# AJAX HELPERS
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def item_price_lookup(request, pk):
    """
    AJAX endpoint: given a StockItem pk, return its default purchase/selling
    price so the voucher form can auto-fill the rate field.
    """
    company = request.current_company
    try:
        item = StockItem.objects.get(pk=pk, company=company, is_active=True)
    except StockItem.DoesNotExist:
        return JsonResponse({"error": "Item not found"}, status=404)

    return JsonResponse({
        "purchase_price": str(item.purchase_price),
        "selling_price":  str(item.selling_price),
        "unit":           item.unit,
        "name":           item.name,
    })

# ─────────────────────────────────────────────────────────────────────────────
# GODOWNS — CRUD
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def godown_list(request):
    company  = request.current_company
    godowns  = Godown.objects.filter(company=company).order_by("name")
    return render(request, "inventory/godown_list.html", {"godowns": godowns})


@login_required
@write_required
def godown_create(request):
    company = request.current_company
    if request.method == "POST":
        form = GodownForm(request.POST, company=company)
        if form.is_valid():
            godown = form.save(commit=False)
            godown.company = company
            godown.save()
            messages.success(request, f"Godown '{godown.name}' created.")
            return redirect("inventory:godown_list")
    else:
        form = GodownForm(company=company)
    return render(request, "inventory/godown_form.html", {"form": form, "title": "Add Godown"})


@login_required
@write_required
@require_POST
def godown_quick_add(request):
    company = request.current_company
    data = _request_data(request)
    name = _clean_text(data.get("name"))
    location = _clean_text(data.get("location"))

    if not name:
        return JsonResponse({"success": False, "error": "Godown name is required."}, status=400)

    existing = Godown.objects.filter(company=company, name__iexact=name).first()
    if existing:
        changed = False
        if not existing.is_active:
            existing.is_active = True
            changed = True
        if location and not existing.location:
            existing.location = location
            changed = True
        if changed:
            existing.save(update_fields=["is_active", "location"])
        return JsonResponse({
            "success": True,
            "created": False,
            "id": existing.pk,
            "name": existing.name,
            "location": existing.location,
        })

    try:
        godown = Godown.objects.create(
            company=company,
            name=name,
            location=location,
            is_active=True,
        )
    except IntegrityError:
        godown = Godown.objects.get(company=company, name__iexact=name)
        return JsonResponse({
            "success": True,
            "created": False,
            "id": godown.pk,
            "name": godown.name,
            "location": godown.location,
        })

    return JsonResponse({
        "success": True,
        "created": True,
        "id": godown.pk,
        "name": godown.name,
        "location": godown.location,
    })


@login_required
@write_required
def godown_edit(request, pk):
    company = request.current_company
    godown  = get_object_or_404(Godown, pk=pk, company=company)
    if request.method == "POST":
        form = GodownForm(request.POST, instance=godown, company=company)
        if form.is_valid():
            form.save()
            messages.success(request, f"Godown '{godown.name}' updated.")
            return redirect("inventory:godown_list")
    else:
        form = GodownForm(instance=godown, company=company)
    return render(request, "inventory/godown_form.html", {
        "form": form, "godown": godown, "title": f"Edit — {godown.name}",
    })


@login_required
@admin_required
def godown_delete(request, pk):
    company = request.current_company
    godown  = get_object_or_404(Godown, pk=pk, company=company)
    if request.method == "POST":
        try:
            godown.delete()
            messages.success(request, f"Godown '{godown.name}' deleted.")
        except ProtectedError:
            messages.error(request, "Cannot delete godown — it has stock movements linked to it.")
        return redirect("inventory:godown_list")
    return render(request, "inventory/godown_confirm_delete.html", {"godown": godown})


# ─────────────────────────────────────────────────────────────────────────────
# BATCHES — CRUD
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def batch_list(request):
    company = request.current_company
    item_pk = request.GET.get("item")
    batches = Batch.objects.filter(stock_item__company=company).select_related("stock_item")
    items   = StockItem.objects.filter(company=company, is_active=True).order_by("name")
    if item_pk:
        batches = batches.filter(stock_item_id=item_pk)
    batches = batches.order_by("stock_item__name", "batch_number")
    return render(request, "inventory/batch_list.html", {
        "batches": batches, "items": items, "item_pk": item_pk,
    })


@login_required
@write_required
def batch_create(request):
    company = request.current_company
    if request.method == "POST":
        form = BatchForm(request.POST, company=company)
        if form.is_valid():
            batch = form.save()
            messages.success(request, f"Batch '{batch.batch_number}' created.")
            return redirect("inventory:batch_list")
    else:
        # Pre-select item if passed as query param
        initial = {}
        item_pk = request.GET.get("item")
        if item_pk:
            initial["stock_item"] = item_pk
        form = BatchForm(company=company, initial=initial)
    return render(request, "inventory/batch_form.html", {"form": form, "title": "Add Batch"})


@login_required
@write_required
@require_POST
def batch_quick_add(request):
    company = request.current_company
    data = _request_data(request)
    stock_item_id = data.get("stock_item_id") or data.get("stock_item")
    godown_id = data.get("godown_id") or data.get("godown")
    batch_number = _clean_text(data.get("batch_number") or data.get("name"))

    if not stock_item_id:
        return JsonResponse({"success": False, "error": "Select a stock item before adding a batch."}, status=400)
    if not batch_number:
        return JsonResponse({"success": False, "error": "Batch number is required."}, status=400)

    stock_item = StockItem.objects.filter(
        pk=stock_item_id,
        company=company,
        is_active=True,
    ).first()
    if not stock_item:
        return JsonResponse({"success": False, "error": "Stock item not found."}, status=400)

    godown = None
    if godown_id:
        godown = Godown.objects.filter(
            pk=godown_id,
            company=company,
            is_active=True,
        ).first()
        if not godown:
            return JsonResponse({"success": False, "error": "Godown not found."}, status=400)

    expiry_raw = _clean_text(data.get("expiry_date"))
    expiry_date = None
    if expiry_raw:
        try:
            expiry_date = _date.fromisoformat(expiry_raw)
        except ValueError:
            return JsonResponse({"success": False, "error": "Expiry date is invalid."}, status=400)

    existing = Batch.objects.filter(
        Q(company=company) | Q(company__isnull=True),
        stock_item=stock_item,
        batch_number__iexact=batch_number,
        godown=godown,
    ).select_related("stock_item", "godown").first()
    if existing:
        changed = False
        if existing.company_id is None:
            existing.company = company
            changed = True
        if changed:
            existing.save(update_fields=["company"])
        return JsonResponse({
            "success": True,
            "created": False,
            "id": existing.pk,
            "name": _batch_label(existing),
            "batch_number": existing.batch_number,
            "stock_item_id": existing.stock_item_id,
            "godown_id": existing.godown_id or "",
        })

    try:
        batch = Batch.objects.create(
            company=company,
            stock_item=stock_item,
            godown=godown,
            batch_number=batch_number,
            expiry_date=expiry_date,
            purchase_rate=_decimal_or_zero(data.get("purchase_rate")),
            quantity=_decimal_or_zero(data.get("quantity"), "0.000"),
        )
    except IntegrityError:
        batch = Batch.objects.filter(
            company=company,
            stock_item=stock_item,
            batch_number__iexact=batch_number,
            godown=godown,
        ).select_related("stock_item", "godown").first()
        if not batch:
            return JsonResponse({"success": False, "error": "Batch could not be created."}, status=400)
        created = False
    else:
        created = True

    return JsonResponse({
        "success": True,
        "created": created,
        "id": batch.pk,
        "name": _batch_label(batch),
        "batch_number": batch.batch_number,
        "stock_item_id": batch.stock_item_id,
        "godown_id": batch.godown_id or "",
    })


@login_required
@write_required
def batch_edit(request, pk):
    company = request.current_company
    batch   = get_object_or_404(Batch, pk=pk, stock_item__company=company)
    if request.method == "POST":
        form = BatchForm(request.POST, instance=batch, company=company)
        if form.is_valid():
            form.save()
            messages.success(request, f"Batch '{batch.batch_number}' updated.")
            return redirect("inventory:batch_list")
    else:
        form = BatchForm(instance=batch, company=company)
    return render(request, "inventory/batch_form.html", {
        "form": form, "batch": batch, "title": f"Edit Batch — {batch.batch_number}",
    })


@login_required
@admin_required
def batch_delete(request, pk):
    company = request.current_company
    batch   = get_object_or_404(Batch, pk=pk, stock_item__company=company)
    if request.method == "POST":
        label = str(batch)
        batch.delete()
        messages.success(request, f"Batch '{label}' deleted.")
        return redirect("inventory:batch_list")
    return render(request, "inventory/batch_confirm_delete.html", {"batch": batch})


# ─────────────────────────────────────────────────────────────────────────────
# AJAX: godown-wise stock breakdown for a StockItem
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def batch_summary(request):
    """
    Step 5: Batch Summary Report.
    Shows Item, Batch, Godown, Expiry, and Quantity.
    """
    company = request.current_company
    
    batches = Batch.objects.filter(
        company=company,
        quantity__gt=0
    ).select_related('stock_item', 'godown').order_by('expiry_date', 'stock_item__name')

    # Step 6: Alerts for near expiry and expired stock
    from datetime import date, timedelta
    today = date.today()
    near_expiry_threshold = today + timedelta(days=30)
    
    rows = []
    for b in batches:
        is_expired = False
        is_near_expiry = False
        if b.expiry_date:
            is_expired = b.expiry_date < today
            is_near_expiry = not is_expired and b.expiry_date <= near_expiry_threshold
            
        rows.append({
            'item_name': b.stock_item.name,
            'batch_number': b.batch_number,
            'godown_name': b.godown.name if b.godown else "Unassigned",
            'expiry_date': b.expiry_date,
            'quantity': b.quantity,
            'is_expired': is_expired,
            'is_near_expiry': is_near_expiry,
        })
        
    return render(request, "inventory/batch_summary.html", {"rows": rows})
