"""
tds/views.py — TDS / TCS Management
"""

import csv
from datetime import date as _date
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone

from core.decorators import admin_required, write_required
from core.models import UserCompanyAccess
from .filing_pack import (
    build_tds_filing_pack_from_params,
    challan_csv_bytes,
    deductee_csv_bytes,
    draft_json_bytes,
    mark_tds_filing_pack_filed,
    reopen_tds_filing_pack,
    save_tds_filing_pack,
    tds_pack_xlsx_bytes,
)
from .models import TDSCertificateIssue, TDSPostFilingTracker, TDSReturnWorkpaper, TDSSection, TDSEntry
from .post_filing import (
    build_tds_post_filing_center_from_params,
    save_post_filing_tracker,
    sync_certificates_from_pack,
    update_certificate_issue,
)
from .forms import TDSReturnWorkpaperForm, TDSSectionForm, TDSEntryForm, TDSDepositForm
from .workbench import (
    build_tds_deposit_watch,
    build_tds_return_workbench,
    fy_options,
    parse_workbench_filters,
    tds_deposit_due_date,
)


PAGE_SIZE = 50


def _can_write(request):
    company = getattr(request, "current_company", None)
    if not company:
        return False
    return UserCompanyAccess.objects.filter(
        user=request.user,
        company=company,
        role__in=["Admin", "Accountant"],
    ).exists()


# ── TDS Section ───────────────────────────────────────────────────────────────

@login_required
def section_list(request):
    company  = request.current_company
    sections = TDSSection.objects.filter(company=company).order_by("section_code")
    return render(request, "tds/section_list.html", {"sections": sections})


@login_required
@write_required
def section_create(request):
    company = request.current_company
    if request.method == "POST":
        form = TDSSectionForm(request.POST)
        if form.is_valid():
            sec = form.save(commit=False)
            sec.company = company
            sec.save()
            messages.success(request, f"TDS Section {sec.section_code} created.")
            return redirect("tds:section_list")
    else:
        form = TDSSectionForm()
    return render(request, "tds/section_form.html", {"form": form, "title": "New TDS/TCS Section"})


@login_required
@write_required
def section_edit(request, pk):
    company = request.current_company
    sec     = get_object_or_404(TDSSection, pk=pk, company=company)
    if request.method == "POST":
        form = TDSSectionForm(request.POST, instance=sec)
        if form.is_valid():
            form.save()
            messages.success(request, f"Section {sec.section_code} updated.")
            return redirect("tds:section_list")
    else:
        form = TDSSectionForm(instance=sec)
    return render(request, "tds/section_form.html", {
        "form": form, "title": f"Edit Section {sec.section_code}", "sec": sec,
    })


# ── TDS Entries ───────────────────────────────────────────────────────────────

@login_required
def entry_list(request):
    company    = request.current_company
    deposited  = request.GET.get("deposited", "")
    section_pk = request.GET.get("section", "")
    q          = request.GET.get("q", "").strip()

    qs = TDSEntry.objects.filter(company=company).select_related(
        "section", "deductee_ledger", "voucher"
    ).order_by("-transaction_date")

    if deposited == "1":
        qs = qs.filter(is_deposited=True)
    elif deposited == "0":
        qs = qs.filter(is_deposited=False)
    if section_pk:
        qs = qs.filter(section_id=section_pk)
    if q:
        qs = qs.filter(Q(deductee_ledger__name__icontains=q) | Q(pan_number__icontains=q))

    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="tds_register.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "Date",
            "Section",
            "Party",
            "PAN",
            "Base Amount",
            "Rate %",
            "TDS Amount",
            "Deposit Due Date",
            "Deposited",
            "Deposit Date",
            "Challan Number",
            "BSR Code",
            "Notes",
        ])
        for entry in qs:
            writer.writerow([
                entry.transaction_date.isoformat(),
                entry.section.section_code,
                entry.deductee_ledger.name,
                entry.pan_number,
                entry.deductible_amount,
                entry.rate_applied,
                entry.tds_amount,
                tds_deposit_due_date(entry.transaction_date).isoformat(),
                "Yes" if entry.is_deposited else "No",
                entry.deposit_date.isoformat() if entry.deposit_date else "",
                entry.challan_number,
                entry.bsr_code,
                entry.notes,
            ])
        return response

    paginator = Paginator(qs, PAGE_SIZE)
    page_obj  = paginator.get_page(request.GET.get("page"))
    sections  = TDSSection.objects.filter(company=company, is_active=True).order_by("section_code")

    deposit_watch = build_tds_deposit_watch(company)
    total_payable = deposit_watch["summary"]["pending_amount"]

    return render(request, "tds/entry_list.html", {
        "page_obj":     page_obj,
        "deposited":    deposited,
        "section_pk":   section_pk,
        "sections":     sections,
        "q":            q,
        "total_payable": total_payable,
        "deposit_watch": deposit_watch,
        "export_query": urlencode({
            "section": section_pk,
            "deposited": deposited,
            "q": q,
            "export": "csv",
        }),
    })


