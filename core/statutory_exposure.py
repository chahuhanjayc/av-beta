import calendar
import csv
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import Sum
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from reports.utils import get_msme_payable_watch
from tds.models import TDSEntry, TDSReturnWorkpaper
from tds.workbench import quarter_dates
from vouchers.models import Voucher

from .models import Company, ComplianceFiling, ComplianceNotice, PracticeTask, UserCompanyAccess
from .statutory_rules import (
    resolve_gstr1_rule,
    resolve_gstr3b_rule,
    resolve_msme_rule,
    resolve_tds_deposit_rule,
    resolve_tds_return_rule,
)


ZERO = Decimal("0.00")
GST_LATE_FEE_PER_DAY = Decimal("50.00")
GST_NIL_LATE_FEE_PER_DAY = Decimal("20.00")
GST_INTEREST_RATE = Decimal("0.18")
TDS_DEPOSIT_INTEREST_RATE_PER_MONTH = Decimal("0.015")
TDS_RETURN_LATE_FEE_PER_DAY = Decimal("200.00")

SEVERITY_RANK = {"critical": 0, "high": 1, "warning": 2, "info": 3}


def _accessible_companies(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return Company.objects.filter(user_access__user=user).distinct().order_by("name")


def _can_create_tasks(user, company):
    if user.is_superuser:
        return True
    return UserCompanyAccess.objects.filter(
        user=user,
        company=company,
        role__in=["Admin", "Accountant"],
    ).exists()


def _add_months(value, months):
    month_index = (value.month - 1) + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _parse_period(raw_period=None, today=None):
    today = today or timezone.localdate()
    if raw_period:
        try:
            year_text, month_text = raw_period.split("-", 1)
            start = date(int(year_text), int(month_text), 1)
        except (TypeError, ValueError):
            start = _add_months(today.replace(day=1), -1)
    else:
        start = _add_months(today.replace(day=1), -1)
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    return start, end


def _period_due(period_start, day, month_offset=1):
    due_month = _add_months(period_start, month_offset)
    return due_month.replace(day=min(day, calendar.monthrange(due_month.year, due_month.month)[1]))


def _money(value):
    return Decimal(value or ZERO).quantize(Decimal("0.01"))


def _days_overdue(due_date, today):
    if not due_date:
        return 0
    return max((today - due_date).days, 0)


def _days_to_due(due_date, today):
    if not due_date:
        return None
    return (due_date - today).days


def _months_or_part(start_date, end_date):
    if end_date <= start_date:
        return 1
    months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month)
    if end_date.day > start_date.day or months == 0:
        months += 1
    return max(months, 1)


def _status_and_severity(due_date, today, horizon_days):
    days_to_due = _days_to_due(due_date, today)
    if days_to_due is None:
        return "No due date", "info"
    if days_to_due < 0:
        return f"{abs(days_to_due)} day(s) overdue", "critical"
    if days_to_due == 0:
        return "Due today", "high"
    if days_to_due <= horizon_days:
        return f"Due in {days_to_due} day(s)", "warning"
    return "Upcoming", "info"


def _switch_url(company, next_path):
    return f"{reverse('core:switch_company', args=[company.pk])}?{urlencode({'next': next_path})}"


def _item(*, key, company, category, title, status, severity, due_date, exposure, daily_burn, base_amount, description, action_label, action_url, reference="", rule_source=""):
    return {
        "key": key,
        "company": company,
        "category": category,
        "title": title,
        "status": status,
        "severity": severity,
        "severity_rank": SEVERITY_RANK.get(severity, 4),
        "due_date": due_date,
        "days_to_due": _days_to_due(due_date, timezone.localdate()) if due_date else None,
        "exposure": _money(exposure),
        "daily_burn": _money(daily_burn),
        "base_amount": _money(base_amount),
        "description": description,
        "action_label": action_label,
        "action_url": action_url,
        "reference": reference,
        "rule_source": rule_source,
    }


def _open_filing(company, filing_type, period_start, period_end):
    return (
        ComplianceFiling.objects.filter(
            company=company,
            filing_type=filing_type,
            period_start=period_start,
            period_end=period_end,
        )
        .exclude(status__in=[ComplianceFiling.STATUS_FILED, ComplianceFiling.STATUS_CANCELLED])
        .first()
    )


def _sales_tax_proxy(company, period_start, period_end):
    return _money(
        Voucher.objects.filter(
            company=company,
            voucher_type="Sales",
            status="APPROVED",
            date__range=(period_start, period_end),
        ).aggregate(total=Sum("total_tax"))["total"]
    )


