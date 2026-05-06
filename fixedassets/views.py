"""
fixedassets/views.py — Fixed Assets & Depreciation
"""

from datetime import date as _date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from core.decorators import admin_required, write_required
from .models import AssetGroup, FixedAsset, AssetDepreciation
from .forms import AssetGroupForm, FixedAssetForm, AssetDisposalForm


def _current_fy():
    today = _date.today()
    year  = today.year if today.month >= 4 else today.year - 1
    return f"{year}-{str(year + 1)[-2:]}"


# ═══════════════════════════════════════════════════════════════════════════════
# ASSET GROUP
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def asset_group_list(request):
    company = request.current_company
    groups  = AssetGroup.objects.filter(company=company).select_related(
        "asset_ledger", "depreciation_ledger"
    ).order_by("name")
    return render(request, "fixedassets/asset_group_list.html", {"groups": groups})


@login_required
@write_required
def asset_group_create(request):
    company = request.current_company
    if request.method == "POST":
        form = AssetGroupForm(request.POST, company=company)
        if form.is_valid():
            grp = form.save(commit=False)
            grp.company = company
            grp.save()
            messages.success(request, f"Asset group '{grp.name}' created.")
            return redirect("fixedassets:asset_group_list")
    else:
        form = AssetGroupForm(company=company)
    return render(request, "fixedassets/asset_group_form.html", {"form": form, "title": "New Asset Group"})


