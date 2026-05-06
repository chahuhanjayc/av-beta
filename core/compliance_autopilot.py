from django.db import transaction
from django.utils import timezone

from tds.models import TDSReturnWorkpaper
from tds.workbench import financial_year_label, quarter_dates, quarter_for_date

from .compliance_calendar import add_months, ensure_filing, month_end, parse_month_start
from .models import CompanyStatutoryProfile, ComplianceFiling
from .statutory_rules import (
    get_statutory_profile,
    period_due,
    resolve_gstr1_rule,
    resolve_gstr3b_rule,
    resolve_tds_deposit_rule,
    resolve_tds_return_rule,
)


QUARTER_END_MONTHS = {3, 6, 9, 12}
AUTOPILOT_DEFAULT_MONTHS = 3
AUTOPILOT_MAX_MONTHS = 12


def normalize_autopilot_months(raw_value, *, default=AUTOPILOT_DEFAULT_MONTHS, maximum=AUTOPILOT_MAX_MONTHS):
    try:
        months = int(raw_value)
    except (TypeError, ValueError):
        months = default
    return max(1, min(months, maximum))


def _saved_profile(company):
    try:
        return company.statutory_profile, True
    except CompanyStatutoryProfile.DoesNotExist:
        return CompanyStatutoryProfile(company=company), False


def autopilot_profile_warnings(company):
    profile, is_saved = _saved_profile(company)
    warnings = []
    if not is_saved:
        warnings.append("Statutory profile is using defaults. Save client-specific settings for safer due dates.")
    if profile.gst_registered and not (company.gstin or "").strip():
        warnings.append("GST is enabled but GSTIN is blank.")
    if profile.tds_applicable and not (company.tan or "").strip():
        warnings.append("TDS is enabled but TAN is blank.")
    if profile.tds_applicable and not any([
        profile.tds_24q_enabled,
        profile.tds_26q_enabled,
        profile.tds_27q_enabled,
    ]):
        warnings.append("TDS is enabled but no quarterly return form is selected.")
    return warnings


def _template(*, filing_type, label, title, period_start, period_end, due_date):
    return {
        "filing_type": filing_type,
        "label": label,
        "title": title,
        "period_start": period_start,
        "period_end": period_end,
        "due_date": due_date,
    }


def _gst_rule_template(rule, *, filing_type, label):
    if not rule.get("enabled"):
        return None
    return _template(
        filing_type=filing_type,
        label=label,
        title=f"{label} - {rule['period_label']}",
        period_start=rule["period_start"],
        period_end=rule["period_end"],
        due_date=rule["due_date"],
    )


def _tds_return_filing_type(form_type):
    if form_type == TDSReturnWorkpaper.FORM_24Q:
        return ComplianceFiling.TYPE_TDS_24Q
    if form_type == TDSReturnWorkpaper.FORM_27Q:
        return ComplianceFiling.TYPE_TDS_27Q
    return ComplianceFiling.TYPE_TDS_26Q


def _tds_return_templates(company, selected_period_start):
    if selected_period_start.month not in QUARTER_END_MONTHS:
        return []

    fy_start, quarter = quarter_for_date(selected_period_start)
    period_start, period_end = quarter_dates(fy_start, quarter)
    period_label = f"{quarter} FY {financial_year_label(fy_start)}"
    templates = []

    for form_type in [
        TDSReturnWorkpaper.FORM_24Q,
        TDSReturnWorkpaper.FORM_26Q,
        TDSReturnWorkpaper.FORM_27Q,
    ]:
        rule = resolve_tds_return_rule(company, fy_start, quarter, form_type)
        if not rule.get("enabled"):
            continue
        label = f"TDS {form_type}"
        templates.append(_template(
            filing_type=_tds_return_filing_type(form_type),
            label=label,
            title=f"{label} - {period_label}",
            period_start=period_start,
            period_end=period_end,
            due_date=rule["due_date"],
        ))

    return templates


def build_autopilot_templates(company, selected_period_start):
    selected_period_start = parse_month_start(selected_period_start)
    profile = get_statutory_profile(company)
    templates = []

    if profile.gst_registered:
        templates.append(_template(
            filing_type=ComplianceFiling.TYPE_GST_IMS,
            label="GST IMS Review",
            title=f"GST IMS Review - {selected_period_start:%b %Y}",
            period_start=selected_period_start,
            period_end=month_end(selected_period_start),
            due_date=period_due(selected_period_start, 10, 1),
        ))
        gstr1_template = _gst_rule_template(
            resolve_gstr1_rule(company, selected_period_start),
            filing_type=ComplianceFiling.TYPE_GSTR1,
            label="GSTR-1",
        )
        gstr3b_template = _gst_rule_template(
            resolve_gstr3b_rule(company, selected_period_start),
            filing_type=ComplianceFiling.TYPE_GSTR3B,
            label="GSTR-3B",
        )
        if gstr1_template:
            templates.append(gstr1_template)
        if gstr3b_template:
            templates.append(gstr3b_template)

    if profile.tds_applicable:
        period_end = month_end(selected_period_start)
        tds_deposit_rule = resolve_tds_deposit_rule(company, period_end)
        if tds_deposit_rule.get("enabled"):
            templates.append(_template(
                filing_type=ComplianceFiling.TYPE_TDS_PAYMENT,
                label="TDS Payment",
                title=f"TDS Payment - {selected_period_start:%b %Y}",
                period_start=selected_period_start,
                period_end=period_end,
                due_date=tds_deposit_rule["due_date"],
            ))
        templates.extend(_tds_return_templates(company, selected_period_start))

    return templates


def _unique_template_key(company, template):
    return (
        company.pk,
        template["filing_type"],
        template["period_start"],
        template["period_end"],
    )


def run_compliance_autopilot(
    *,
    companies,
    months=AUTOPILOT_DEFAULT_MONTHS,
    from_date=None,
    assigned_to=None,
    reviewer=None,
    created_by=None,
    dry_run=False,
):
    months = normalize_autopilot_months(months)
    start = parse_month_start(from_date or timezone.localdate())
    company_list = list(companies)
    created = []
    existing = []
    profile_warnings = []
    seen = set()

    with transaction.atomic():
        for company in company_list:
            for warning in autopilot_profile_warnings(company):
                profile_warnings.append({
                    "company": company.name,
                    "company_id": company.pk,
                    "message": warning,
                })

            for offset in range(months):
                period_start = add_months(start, offset)
                for template in build_autopilot_templates(company, period_start):
                    key = _unique_template_key(company, template)
                    if key in seen:
                        continue
                    seen.add(key)
                    item = ensure_filing(
                        company=company,
                        assigned_to=assigned_to,
                        reviewer=reviewer,
                        created_by=created_by,
                        dry_run=dry_run,
                        source_prefix="AUTO",
                        notes=(
                            "Auto-generated by Compliance Autopilot from the client statutory profile. "
                            "Verify portal notifications and client-specific due-date changes before filing."
                        ),
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
        "months": months,
        "items": items,
        "created_items": created,
        "existing_items": existing,
        "profile_warnings": profile_warnings,
    }