def _add_gst_rows(items, company, period_start, period_end, today, horizon_days):
    filings_url = _switch_url(company, reverse("core:compliance_filings"))

    gstr1_rule = resolve_gstr1_rule(company, period_start)
    if gstr1_rule.get("enabled"):
        gstr1_due = gstr1_rule["due_date"]
        gstr1_start = gstr1_rule["period_start"]
        gstr1_end = gstr1_rule["period_end"]
        gstr1_filing = _open_filing(company, ComplianceFiling.TYPE_GSTR1, gstr1_start, gstr1_end)
    else:
        gstr1_due = None
        gstr1_filing = None
    if gstr1_due and (gstr1_filing or gstr1_due <= today + timedelta(days=horizon_days)):
        days = _days_overdue(gstr1_due, today)
        status, severity = _status_and_severity(gstr1_due, today, horizon_days)
        daily_fee = gstr1_rule["late_fee_per_day"]
        items.append(_item(
            key=f"gst-gstr1-{company.pk}-{gstr1_start:%Y%m%d}-{gstr1_end:%Y%m%d}",
            company=company,
            category="GST",
            title=f"GSTR-1 {gstr1_rule['period_label']}",
            status=status,
            severity=severity,
            due_date=gstr1_due,
            exposure=daily_fee * days,
            daily_burn=daily_fee,
            base_amount=ZERO,
            description="Estimated late fee uses this client's statutory profile or override; verify nil/turnover-specific caps before filing.",
            action_label="Open Filings",
            action_url=filings_url,
            reference=gstr1_filing.source_reference if gstr1_filing else "workflow-missing",
            rule_source=gstr1_rule["source_reference"],
        ))

    gstr3b_rule = resolve_gstr3b_rule(company, period_start)
    if gstr3b_rule.get("enabled"):
        gstr3b_due = gstr3b_rule["due_date"]
        gstr3b_start = gstr3b_rule["period_start"]
        gstr3b_end = gstr3b_rule["period_end"]
        gstr3b_filing = _open_filing(company, ComplianceFiling.TYPE_GSTR3B, gstr3b_start, gstr3b_end)
        tax_proxy = _sales_tax_proxy(company, gstr3b_start, gstr3b_end)
    else:
        gstr3b_due = None
        gstr3b_filing = None
        tax_proxy = ZERO
    if gstr3b_due and (gstr3b_filing or gstr3b_due <= today + timedelta(days=horizon_days)):
        days = _days_overdue(gstr3b_due, today)
        status, severity = _status_and_severity(gstr3b_due, today, horizon_days)
        late_fee = gstr3b_rule["late_fee_per_day"] * days
        interest = (tax_proxy * gstr3b_rule["interest_rate"] * Decimal(days)) / Decimal("365") if days else ZERO
        daily_interest = (tax_proxy * gstr3b_rule["interest_rate"]) / Decimal("365") if tax_proxy else ZERO
        items.append(_item(
            key=f"gst-gstr3b-{company.pk}-{gstr3b_start:%Y%m%d}-{gstr3b_end:%Y%m%d}",
            company=company,
            category="GST",
            title=f"GSTR-3B {gstr3b_rule['period_label']}",
            status=status,
            severity=severity,
            due_date=gstr3b_due,
            exposure=late_fee + interest,
            daily_burn=gstr3b_rule["late_fee_per_day"] + daily_interest,
            base_amount=tax_proxy,
            description="Interest estimate uses approved sales tax as a cash-liability proxy and this client's statutory profile; actual 3B interest depends on cash ledger discharge and portal computation.",
            action_label="Open GST Workbench",
            action_url=_switch_url(company, reverse("core:gst_workbench_detail", args=[company.pk, gstr3b_start.strftime("%Y-%m")])),
            reference=gstr3b_filing.source_reference if gstr3b_filing else "workflow-missing",
            rule_source=gstr3b_rule["source_reference"],
        ))