@login_required
@write_required
def asset_group_edit(request, pk):
    company = request.current_company
    grp     = get_object_or_404(AssetGroup, pk=pk, company=company)
    if request.method == "POST":
        form = AssetGroupForm(request.POST, instance=grp, company=company)
        if form.is_valid():
            form.save()
            messages.success(request, f"Asset group '{grp.name}' updated.")
            return redirect("fixedassets:asset_group_list")
    else:
        form = AssetGroupForm(instance=grp, company=company)
    return render(request, "fixedassets/asset_group_form.html", {
        "form": form, "title": f"Edit {grp.name}", "grp": grp,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# FIXED ASSET CRUD
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def asset_list(request):
    company = request.current_company
    status  = request.GET.get("status", "Active")
    group   = request.GET.get("group", "")
    q       = request.GET.get("q", "").strip()

    qs = FixedAsset.objects.filter(company=company).select_related("asset_group")
    if status:
        qs = qs.filter(status=status)
    if group:
        qs = qs.filter(asset_group_id=group)
    if q:
        qs = qs.filter(name__icontains=q) | qs.filter(asset_code__icontains=q)

    groups = AssetGroup.objects.filter(company=company, is_active=True).order_by("name")

    return render(request, "fixedassets/asset_list.html", {
        "assets": qs, "status": status, "group": group, "q": q, "groups": groups,
        "fy": _current_fy(),
    })


@login_required
@write_required
def asset_create(request):
    company = request.current_company
    if request.method == "POST":
        form = FixedAssetForm(request.POST, company=company)
        if form.is_valid():
            asset = form.save(commit=False)
            asset.company = company
            asset.save()
            messages.success(request, f"Fixed asset '{asset.name}' added.")
            return redirect("fixedassets:asset_detail", pk=asset.pk)
    else:
        form = FixedAssetForm(company=company, initial={"purchase_date": _date.today()})
    return render(request, "fixedassets/asset_form.html", {"form": form, "title": "Add Fixed Asset"})


@login_required
@write_required
def asset_edit(request, pk):
    company = request.current_company
    asset   = get_object_or_404(FixedAsset, pk=pk, company=company)
    if request.method == "POST":
        form = FixedAssetForm(request.POST, instance=asset, company=company)
        if form.is_valid():
            form.save()
            messages.success(request, f"Asset '{asset.name}' updated.")
            return redirect("fixedassets:asset_detail", pk=pk)
    else:
        form = FixedAssetForm(instance=asset, company=company)
    return render(request, "fixedassets/asset_form.html", {
        "form": form, "title": f"Edit {asset.name}", "asset": asset,
    })


@login_required
def asset_detail(request, pk):
    company = request.current_company
    asset   = get_object_or_404(FixedAsset, pk=pk, company=company)
    deprs   = asset.depreciations.order_by("financial_year")
    return render(request, "fixedassets/asset_detail.html", {
        "asset": asset, "deprs": deprs, "current_fy": _current_fy(),
    })


@login_required
@admin_required
def asset_delete(request, pk):
    company = request.current_company
    asset   = get_object_or_404(FixedAsset, pk=pk, company=company)
    if request.method == "POST":
        name = asset.name
        asset.delete()
        messages.success(request, f"Asset '{name}' deleted.")
        return redirect("fixedassets:asset_list")
    return render(request, "fixedassets/asset_confirm_delete.html", {"asset": asset})


# ═══════════════════════════════════════════════════════════════════════════════
# DEPRECIATION
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
@write_required
def depreciation_post(request, pk):
    """
    Compute and post depreciation for a fixed asset for the current FY.
    Creates an AssetDepreciation record and optionally a journal voucher.
    """
    company = request.current_company
    asset   = get_object_or_404(FixedAsset, pk=pk, company=company)
    fy      = request.GET.get("fy", _current_fy())

    if asset.status == FixedAsset.STATUS_DISPOSED:
        messages.error(request, "Cannot post depreciation for a disposed asset.")
        return redirect("fixedassets:asset_detail", pk=pk)

    # Check if already posted for this FY
    if AssetDepreciation.objects.filter(asset=asset, financial_year=fy).exists():
        messages.warning(request, f"Depreciation already posted for FY {fy}.")
        return redirect("fixedassets:asset_detail", pk=pk)

    if request.method != "POST":
        opening_bv   = asset.book_value
        depr_amount  = asset.compute_depreciation_for_fy(opening_bv)
        closing_bv   = opening_bv - depr_amount
        return render(request, "fixedassets/depreciation_confirm.html", {
            "asset":       asset,
            "fy":          fy,
            "opening_bv":  opening_bv,
            "depr_amount": depr_amount,
            "closing_bv":  closing_bv,
        })

    with transaction.atomic():
        opening_bv  = asset.book_value
        depr_amount = asset.compute_depreciation_for_fy(opening_bv)
        closing_bv  = opening_bv - depr_amount

        depr = AssetDepreciation.objects.create(
            asset               = asset,
            financial_year      = fy,
            book_value_opening  = opening_bv,
            depreciation_amount = depr_amount,
            book_value_closing  = closing_bv,
            posted_at           = timezone.now(),
        )

        # ── Optionally create a journal voucher ────────────────────────────
        grp = asset.asset_group
        if grp.depreciation_ledger and grp.accumulated_depr_ledger:
            from vouchers.models import Voucher, VoucherItem
            # Determine FY end date (March 31)
            fy_start_year = int(fy.split("-")[0])
            from datetime import date as dt
            voucher_date = dt(fy_start_year + 1, 3, 31)

            voucher = Voucher(
                company     = company,
                voucher_type = "Journal",
                date        = voucher_date,
                narration   = f"Depreciation on {asset.name} for FY {fy}",
            )
            voucher.save()

            # Dr Depreciation Expense / Cr Accumulated Depreciation
            VoucherItem.objects.create(
                voucher=voucher, ledger=grp.depreciation_ledger,
                entry_type="DR", amount=depr_amount,
            )
            VoucherItem.objects.create(
                voucher=voucher, ledger=grp.accumulated_depr_ledger,
                entry_type="CR", amount=depr_amount,
            )
            voucher.validate_balance()
            voucher.approve(request.user)

            depr.posted_voucher = voucher
            depr.save(update_fields=["posted_voucher"])

    messages.success(request,
        f"Depreciation of ₹{depr_amount} posted for '{asset.name}' — FY {fy}.")
    return redirect("fixedassets:asset_detail", pk=pk)


@login_required
def asset_register(request):
    """Fixed Asset Register — full list with book values and depreciation schedule."""
    company = request.current_company
    fy      = request.GET.get("fy", _current_fy())
    status  = request.GET.get("status", "Active")

    assets  = FixedAsset.objects.filter(company=company).select_related(
        "asset_group"
    ).prefetch_related("depreciations")
    if status:
        assets = assets.filter(status=status)

    rows = []
    for asset in assets:
        depr_this_fy = asset.depreciations.filter(financial_year=fy).first()
        rows.append({
            "asset":        asset,
            "book_value":   asset.book_value,
            "depr_this_fy": depr_this_fy,
            "already_posted": depr_this_fy is not None,
        })

    total_purchase  = sum(r["asset"].purchase_value for r in rows)
    total_accum     = sum(r["asset"].accumulated_depreciation for r in rows)
    total_book      = sum(r["book_value"] for r in rows)

    return render(request, "fixedassets/asset_register.html", {
        "rows": rows, "fy": fy, "status": status,
        "total_purchase": total_purchase,
        "total_accum":    total_accum,
        "total_book":     total_book,
    })