@login_required
@write_required
def entry_create(request):
    company = request.current_company
    if request.method == "POST":
        form = TDSEntryForm(request.POST, company=company)
        if form.is_valid():
            entry = form.save(commit=False)
            entry.company = company
            entry.save()
            messages.success(request, f"TDS entry created — ₹{entry.tds_amount}.")
            return redirect("tds:entry_list")
    else:
        form = TDSEntryForm(company=company, initial={"transaction_date": _date.today()})
    return render(request, "tds/entry_form.html", {"form": form, "title": "New TDS/TCS Entry"})


@login_required
@write_required
def entry_edit(request, pk):
    company = request.current_company
    entry   = get_object_or_404(TDSEntry, pk=pk, company=company)
    if request.method == "POST":
        form = TDSEntryForm(request.POST, instance=entry, company=company)
        if form.is_valid():
            form.save()
            messages.success(request, "TDS entry updated.")
            return redirect("tds:entry_list")
    else:
        form = TDSEntryForm(instance=entry, company=company)
    return render(request, "tds/entry_form.html", {
        "form": form, "title": "Edit TDS/TCS Entry", "entry": entry,
    })


@login_required
@admin_required
def entry_delete(request, pk):
    company = request.current_company
    entry   = get_object_or_404(TDSEntry, pk=pk, company=company)
    if request.method == "POST":
        entry.delete()
        messages.success(request, "TDS entry deleted.")
        return redirect("tds:entry_list")
    return render(request, "tds/entry_confirm_delete.html", {"entry": entry})


@login_required
@write_required
def entry_deposit(request, pk):
    """Mark a TDS entry as deposited with challan details."""
    company = request.current_company
    entry   = get_object_or_404(TDSEntry, pk=pk, company=company)
    if request.method == "POST":
        form = TDSDepositForm(request.POST, instance=entry)
        if form.is_valid():
            e = form.save(commit=False)
            e.is_deposited = True
            e.save()
            messages.success(request, f"TDS marked as deposited. Challan: {e.challan_number}")
            return redirect("tds:entry_list")
    else:
        form = TDSDepositForm(instance=entry)
    return render(request, "tds/entry_deposit.html", {"form": form, "entry": entry})


# ── Reports ───────────────────────────────────────────────────────────────────