def _add_tds_deposit_row(items, company, today, horizon_days):
    entries = (
        TDSEntry.objects.filter(company=company, is_deposited=False)
        .select_related("section", "deductee_ledger")
        .order_by("transaction_date", "id")
    )
    total = overdue_amount = due_soon_amount = exposure = daily_burn = ZERO
    count = overdue_count = due_soon_count = 0
    earliest_due = None
    rule_sources = set()
    for entry in entries:
        rule = resolve_tds_deposit_rule(company, entry.transaction_date)
        if not rule.get("enabled"):
            continue
        due_date = rule["due_date"]
        rule_sources.add(rule.get("source_reference", "profile"))
        amount = entry.tds_amount or ZERO
        total += amount
        count += 1
        if earliest_due is None or due_date < earliest_due:
            earliest_due = due_date
        if due_date < today:
            overdue_count += 1
            overdue_amount += amount
            exposure += amount * rule["interest_rate_per_month"] * Decimal(_months_or_part(entry.transaction_date, today))
            daily_burn += (amount * rule["interest_rate_per_month"]) / Decimal("30")
        elif due_date <= today + timedelta(days=horizon_days):
            due_soon_count += 1
            due_soon_amount += amount
    if not count or (earliest_due and earliest_due > today + timedelta(days=horizon_days)):
        return
    status, severity = _status_and_severity(earliest_due, today, horizon_days)
    items.append(_item(
        key=f"tds-deposit-{company.pk}",
        company=company,
        category="TDS",
        title="TDS deposit exposure",
        status=status,
        severity=severity,
        due_date=earliest_due,
        exposure=exposure,
        daily_burn=daily_burn,
        base_amount=total,
        description=f"{overdue_count} overdue and {due_soon_count} due-soon TDS item(s). Interest estimate uses the configured monthly rate for deducted but unpaid tax.",
        action_label="Open TDS Register",
        action_url=_switch_url(company, f"{reverse('tds:entry_list')}?deposited=0"),
        reference=f"{count} unpaid entries",
        rule_source=", ".join(sorted(rule_sources)) if rule_sources else "profile",
    ))


def _fy_quarter_for_period(period_start):
    if period_start.month <= 3:
        fy_start = period_start.year - 1
    else:
        fy_start = period_start.year
    if period_start.month in {4, 5, 6}:
        quarter = TDSReturnWorkpaper.Q1
    elif period_start.month in {7, 8, 9}:
        quarter = TDSReturnWorkpaper.Q2
    elif period_start.month in {10, 11, 12}:
        quarter = TDSReturnWorkpaper.Q3
    else:
        quarter = TDSReturnWorkpaper.Q4
    return fy_start, quarter


def _add_tds_return_row(items, company, period_start, today, horizon_days):
    fy_start, quarter = _fy_quarter_for_period(period_start)
    q_start, q_end = quarter_dates(fy_start, quarter)
    rule = resolve_tds_return_rule(company, fy_start, quarter, TDSReturnWorkpaper.FORM_26Q)
    if not rule.get("enabled"):
        return
    due_date = rule["due_date"]
    if due_date > today + timedelta(days=horizon_days):
        return
    entries = TDSEntry.objects.filter(company=company, transaction_date__range=(q_start, q_end))
    tds_total = _money(entries.aggregate(total=Sum("tds_amount"))["total"])
    if not tds_total:
        return
    workpaper = (
        TDSReturnWorkpaper.objects.filter(
            company=company,
            financial_year_start=fy_start,
            quarter=quarter,
            form_type=TDSReturnWorkpaper.FORM_26Q,
        )
        .exclude(status=TDSReturnWorkpaper.STATUS_FILED)
        .first()
    )
    filed = TDSReturnWorkpaper.objects.filter(
        company=company,
        financial_year_start=fy_start,
        quarter=quarter,
        form_type=TDSReturnWorkpaper.FORM_26Q,
        status=TDSReturnWorkpaper.STATUS_FILED,
    ).exists()
    if filed:
        return
    days = _days_overdue(due_date, today)
    status, severity = _status_and_severity(due_date, today, horizon_days)
    exposure = min(rule["late_fee_per_day"] * days, tds_total) if days else ZERO
    items.append(_item(
        key=f"tds-return-{company.pk}-{fy_start}-{quarter}",
        company=company,
        category="TDS",
        title=f"TDS return {quarter} FY {fy_start}-{str(fy_start + 1)[-2:]}",
        status=status,
        severity=severity,
        due_date=due_date,
        exposure=exposure,
        daily_burn=rule["late_fee_per_day"] if exposure < tds_total else ZERO,
        base_amount=tds_total,
        description="Estimated section 234E fee uses this client's statutory profile or override and is capped at tax deductible/collectible for the statement.",
        action_label="Open TDS Workbench",
        action_url=_switch_url(company, f"{reverse('tds:return_workbench')}?{urlencode({'fy': fy_start, 'quarter': quarter, 'form_type': TDSReturnWorkpaper.FORM_26Q})}"),
        reference=f"Workpaper #{workpaper.pk}" if workpaper else "workpaper-missing",
        rule_source=rule["source_reference"],
    ))


