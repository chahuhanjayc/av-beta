"""TDS return workbench calculations and validation helpers."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
import re

from django.urls import reverse
from django.utils import timezone

from .models import TDSEntry, TDSReturnWorkpaper


PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")
BSR_RE = re.compile(r"^[0-9]{7}$")
SALARY_SECTION_PREFIXES = ("192",)
ZERO = Decimal("0.00")


def financial_year_label(fy_start):
    return f"{fy_start}-{str(fy_start + 1)[-2:]}"


def current_financial_year_start(today=None):
    today = today or timezone.localdate()
    return today.year if today.month >= 4 else today.year - 1


def quarter_for_date(value):
    if value.month in (4, 5, 6):
        return current_financial_year_start(value), TDSReturnWorkpaper.Q1
    if value.month in (7, 8, 9):
        return current_financial_year_start(value), TDSReturnWorkpaper.Q2
    if value.month in (10, 11, 12):
        return current_financial_year_start(value), TDSReturnWorkpaper.Q3
    return current_financial_year_start(value), TDSReturnWorkpaper.Q4


def default_return_period(today=None):
    """Default to the last completed TDS quarter, not the active quarter."""
    today = today or timezone.localdate()
    fy_start, quarter = quarter_for_date(today)
    if quarter == TDSReturnWorkpaper.Q1:
        return fy_start - 1, TDSReturnWorkpaper.Q4
    if quarter == TDSReturnWorkpaper.Q2:
        return fy_start, TDSReturnWorkpaper.Q1
    if quarter == TDSReturnWorkpaper.Q3:
        return fy_start, TDSReturnWorkpaper.Q2
    return fy_start, TDSReturnWorkpaper.Q3


def parse_workbench_filters(params, today=None):
    default_fy, default_quarter = default_return_period(today)
    fy_raw = (params.get("fy") or "").strip()
    quarter = (params.get("quarter") or default_quarter).strip().upper()
    form_type = (params.get("form_type") or TDSReturnWorkpaper.FORM_26Q).strip().upper()

    try:
        fy_start = int(fy_raw[:4]) if fy_raw else default_fy
    except (TypeError, ValueError):
        fy_start = default_fy

    valid_quarters = {choice[0] for choice in TDSReturnWorkpaper.QUARTER_CHOICES}
    valid_forms = {choice[0] for choice in TDSReturnWorkpaper.FORM_TYPE_CHOICES}
    if quarter not in valid_quarters:
        quarter = default_quarter
    if form_type not in valid_forms:
        form_type = TDSReturnWorkpaper.FORM_26Q

    period_start, period_end = quarter_dates(fy_start, quarter)
    return {
        "fy_start": fy_start,
        "fy_label": financial_year_label(fy_start),
        "quarter": quarter,
        "form_type": form_type,
        "period_start": period_start,
        "period_end": period_end,
        "due_date": return_due_date(fy_start, quarter),
    }


def quarter_dates(fy_start, quarter):
    if quarter == TDSReturnWorkpaper.Q1:
        return date(fy_start, 4, 1), date(fy_start, 6, 30)
    if quarter == TDSReturnWorkpaper.Q2:
        return date(fy_start, 7, 1), date(fy_start, 9, 30)
    if quarter == TDSReturnWorkpaper.Q3:
        return date(fy_start, 10, 1), date(fy_start, 12, 31)
    return date(fy_start + 1, 1, 1), date(fy_start + 1, 3, 31)


def return_due_date(fy_start, quarter):
    if quarter == TDSReturnWorkpaper.Q1:
        return date(fy_start, 7, 31)
    if quarter == TDSReturnWorkpaper.Q2:
        return date(fy_start, 10, 31)
    if quarter == TDSReturnWorkpaper.Q3:
        return date(fy_start + 1, 1, 31)
    return date(fy_start + 1, 5, 31)


def tds_deposit_due_date(transaction_date):
    if transaction_date.month == 3:
        return date(transaction_date.year, 4, 30)
    if transaction_date.month == 12:
        return date(transaction_date.year + 1, 1, 7)
    return date(transaction_date.year, transaction_date.month + 1, 7)


def build_tds_deposit_watch(company, today=None, horizon_days=5, limit=50):
    today = today or timezone.localdate()
    upcoming_cutoff = today + timedelta(days=horizon_days)
    entries = (
        TDSEntry.objects.filter(company=company, is_deposited=False)
        .select_related("section", "deductee_ledger")
        .order_by("transaction_date", "id")
    )

    rows = []
    pending_amount = ZERO
    overdue_amount = ZERO
    due_today_amount = ZERO
    due_soon_amount = ZERO
    overdue_count = 0
    due_today_count = 0
    due_soon_count = 0

    for entry in entries:
        due_date = tds_deposit_due_date(entry.transaction_date)
        amount = entry.tds_amount or ZERO
        pending_amount += amount
        status = "open"
        if due_date < today:
            status = "overdue"
            overdue_count += 1
            overdue_amount += amount
        elif due_date == today:
            status = "due_today"
            due_today_count += 1
            due_today_amount += amount
        elif due_date <= upcoming_cutoff:
            status = "due_soon"
            due_soon_count += 1
            due_soon_amount += amount
        status_label = {
            "overdue": "Overdue",
            "due_today": "Due today",
            "due_soon": "Due soon",
        }.get(status, "Open")

        rows.append({
            "entry": entry,
            "due_date": due_date,
            "status": status,
            "status_label": status_label,
            "section": entry.section.section_code,
            "party": entry.deductee_ledger.name,
            "tds_amount": amount,
            "transaction_date": entry.transaction_date,
        })

    rows.sort(key=lambda row: (row["due_date"], row["entry"].pk))
    pending_count = len(rows)
    return {
        "rows": rows[:limit],
        "summary": {
            "pending_count": pending_count,
            "pending_amount": pending_amount,
            "overdue_count": overdue_count,
            "overdue_amount": overdue_amount,
            "due_today_count": due_today_count,
            "due_today_amount": due_today_amount,
            "due_soon_count": due_soon_count,
            "due_soon_amount": due_soon_amount,
            "attention_count": overdue_count + due_today_count + due_soon_count,
            "attention_amount": overdue_amount + due_today_amount + due_soon_amount,
            "next_due_date": rows[0]["due_date"] if rows else None,
        },
    }


def fy_options(anchor_fy=None, years_back=4, years_forward=1):
    anchor_fy = anchor_fy or current_financial_year_start()
    return [
        {"value": year, "label": financial_year_label(year)}
        for year in range(anchor_fy - years_back, anchor_fy + years_forward + 1)
    ]


def build_tds_return_workbench(company, fy_start, quarter, form_type, workpaper_override=None):
    period_start, period_end = quarter_dates(fy_start, quarter)
    due_date = return_due_date(fy_start, quarter)
    workpaper = workpaper_override or TDSReturnWorkpaper.objects.filter(
        company=company,
        form_type=form_type,
        financial_year_start=fy_start,
        quarter=quarter,
    ).first()

    entries_qs = TDSEntry.objects.filter(
        company=company,
        section__nature="TDS",
        transaction_date__range=(period_start, period_end),
    ).select_related("section", "deductee_ledger", "voucher").order_by("transaction_date", "id")
    entries = list(_filter_entries_for_form(entries_qs, form_type))

    rows = [_entry_row(entry) for entry in entries]
    challans = _challan_rows(rows)
    sections = _section_rows(rows)
    summary = _summary(company, rows, challans, period_start, period_end, due_date, fy_start, quarter, form_type)
    validations = _validations(company, workpaper, rows, challans, summary, form_type)
    summary["critical_issue_count"] = sum(1 for item in validations if item["severity"] == "critical")
    summary["warning_issue_count"] = sum(1 for item in validations if item["severity"] == "warning")
    summary["ok_issue_count"] = sum(1 for item in validations if item["severity"] == "ok")
    summary["readiness_score"] = max(
        0,
        100 - (summary["critical_issue_count"] * 25) - (summary["warning_issue_count"] * 10),
    )
    summary["can_mark_ready"] = summary["critical_issue_count"] == 0

    return {
        "workpaper": workpaper,
        "rows": rows,
        "challans": challans,
        "sections": sections,
        "summary": summary,
        "validations": validations,
        "summary_snapshot": _json_summary(summary),
        "validation_snapshot": {"items": validations},
    }


def _filter_entries_for_form(entries_qs, form_type):
    if form_type == TDSReturnWorkpaper.FORM_24Q:
        return [entry for entry in entries_qs if _is_salary_section(entry.section.section_code)]
    if form_type == TDSReturnWorkpaper.FORM_26Q:
        return [entry for entry in entries_qs if not _is_salary_section(entry.section.section_code)]
    return []


def _is_salary_section(section_code):
    code = (section_code or "").strip().upper().replace(" ", "")
    return code.startswith(SALARY_SECTION_PREFIXES)


def _entry_row(entry):
    pan = (entry.pan_number or "").strip().upper()
    bsr = (entry.bsr_code or "").strip()
    challan_number = (entry.challan_number or "").strip()
    issues = []
    if not PAN_RE.match(pan):
        issues.append("PAN")
    if not entry.is_deposited:
        issues.append("Deposit")
    elif not entry.deposit_date or not challan_number or not bsr:
        issues.append("Challan")
    if bsr and not BSR_RE.match(bsr):
        issues.append("BSR")

    return {
        "entry": entry,
        "date": entry.transaction_date,
        "section": entry.section.section_code,
        "section_description": entry.section.description,
        "party": entry.deductee_ledger.name,
        "pan": pan,
        "base_amount": entry.deductible_amount or ZERO,
        "rate": entry.rate_applied or ZERO,
        "tds_amount": entry.tds_amount or ZERO,
        "is_deposited": entry.is_deposited,
        "deposit_date": entry.deposit_date,
        "challan_number": challan_number,
        "bsr_code": bsr,
        "issues": issues,
    }


def _challan_rows(rows):
    grouped = {}
    for row in rows:
        if not row["is_deposited"]:
            continue
        key = (
            row["bsr_code"] or "Missing BSR",
            row["challan_number"] or "Missing Challan",
            row["deposit_date"],
        )
        if key not in grouped:
            grouped[key] = {
                "bsr_code": key[0],
                "challan_number": key[1],
                "deposit_date": key[2],
                "entry_count": 0,
                "tds_amount": ZERO,
                "has_issue": False,
            }
        grouped[key]["entry_count"] += 1
        grouped[key]["tds_amount"] += row["tds_amount"]
        if "Challan" in row["issues"] or "BSR" in row["issues"]:
            grouped[key]["has_issue"] = True
    return sorted(grouped.values(), key=lambda item: (item["deposit_date"] or date.min, item["bsr_code"], item["challan_number"]))


def _section_rows(rows):
    grouped = defaultdict(lambda: {"entry_count": 0, "base_amount": ZERO, "tds_amount": ZERO})
    descriptions = {}
    for row in rows:
        key = row["section"]
        grouped[key]["entry_count"] += 1
        grouped[key]["base_amount"] += row["base_amount"]
        grouped[key]["tds_amount"] += row["tds_amount"]
        descriptions[key] = row["section_description"]
    return [
        {
            "section": section,
            "description": descriptions.get(section, ""),
            "entry_count": values["entry_count"],
            "base_amount": values["base_amount"],
            "tds_amount": values["tds_amount"],
        }
        for section, values in sorted(grouped.items())
    ]


def _summary(company, rows, challans, period_start, period_end, due_date, fy_start, quarter, form_type):
    total_tds = sum((row["tds_amount"] for row in rows), ZERO)
    deposited_tds = sum((row["tds_amount"] for row in rows if row["is_deposited"]), ZERO)
    pending_tds = total_tds - deposited_tds
    invalid_pan_count = sum(1 for row in rows if "PAN" in row["issues"])
    pending_deposit_count = sum(1 for row in rows if "Deposit" in row["issues"])
    missing_challan_count = sum(1 for row in rows if "Challan" in row["issues"])
    invalid_bsr_count = sum(1 for row in rows if "BSR" in row["issues"])
    tan = (company.tan or "").strip().upper()
    return {
        "company_tan": tan,
        "tan_valid": bool(TAN_RE.match(tan)),
        "form_type": form_type,
        "fy_start": fy_start,
        "fy_label": financial_year_label(fy_start),
        "quarter": quarter,
        "period_start": period_start,
        "period_end": period_end,
        "due_date": due_date,
        "entry_count": len(rows),
        "challan_count": len(challans),
        "total_tds": total_tds,
        "deposited_tds": deposited_tds,
        "pending_tds": pending_tds,
        "invalid_pan_count": invalid_pan_count,
        "pending_deposit_count": pending_deposit_count,
        "missing_challan_count": missing_challan_count,
        "invalid_bsr_count": invalid_bsr_count,
    }


def _validations(company, workpaper, rows, challans, summary, form_type):
    validations = []

    def add(severity, code, title, count, description, action_url=""):
        validations.append({
            "severity": severity,
            "code": code,
            "title": title,
            "count": count,
            "description": description,
            "action_url": action_url,
        })

    add(
        "ok" if summary["tan_valid"] else "critical",
        "deductor_tan",
        "Deductor TAN",
        0 if summary["tan_valid"] else 1,
        "Company TAN is configured." if summary["tan_valid"] else "Company TAN is missing or invalid.",
        reverse("core:company_settings"),
    )
    add(
        "ok" if rows else "warning",
        "entry_volume",
        "Quarter Entries",
        len(rows),
        f"{len(rows)} TDS entries found for the selected return." if rows else "No TDS entries found for this return period.",
        reverse("tds:entry_list"),
    )
    add(
        "ok" if summary["invalid_pan_count"] == 0 else "critical",
        "deductee_pan",
        "Deductee PAN",
        summary["invalid_pan_count"],
        "All deductee PAN values look valid." if summary["invalid_pan_count"] == 0 else "Some deductee PAN values are missing or invalid.",
        reverse("tds:entry_list"),
    )
    add(
        "ok" if summary["pending_deposit_count"] == 0 else "critical",
        "tds_deposit",
        "TDS Deposit",
        summary["pending_deposit_count"],
        "All selected entries are marked deposited." if summary["pending_deposit_count"] == 0 else "Some selected entries are still pending deposit.",
        reverse("tds:entry_list") + "?deposited=0",
    )
    add(
        "ok" if summary["missing_challan_count"] == 0 else "critical",
        "challan_details",
        "Challan Details",
        summary["missing_challan_count"],
        "Deposited entries have challan number, BSR and deposit date." if summary["missing_challan_count"] == 0 else "Some deposited entries are missing challan number, BSR or deposit date.",
        reverse("tds:entry_list"),
    )
    add(
        "ok" if summary["invalid_bsr_count"] == 0 else "critical",
        "bsr_code",
        "BSR Code",
        summary["invalid_bsr_count"],
        "BSR codes are seven digits where provided." if summary["invalid_bsr_count"] == 0 else "Some BSR codes are not seven digits.",
        reverse("tds:entry_list"),
    )

    fvu_status = workpaper.fvu_status if workpaper else TDSReturnWorkpaper.FVU_NOT_RUN
    if fvu_status == TDSReturnWorkpaper.FVU_VALIDATED:
        add("ok", "fvu_status", "FVU Validation", 0, "FVU status is marked validated.")
    elif fvu_status == TDSReturnWorkpaper.FVU_FAILED:
        add("critical", "fvu_status", "FVU Validation", 1, "FVU status is failed. Correct errors before review.")
    else:
        add("warning", "fvu_status", "FVU Validation", 1, "FVU validation is not marked clean yet.")

    challan_status = workpaper.challan_status if workpaper else TDSReturnWorkpaper.CHALLAN_NOT_CHECKED
    if challan_status == TDSReturnWorkpaper.CHALLAN_MATCHED:
        add("ok", "oltas_challan", "OLTAS Challan Match", 0, "Challan status is marked matched.")
    elif challan_status in {TDSReturnWorkpaper.CHALLAN_UNMATCHED, TDSReturnWorkpaper.CHALLAN_OVERBOOKED}:
        add("critical", "oltas_challan", "OLTAS Challan Match", 1, "Challan status is unresolved.")
    else:
        add("warning", "oltas_challan", "OLTAS Challan Match", 1, "Challan status has not been checked.")

    traces_status = workpaper.traces_statement_status if workpaper else TDSReturnWorkpaper.TRACES_NOT_CHECKED
    if traces_status == TDSReturnWorkpaper.TRACES_ACCEPTED:
        add("ok", "traces_status", "TRACES Statement Status", 0, "TRACES status is accepted or processed.")
    elif traces_status == TDSReturnWorkpaper.TRACES_REJECTED:
        add("critical", "traces_status", "TRACES Statement Status", 1, "TRACES status is rejected.")
    elif traces_status == TDSReturnWorkpaper.TRACES_PROCESSED_DEFAULT:
        add("warning", "traces_status", "TRACES Statement Status", 1, "TRACES status shows processed with default.")
    else:
        add("warning", "traces_status", "TRACES Statement Status", 1, "TRACES status has not been checked.")

    if form_type == TDSReturnWorkpaper.FORM_27Q:
        add(
            "warning",
            "non_resident_classification",
            "27Q Classification",
            1,
            "Non-resident deductee classification is not modelled yet, so 27Q rows are held out.",
        )

    return validations


def _json_summary(summary):
    return {
        key: _json_value(value)
        for key, value in summary.items()
        if key not in {"can_mark_ready"}
    }


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))
    if isinstance(value, date):
        return value.isoformat()
    return value