@login_required
def return_workbench(request):
    company = request.current_company
    params = request.POST if request.method == "POST" else request.GET
    filters = parse_workbench_filters(params)
    workbench = build_tds_return_workbench(
        company=company,
        fy_start=filters["fy_start"],
        quarter=filters["quarter"],
        form_type=filters["form_type"],
    )
    if request.method == "GET" and request.GET.get("export") == "csv":
        summary = workbench["summary"]
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="TDS_Return_Workbench_{summary["form_type"]}_{summary["quarter"]}_FY{summary["fy_label"]}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow([
            "TDS Return Workbench",
            company.name,
            summary["form_type"],
            summary["quarter"],
            f'FY {summary["fy_label"]}',
        ])
        writer.writerow(["Readiness Score", summary["readiness_score"]])
        writer.writerow(["Total TDS", summary["total_tds"]])
        writer.writerow(["Pending Deposit", summary["pending_tds"]])
        writer.writerow([])
        writer.writerow(["Validations"])
        writer.writerow(["Severity", "Code", "Check", "Count", "Description"])
        for item in workbench["validations"]:
            writer.writerow([item["severity"], item["code"], item["title"], item["count"], item["description"]])
        writer.writerow([])
        writer.writerow(["Section Summary"])
        writer.writerow(["Section", "Description", "Entries", "Base Amount", "TDS Amount"])
        for row in workbench["sections"]:
            writer.writerow([row["section"], row["description"], row["entry_count"], row["base_amount"], row["tds_amount"]])
        writer.writerow([])
        writer.writerow(["Challan Summary"])
        writer.writerow(["BSR Code", "Challan Number", "Deposit Date", "Entries", "TDS Amount", "Has Issue"])
        for row in workbench["challans"]:
            writer.writerow([
                row["bsr_code"],
                row["challan_number"],
                row["deposit_date"].isoformat() if row["deposit_date"] else "",
                row["entry_count"],
                row["tds_amount"],
                "Yes" if row["has_issue"] else "No",
            ])
        writer.writerow([])
        writer.writerow(["Deductee Rows"])
        writer.writerow(["Date", "Section", "Party", "PAN", "Base Amount", "Rate %", "TDS Amount", "Deposited", "Deposit Date", "Challan", "BSR", "Issues"])
        for row in workbench["rows"]:
            writer.writerow([
                row["date"].isoformat(),
                row["section"],
                row["party"],
                row["pan"],
                row["base_amount"],
                row["rate"],
                row["tds_amount"],
                "Yes" if row["is_deposited"] else "No",
                row["deposit_date"].isoformat() if row["deposit_date"] else "",
                row["challan_number"],
                row["bsr_code"],
                ", ".join(row["issues"]),
            ])
        return response

    workpaper = workbench["workpaper"] or TDSReturnWorkpaper(
        company=company,
        form_type=filters["form_type"],
        financial_year_start=filters["fy_start"],
        quarter=filters["quarter"],
        period_start=filters["period_start"],
        period_end=filters["period_end"],
        due_date=filters["due_date"],
        form16_status=(
            TDSReturnWorkpaper.FORM16_NOT_REQUESTED
            if filters["form_type"] == TDSReturnWorkpaper.FORM_24Q
            else TDSReturnWorkpaper.FORM16_NOT_APPLICABLE
        ),
    )

    form = TDSReturnWorkpaperForm(request.POST or None, instance=workpaper)
    if request.method == "POST":
        if not _can_write(request):
            return HttpResponseForbidden("You do not have permission to update this workpaper.")
        if form.is_valid():
            action = request.POST.get("action", "save")
            obj = form.save(commit=False)
            obj.company = company
            obj.form_type = filters["form_type"]
            obj.financial_year_start = filters["fy_start"]
            obj.quarter = filters["quarter"]
            obj.period_start = filters["period_start"]
            obj.period_end = filters["period_end"]
            obj.due_date = filters["due_date"]
            if not obj.prepared_by_id:
                obj.prepared_by = request.user

            post_workbench = build_tds_return_workbench(
                company=company,
                fy_start=filters["fy_start"],
                quarter=filters["quarter"],
                form_type=filters["form_type"],
                workpaper_override=obj,
            )
            obj.summary_snapshot = post_workbench["summary_snapshot"]
            obj.validation_snapshot = post_workbench["validation_snapshot"]

            if obj.status == TDSReturnWorkpaper.STATUS_READY_FOR_REVIEW and not post_workbench["summary"]["can_mark_ready"]:
                messages.error(request, "Clear critical TDS return blockers before setting this workpaper ready for review.")
                return redirect(_return_workbench_url(filters))
            if obj.status == TDSReturnWorkpaper.STATUS_FILED and not obj.ack_number:
                messages.error(request, "Add the TRACES acknowledgement number before setting the return filed.")
                return redirect(_return_workbench_url(filters))

            if action == "mark_ready":
                if not post_workbench["summary"]["can_mark_ready"]:
                    messages.error(request, "Clear critical TDS return blockers before marking this workpaper ready.")
                    return redirect(_return_workbench_url(filters))
                obj.status = TDSReturnWorkpaper.STATUS_READY_FOR_REVIEW
                obj.reviewed_by = request.user
            elif action == "mark_filed":
                if not obj.ack_number:
                    messages.error(request, "Add the TRACES acknowledgement number before marking the return filed.")
                    return redirect(_return_workbench_url(filters))
                obj.status = TDSReturnWorkpaper.STATUS_FILED
                obj.filed_by = request.user
                obj.filed_at = timezone.now()
            elif action == "reopen":
                obj.status = TDSReturnWorkpaper.STATUS_REOPENED

            obj.save()
            messages.success(request, "TDS return workpaper updated.")
            return redirect(_return_workbench_url(filters))
        messages.error(request, "Please correct the highlighted TDS workpaper fields.")

    return render(request, "tds/return_workbench.html", {
        "form": form,
        "filters": filters,
        "fy_options": fy_options(filters["fy_start"]),
        "quarter_options": TDSReturnWorkpaper.QUARTER_CHOICES,
        "form_type_options": TDSReturnWorkpaper.FORM_TYPE_CHOICES,
        "workpaper": workbench["workpaper"],
        "rows": workbench["rows"],
        "challans": workbench["challans"],
        "sections": workbench["sections"],
        "summary": workbench["summary"],
        "validations": workbench["validations"],
        "can_write": _can_write(request),
        "export_query": urlencode({
            "fy": filters["fy_start"],
            "quarter": filters["quarter"],
            "form_type": filters["form_type"],
            "export": "csv",
        }),
    })