def _add_msme_row(items, company, today, horizon_days):
    rule = resolve_msme_rule(company)
    if not rule.get("enabled"):
        return
    watch = get_msme_payable_watch(company, as_of_date=today)
    summary = watch["summary"]
    if not summary["overdue_count"] and not summary["due_soon_count"]:
        return
    due_dates = [row["due_date"] for row in watch["rows"] if row["status"] in {"overdue", "due_soon"}]
    due_date = min(due_dates) if due_dates else None
    status, severity = _status_and_severity(due_date, today, horizon_days)
    amount = summary["overdue_amount"] + summary["due_soon_amount"]
    daily_burn = (summary["overdue_amount"] * rule["interest_rate"]) / Decimal("365") if summary["overdue_amount"] else ZERO
    items.append(_item(
        key=f"msme-{company.pk}",
        company=company,
        category="MSME",
        title="MSME payment exposure",
        status=status,
        severity=severity,
        due_date=due_date,
        exposure=summary["interest_liability"],
        daily_burn=daily_burn,
        base_amount=amount,
        description=f"{summary['overdue_count']} overdue and {summary['due_soon_count']} due-soon MSME payable(s). Interest estimate uses the configured MSME profile rate.",
        action_label="Open MSME Report",
        action_url=_switch_url(company, reverse("reports:msme_overdue")),
        reference=f"{summary['total_count']} watched bills",
        rule_source="profile",
    ))


def _add_notice_rows(items, company, today, horizon_days):
    notices = (
        ComplianceNotice.objects.filter(company=company)
        .exclude(status=ComplianceNotice.STATUS_CLOSED)
        .filter(response_due_date__lte=today + timedelta(days=horizon_days))
        .order_by("response_due_date", "id")[:25]
    )
    for notice in notices:
        status, severity = _status_and_severity(notice.response_due_date, today, horizon_days)
        items.append(_item(
            key=f"notice-{notice.pk}",
            company=company,
            category="Notice",
            title=notice.title,
            status=status,
            severity=severity,
            due_date=notice.response_due_date,
            exposure=ZERO,
            daily_burn=ZERO,
            base_amount=ZERO,
            description="Penalty amount is notice-specific; this row tracks response-deadline exposure.",
            action_label="Open Notices",
            action_url=_switch_url(company, f"{reverse('core:compliance_notices')}?{urlencode({'company': company.pk})}"),
            reference=notice.reference_number or f"NOTICE-{notice.pk}",
        ))


