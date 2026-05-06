import calendar
from datetime import date

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from .compliance_workflow import sync_task_for_filing
from .models import ComplianceFiling, PracticeTask


QUARTER_END_MONTHS = {3, 6, 9, 12}


def month_start(value):
    return value.replace(day=1)


def add_months(value, months):
    month_index = (value.month - 1) + months
    year = value.year + (month_index // 12)
    month = (month_index % 12) + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_end(value):
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def due_date(period_start, day, month_offset):
    due_month = add_months(period_start, month_offset)
    last_day = calendar.monthrange(due_month.year, due_month.month)[1]
    return due_month.replace(day=min(day, last_day))


def parse_month_start(raw_value):
    if not raw_value:
        return month_start(timezone.localdate())
    if isinstance(raw_value, date):
        return month_start(raw_value)
    parsed = parse_date(str(raw_value))
    if not parsed:
        raise ValueError("Date must be YYYY-MM-DD.")
    return month_start(parsed)


def parse_optional_date(raw_value):
    if not raw_value:
        return None
    if isinstance(raw_value, date):
        return raw_value
    parsed = parse_date(str(raw_value))
    if not parsed:
        raise ValueError("Date must be YYYY-MM-DD.")
    return parsed


def fiscal_year_label(period_end):
    fy_start = period_end.year - 1 if period_end.month <= 3 else period_end.year
    return f"FY {fy_start}-{str(fy_start + 1)[-2:]}"


def quarter_period_for_month(period_start):
    if period_start.month not in QUARTER_END_MONTHS:
        return None

    if period_start.month == 3:
        quarter_start = date(period_start.year, 1, 1)
        quarter_label = "Q4"
        due = due_date(period_start, 31, 2)
    elif period_start.month == 6:
        quarter_start = date(period_start.year, 4, 1)
        quarter_label = "Q1"
        due = due_date(period_start, 31, 1)
    elif period_start.month == 9:
        quarter_start = date(period_start.year, 7, 1)
        quarter_label = "Q2"
        due = due_date(period_start, 31, 1)
    else:
        quarter_start = date(period_start.year, 10, 1)
        quarter_label = "Q3"
        due = due_date(period_start, 31, 1)

    quarter_end = month_end(period_start)
    return quarter_label, quarter_start, quarter_end, due


def monthly_templates(
    period_start,
    *,
    include_ims=True,
    include_gstr1=True,
    include_gstr3b=True,
    include_tds_payment=True,
    include_tds_returns=True,
    due_month_offset=1,
    ims_review_day=10,
    gstr1_day=11,
    gstr3b_day=20,
    tds_payment_day=7,
):
    templates = []
    if include_ims:
        templates.append({
            "filing_type": ComplianceFiling.TYPE_GST_IMS,
            "label": "GST IMS Review",
            "title": f"GST IMS Review - {period_start:%b %Y}",
            "period_start": period_start,
            "period_end": month_end(period_start),
            "due_date": due_date(period_start, ims_review_day, due_month_offset),
        })
    if include_gstr1:
        templates.append({
            "filing_type": ComplianceFiling.TYPE_GSTR1,
            "label": "GSTR-1",
            "title": f"GSTR-1 - {period_start:%b %Y}",
            "period_start": period_start,
            "period_end": month_end(period_start),
            "due_date": due_date(period_start, gstr1_day, due_month_offset),
        })
    if include_gstr3b:
        templates.append({
            "filing_type": ComplianceFiling.TYPE_GSTR3B,
            "label": "GSTR-3B",
            "title": f"GSTR-3B - {period_start:%b %Y}",
            "period_start": period_start,
            "period_end": month_end(period_start),
            "due_date": due_date(period_start, gstr3b_day, due_month_offset),
        })
    if include_tds_payment:
        templates.append({
            "filing_type": ComplianceFiling.TYPE_TDS_PAYMENT,
            "label": "TDS Payment",
            "title": f"TDS Payment - {period_start:%b %Y}",
            "period_start": period_start,
            "period_end": month_end(period_start),
            "due_date": due_date(period_start, tds_payment_day, due_month_offset),
        })

    quarter = quarter_period_for_month(period_start)
    if include_tds_returns and quarter:
        quarter_label, quarter_start, quarter_end, tds_return_due = quarter
        fy_label = fiscal_year_label(quarter_end)
        templates.extend([
            {
                "filing_type": ComplianceFiling.TYPE_TDS_24Q,
                "label": "TDS 24Q",
                "title": f"TDS 24Q - {quarter_label} {fy_label}",
                "period_start": quarter_start,
                "period_end": quarter_end,
                "due_date": tds_return_due,
            },
            {
                "filing_type": ComplianceFiling.TYPE_TDS_26Q,
                "label": "TDS 26Q",
                "title": f"TDS 26Q - {quarter_label} {fy_label}",
                "period_start": quarter_start,
                "period_end": quarter_end,
                "due_date": tds_return_due,
            },
        ])
    return templates


def annual_templates(
    *,
    gstr9_due=None,
    gstr9c_due=None,
    itr_due=None,
    tax_audit_due=None,
    mca_aoc4_due=None,
    mca_mgt7_due=None,
):
    mapping = [
        (gstr9_due, ComplianceFiling.TYPE_GSTR9, "GSTR-9"),
        (gstr9c_due, ComplianceFiling.TYPE_GSTR9C, "GSTR-9C"),
        (itr_due, ComplianceFiling.TYPE_ITR, "ITR"),
        (tax_audit_due, ComplianceFiling.TYPE_TAX_AUDIT, "Tax Audit"),
        (mca_aoc4_due, ComplianceFiling.TYPE_MCA_AOC4, "MCA AOC-4"),
        (mca_mgt7_due, ComplianceFiling.TYPE_MCA_MGT7, "MCA MGT-7"),
    ]
    templates = []
    for raw_due, filing_type, label in mapping:
        parsed_due = parse_optional_date(raw_due)
        if not parsed_due:
            continue
        period_end = date(parsed_due.year, 3, 31)
        if parsed_due.month <= 3:
            period_end = date(parsed_due.year - 1, 3, 31)
        period_start = date(period_end.year - 1, 4, 1)
        templates.append({
            "filing_type": filing_type,
            "label": label,
            "title": f"{label} - {fiscal_year_label(period_end)}",
            "period_start": period_start,
            "period_end": period_end,
            "due_date": parsed_due,
        })
    return templates


def generate_compliance_calendar(
    *,
    companies,
    months=3,
    from_date=None,
    assigned_to=None,
    reviewer=None,
    created_by=None,
    dry_run=False,
    include_ims=True,
    include_gstr1=True,
    include_gstr3b=True,
    include_tds_payment=True,
    include_tds_returns=True,
    due_month_offset=1,
    ims_review_day=10,
    gstr1_day=11,
    gstr3b_day=20,
    tds_payment_day=7,
    gstr9_due=None,
    gstr9c_due=None,
    itr_due=None,
    tax_audit_due=None,
    mca_aoc4_due=None,
    mca_mgt7_due=None,
):
    if months < 1:
        raise ValueError("months must be at least 1.")

    start = parse_month_start(from_date)
    company_list = list(companies)
    if not company_list:
        raise ValueError("At least one company is required.")

    monthly_options = {
        "include_ims": include_ims,
        "include_gstr1": include_gstr1,
        "include_gstr3b": include_gstr3b,
        "include_tds_payment": include_tds_payment,
        "include_tds_returns": include_tds_returns,
        "due_month_offset": due_month_offset,
        "ims_review_day": ims_review_day,
        "gstr1_day": gstr1_day,
        "gstr3b_day": gstr3b_day,
        "tds_payment_day": tds_payment_day,
    }
    annual_options = {
        "gstr9_due": gstr9_due,
        "gstr9c_due": gstr9c_due,
        "itr_due": itr_due,
        "tax_audit_due": tax_audit_due,
        "mca_aoc4_due": mca_aoc4_due,
        "mca_mgt7_due": mca_mgt7_due,
    }

    created = []
    existing = []
    with transaction.atomic():
        for company in company_list:
            for offset in range(months):
                period_start = add_months(start, offset)
                for template in monthly_templates(period_start, **monthly_options):
                    item = ensure_filing(
                        company=company,
                        assigned_to=assigned_to,
                        reviewer=reviewer,
                        created_by=created_by,
                        dry_run=dry_run,
                        **template,
                    )
                    (created if item["created"] else existing).append(item)

            for template in annual_templates(**annual_options):
                item = ensure_filing(
                    company=company,
                    assigned_to=assigned_to,
                    reviewer=reviewer,
                    created_by=created_by,
                    dry_run=dry_run,
                    **template,
                )
                (created if item["created"] else existing).append(item)

        if dry_run:
            transaction.set_rollback(True)

    items = created + existing
    return {
        "created": len(created),
        "existing": len(existing),
        "dry_run": dry_run,
        "companies": len(company_list),
        "items": items,
        "created_items": created,
        "existing_items": existing,
    }


def ensure_filing(
    *,
    company,
    filing_type,
    label,
    title,
    period_start,
    period_end,
    due_date,
    assigned_to=None,
    reviewer=None,
    created_by=None,
    dry_run=False,
    source_prefix="CAL",
    notes=None,
):
    existing = ComplianceFiling.objects.filter(
        company=company,
        filing_type=filing_type,
        period_start=period_start,
        period_end=period_end,
    ).order_by("pk").first()
    if existing:
        return _item_payload(existing, created=False, label=label)

    source_reference = f"{source_prefix}:{filing_type}:{period_start:%Y%m%d}:{period_end:%Y%m%d}"
    item = {
        "created": True,
        "company": company.name,
        "company_id": company.pk,
        "filing_type": filing_type,
        "label": label,
        "title": title,
        "period_start": period_start,
        "period_end": period_end,
        "due_date": due_date,
        "status": ComplianceFiling.STATUS_NOT_STARTED,
        "filing_id": None,
        "task_id": None,
    }
    if dry_run:
        return item

    filing = ComplianceFiling.objects.create(
        company=company,
        filing_type=filing_type,
        title=title,
        status=ComplianceFiling.STATUS_NOT_STARTED,
        priority=PracticeTask.PRIORITY_NORMAL,
        period_start=period_start,
        period_end=period_end,
        due_date=due_date,
        assigned_to=assigned_to,
        reviewer=reviewer,
        created_by=created_by,
        source=ComplianceFiling.SOURCE_CALENDAR,
        source_reference=source_reference,
        notes=notes or "Auto-generated from the compliance calendar. Verify statutory due date before filing.",
    )
    task = sync_task_for_filing(filing, user=created_by)
    item["filing_id"] = filing.pk
    item["task_id"] = task.pk
    return item


def _item_payload(filing, *, created, label):
    return {
        "created": created,
        "company": filing.company.name,
        "company_id": filing.company_id,
        "filing_type": filing.filing_type,
        "label": label,
        "title": filing.title,
        "period_start": filing.period_start,
        "period_end": filing.period_end,
        "due_date": filing.due_date,
        "status": filing.status,
        "filing_id": filing.pk,
        "task_id": filing.related_task_id,
    }