def _return_workbench_url(filters):
    return (
        f"{reverse('tds:return_workbench')}"
        f"?fy={filters['fy_start']}&quarter={filters['quarter']}&form_type={filters['form_type']}"
    )


@login_required
def filing_pack(request):
    company = request.current_company
    params = request.POST if request.method == "POST" else request.GET
    pack = build_tds_filing_pack_from_params(company, params)
    filters = pack["filters"]
    can_write = _can_write(request)

    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("You do not have permission to update this filing pack.")

        action = request.POST.get("action", "")
        notes = request.POST.get("notes", "")
        try:
            if action == "generate_pack":
                record = save_tds_filing_pack(pack, request.user, notes)
                messages.success(request, f"TDS filing pack generated: {record.get_status_display()}.")
            elif action == "mark_filed":
                if not pack["pack_record"]:
                    raise ValueError("Generate the TDS filing pack before marking it filed.")
                record = mark_tds_filing_pack_filed(
                    pack["pack_record"],
                    request.user,
                    request.POST.get("ack_number", ""),
                    notes,
                )
                messages.success(request, f"TDS filing pack archived as filed with {record.ack_number}.")
            elif action == "reopen_pack":
                if not pack["pack_record"]:
                    raise ValueError("No TDS filing pack exists to reopen.")
                reopen_tds_filing_pack(pack["pack_record"])
                messages.success(request, "TDS filing pack reopened.")
            else:
                messages.error(request, "Invalid TDS filing pack action.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect(_tds_filing_pack_url(filters))

    return render(request, "tds/filing_pack.html", {
        "pack": pack,
        "filters": filters,
        "fy_options": fy_options(filters["fy_start"]),
        "quarter_options": TDSReturnWorkpaper.QUARTER_CHOICES,
        "form_type_options": TDSReturnWorkpaper.FORM_TYPE_CHOICES,
        "can_write": can_write,
    })


@login_required
def filing_pack_download(request, kind):
    company = request.current_company
    pack = build_tds_filing_pack_from_params(company, request.GET)
    if not pack["can_generate"]:
        messages.error(request, "Mark the workpaper ready and clear critical validations before downloading final TDS exports.")
        return redirect(_tds_filing_pack_url(pack["filters"]))

    safe_name = "".join(ch if ch.isalnum() else "_" for ch in company.name).strip("_") or "company"
    suffix = f"{pack['filters']['form_type']}_{pack['filters']['quarter']}_FY{pack['filters']['fy_label']}"
    if kind == "xlsx":
        response = HttpResponse(
            tds_pack_xlsx_bytes(pack),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="TDS_Filing_Pack_{safe_name}_{suffix}.xlsx"'
        return response
    if kind == "deductees":
        response = HttpResponse(deductee_csv_bytes(pack), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="TDS_RPU_Deductees_{safe_name}_{suffix}.csv"'
        return response
    if kind == "challans":
        response = HttpResponse(challan_csv_bytes(pack), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="TDS_RPU_Challans_{safe_name}_{suffix}.csv"'
        return response
    if kind == "json":
        response = HttpResponse(draft_json_bytes(pack), content_type="application/json")
        response["Content-Disposition"] = f'attachment; filename="TDS_Filing_Draft_{safe_name}_{suffix}.json"'
        return response

    raise Http404("Unknown TDS filing pack download.")


def _tds_filing_pack_url(filters):
    query = urlencode({
        'fy': filters['fy_start'],
        'quarter': filters['quarter'],
        'form_type': filters['form_type'],
    })
    return f"{reverse('tds:filing_pack')}?{query}"


@login_required
def post_filing_center(request):
    company = request.current_company
    params = request.POST if request.method == "POST" else request.GET
    center = build_tds_post_filing_center_from_params(company, params)
    filters = center["filters"]
    pack = center["pack"]
    can_write = _can_write(request)

    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("You do not have permission to update this post-filing tracker.")

        action = request.POST.get("action", "")
        try:
            if action == "save_tracker":
                if not pack:
                    raise ValueError("Generate and file the TDS filing pack before saving TRACES status.")
                save_post_filing_tracker(pack, request.user, request.POST)
                messages.success(request, "TRACES post-filing status updated.")
            elif action == "sync_certificates":
                if not pack:
                    raise ValueError("Generate the TDS filing pack before syncing certificate rows.")
                result = sync_certificates_from_pack(pack)
                messages.success(
                    request,
                    f"Certificate rows synced: {result['created']} created, {result['updated']} updated, {result['total']} total.",
                )
            elif action == "update_certificate":
                cert = get_object_or_404(
                    TDSCertificateIssue,
                    pk=request.POST.get("certificate_id"),
                    pack__company=company,
                )
                update_certificate_issue(cert, request.user, request.POST)
                messages.success(request, f"{cert.get_certificate_type_display()} status updated for {cert.deductee_name}.")
            else:
                messages.error(request, "Invalid TDS post-filing action.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect(_tds_post_filing_url(filters))

    return render(request, "tds/post_filing_center.html", {
        "center": center,
        "filters": filters,
        "pack": pack,
        "tracker": center["tracker"],
        "certificates": center["certificates"],
        "summary": center["summary"],
        "validations": center["validations"],
        "fy_options": fy_options(filters["fy_start"]),
        "quarter_options": TDSReturnWorkpaper.QUARTER_CHOICES,
        "form_type_options": TDSReturnWorkpaper.FORM_TYPE_CHOICES,
        "statement_status_choices": TDSPostFilingTracker.STATEMENT_STATUS_CHOICES,
        "report_status_choices": TDSPostFilingTracker.REPORT_STATUS_CHOICES,
        "correction_status_choices": TDSPostFilingTracker.CORRECTION_STATUS_CHOICES,
        "certificate_status_choices": TDSCertificateIssue.STATUS_CHOICES,
        "issue_channel_choices": TDSCertificateIssue.CHANNEL_CHOICES,
        "can_write": can_write,
    })


def _tds_post_filing_url(filters):
    query = urlencode({
        "fy": filters["fy_start"],
        "quarter": filters["quarter"],
        "form_type": filters["form_type"],
    })
    return f"{reverse('tds:post_filing_center')}?{query}"


@login_required
def tds_register(request):
    """TDS register — grouped by section, showing payable vs deposited."""
    company = request.current_company
    fy      = request.GET.get("fy", "")

    # Build FY date range
    if fy:
        try:
            fy_year  = int(fy.split("-")[0])
            from datetime import date as dt
            date_from = dt(fy_year, 4, 1)
            date_to   = dt(fy_year + 1, 3, 31)
        except (ValueError, IndexError):
            date_from = date_to = None
    else:
        date_from = date_to = None

    qs = TDSEntry.objects.filter(company=company).select_related("section", "deductee_ledger")
    if date_from and date_to:
        qs = qs.filter(transaction_date__range=(date_from, date_to))

    from django.db.models import Sum, Count
    # Aggregate by section
    summary = []
    for sec in TDSSection.objects.filter(company=company, is_active=True):
        sec_entries = qs.filter(section=sec)
        total     = sec_entries.aggregate(t=Sum("tds_amount"))["t"] or 0
        deposited = sec_entries.filter(is_deposited=True).aggregate(d=Sum("tds_amount"))["d"] or 0
        count     = sec_entries.count()
        if count:
            summary.append({
                "section":   sec,
                "count":     count,
                "total":     total,
                "deposited": deposited,
                "payable":   total - deposited,
            })

    total_tds       = sum(r["total"]     for r in summary)
    total_deposited = sum(r["deposited"] for r in summary)
    total_payable   = sum(r["payable"]   for r in summary)

    # Available FYs
    from costcenter.views import _current_fy
    current_fy = _current_fy()
    fy_start = int(current_fy.split("-")[0])
    fy_list  = [f"{y}-{str(y+1)[-2:]}" for y in range(fy_start - 3, fy_start + 1)]

    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="tds_section_summary.csv"'
        writer = csv.writer(response)
        writer.writerow(["Section", "Nature", "Description", "Entries", "Total TDS", "Deposited", "Payable"])
        for row in summary:
            writer.writerow([
                row["section"].section_code,
                row["section"].nature,
                row["section"].description,
                row["count"],
                f"{row['total']:.2f}",
                f"{row['deposited']:.2f}",
                f"{row['payable']:.2f}",
            ])
        writer.writerow([])
        writer.writerow(["TOTAL", "", "", "", f"{total_tds:.2f}", f"{total_deposited:.2f}", f"{total_payable:.2f}"])
        return response

    return render(request, "tds/tds_register.html", {
        "summary":        summary,
        "fy":             fy,
        "fy_list":        fy_list,
        "total_tds":      total_tds,
        "total_deposited": total_deposited,
        "total_payable":  total_payable,
    })