def build_statutory_exposure(user, params=None):
    params = params or {}
    today = timezone.localdate()
    try:
        horizon_days = int(params.get("horizon") or 10)
    except (TypeError, ValueError):
        horizon_days = 10
    horizon_days = min(max(horizon_days, 0), 90)
    period_start, period_end = _parse_period(params.get("period"), today)
    companies = list(_accessible_companies(user))
    items = []
    for company in companies:
        _add_gst_rows(items, company, period_start, period_end, today, horizon_days)
        _add_tds_deposit_row(items, company, today, horizon_days)
        _add_tds_return_row(items, company, period_start, today, horizon_days)
        _add_msme_row(items, company, today, horizon_days)
        _add_notice_rows(items, company, today, horizon_days)

    company_filter = (params.get("company") or "all").strip() or "all"
    category_filter = (params.get("category") or "all").strip() or "all"
    severity_filter = (params.get("severity") or "all").strip() or "all"
    q = (params.get("q") or "").strip().lower()
    visible = items
    if company_filter != "all" and company_filter.isdigit():
        visible = [item for item in visible if item["company"].pk == int(company_filter)]
    if category_filter != "all":
        visible = [item for item in visible if item["category"] == category_filter]
    if severity_filter != "all":
        visible = [item for item in visible if item["severity"] == severity_filter]
    if q:
        visible = [
            item for item in visible
            if q in " ".join([
                item["company"].name,
                item["category"],
                item["title"],
                item["description"],
                item["reference"],
            ]).lower()
        ]

    visible.sort(key=lambda item: (
        item["severity_rank"],
        item["due_date"] or date.max,
        -item["exposure"],
        item["company"].name.lower(),
    ))

    categories = sorted({item["category"] for item in items})
    totals = {
        "items": len(items),
        "visible": len(visible),
        "critical": sum(1 for item in visible if item["severity"] == "critical"),
        "high": sum(1 for item in visible if item["severity"] == "high"),
        "warning": sum(1 for item in visible if item["severity"] == "warning"),
        "exposure": sum((item["exposure"] for item in items), ZERO),
        "visible_exposure": sum((item["exposure"] for item in visible), ZERO),
        "daily_burn": sum((item["daily_burn"] for item in visible), ZERO),
        "gst_exposure": sum((item["exposure"] for item in visible if item["category"] == "GST"), ZERO),
        "tds_exposure": sum((item["exposure"] for item in visible if item["category"] == "TDS"), ZERO),
        "msme_exposure": sum((item["exposure"] for item in visible if item["category"] == "MSME"), ZERO),
    }
    query = {
        "period": period_start.strftime("%Y-%m"),
        "horizon": horizon_days,
        "company": company_filter,
        "category": category_filter,
        "severity": severity_filter,
        "q": params.get("q", ""),
    }
    return {
        "items": visible,
        "all_items": items,
        "companies": companies,
        "categories": categories,
        "totals": totals,
        "period_start": period_start,
        "period_end": period_end,
        "period_value": period_start.strftime("%Y-%m"),
        "horizon_days": horizon_days,
        "today": today,
        "company_filter": company_filter,
        "category_filter": category_filter,
        "severity_filter": severity_filter,
        "q": params.get("q", ""),
        "export_query": urlencode({**query, "export": "csv"}),
        "task_query": urlencode(query),
        "title": "Statutory Exposure",
        "sources": [
            "Client statutory profile: GST/TDS frequency, due days, fee and interest assumptions.",
            "Client rule overrides: notification or engagement-specific due-date/rate exceptions.",
            "GST, Income Tax/TRACES, and MSME defaults are starting points; verify portal computation before filing.",
        ],
    }


def statutory_exposure_csv_response(items, period_value):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="statutory-exposure-{period_value}.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "Category",
        "Severity",
        "Status",
        "Due Date",
        "Title",
        "Exposure",
        "Daily Burn",
        "Base Amount",
        "Reference",
        "Rule Source",
        "Description",
        "Action",
    ])
    for item in items:
        writer.writerow([
            item["company"].name,
            item["category"],
            item["severity"],
            item["status"],
            item["due_date"].isoformat() if item["due_date"] else "",
            item["title"],
            item["exposure"],
            item["daily_burn"],
            item["base_amount"],
            item["reference"],
            item["rule_source"],
            item["description"],
            item["action_label"],
        ])
    return response


def create_statutory_exposure_tasks(items, user):
    created = 0
    existing = 0
    skipped = 0
    today = timezone.localdate()
    for item in items:
        if item["severity"] == "info":
            continue
        if not _can_create_tasks(user, item["company"]):
            skipped += 1
            continue
        reference = f"STATEX:{item['key']}"
        task, was_created = PracticeTask.objects.get_or_create(
            company=item["company"],
            reference=reference,
            defaults={
                "title": f"{item['category']} recovery: {item['title']}",
                "task_type": _task_type_for_category(item["category"]),
                "priority": _priority_for_severity(item["severity"]),
                "status": PracticeTask.STATUS_OPEN,
                "due_date": item["due_date"] or today,
                "created_by": user,
                "description": (
                    f"{item['description']}\n"
                    f"Estimated exposure: Rs.{item['exposure']}\n"
                    f"Daily burn: Rs.{item['daily_burn']}\n"
                    f"Reference: {item['reference']}"
                ),
            },
        )
        if was_created:
            created += 1
        else:
            existing += 1
    return {"created": created, "existing": existing, "skipped": skipped}


def _task_type_for_category(category):
    if category == "GST":
        return PracticeTask.TYPE_GST
    if category == "TDS":
        return PracticeTask.TYPE_TDS
    if category == "Notice":
        return PracticeTask.TYPE_NOTICE
    return PracticeTask.TYPE_OTHER


def _priority_for_severity(severity):
    if severity == "critical":
        return PracticeTask.PRIORITY_CRITICAL
    if severity == "high":
        return PracticeTask.PRIORITY_HIGH
    if severity == "warning":
        return PracticeTask.PRIORITY_NORMAL
    return PracticeTask.PRIORITY_LOW
