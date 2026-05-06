import calendar
from datetime import date
from decimal import Decimal

from django.db.models import Q

from tds.models import TDSReturnWorkpaper
from tds.workbench import return_due_date

from .models import CompanyStatutoryProfile, StatutoryRuleOverride


ZERO = Decimal("0.00")
QUARTER_END_MONTHS = {3, 6, 9, 12}


def add_months(value, months):
    month_index = (value.month - 1) + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_end(value):
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def period_due(period_start, day, month_offset=1):
    due_month = add_months(period_start, month_offset)
    return due_month.replace(day=min(day, calendar.monthrange(due_month.year, due_month.month)[1]))


def get_statutory_profile(company):
    try:
        return company.statutory_profile
    except CompanyStatutoryProfile.DoesNotExist:
        return CompanyStatutoryProfile(company=company)


def _quarter_bounds(value):
    if value.month <= 3:
        quarter_start = date(value.year, 1, 1)
        quarter_end = date(value.year, 3, 31)
        quarter_label = "Q4"
        fy_start = value.year - 1
    elif value.month <= 6:
        quarter_start = date(value.year, 4, 1)
        quarter_end = date(value.year, 6, 30)
        quarter_label = "Q1"
        fy_start = value.year
    elif value.month <= 9:
        quarter_start = date(value.year, 7, 1)
        quarter_end = date(value.year, 9, 30)
        quarter_label = "Q2"
        fy_start = value.year
    else:
        quarter_start = date(value.year, 10, 1)
        quarter_end = date(value.year, 12, 31)
        quarter_label = "Q3"
        fy_start = value.year
    return quarter_start, quarter_end, quarter_label, fy_start


def _financial_year_label(fy_start):
    return f"FY {fy_start}-{str(fy_start + 1)[-2:]}"


def _period_label(period_start, period_end):
    if period_start == period_end.replace(day=1):
        return f"{period_start:%b %Y}"
    _, _, quarter_label, fy_start = _quarter_bounds(period_end)
    return f"{quarter_label} {_financial_year_label(fy_start)}"


def _active_override(company, rule_type, period_start=None, period_end=None):
    queryset = StatutoryRuleOverride.objects.filter(
        company=company,
        rule_type=rule_type,
        is_active=True,
    )
    if period_start and period_end:
        queryset = queryset.filter(
            Q(period_start__isnull=True) | Q(period_start__lte=period_end),
            Q(period_end__isnull=True) | Q(period_end__gte=period_start),
        )
    elif period_start:
        queryset = queryset.filter(
            Q(period_start__isnull=True) | Q(period_start__lte=period_start),
            Q(period_end__isnull=True) | Q(period_end__gte=period_start),
        )
    return queryset.order_by("-period_start", "-updated_at", "-id").first()


def _override_money(override, field_name, fallback):
    if override:
        value = getattr(override, field_name, None)
        if value is not None:
            return value
    return fallback


def _override_rate(override, field_name, fallback_percent):
    percent = _override_money(override, field_name, fallback_percent)
    return (percent or ZERO) / Decimal("100")


def _source_reference(override, default="profile"):
    if not override:
        return default
    due_text = override.override_due_date.isoformat() if override.override_due_date else "rate-only"
    return f"override:{override.get_rule_type_display()}:{due_text}"


def resolve_gstr1_rule(company, selected_period_start):
    profile = get_statutory_profile(company)
    if not profile.gst_registered:
        return {"enabled": False}

    if profile.gstr1_frequency == CompanyStatutoryProfile.GSTR1_QUARTERLY:
        period_start, period_end, _, _ = _quarter_bounds(selected_period_start)
        due = period_due(period_end.replace(day=1), profile.gstr1_quarterly_due_day, 1)
    else:
        period_start = selected_period_start
        period_end = month_end(selected_period_start)
        due = period_due(period_start, profile.gstr1_monthly_due_day, 1)

    override = _active_override(company, StatutoryRuleOverride.RULE_GSTR1, period_start, period_end)
    return {
        "enabled": True,
        "period_start": period_start,
        "period_end": period_end,
        "period_label": _period_label(period_start, period_end),
        "due_date": override.override_due_date if override and override.override_due_date else due,
        "late_fee_per_day": _override_money(override, "late_fee_per_day", profile.gst_late_fee_per_day),
        "nil_late_fee_per_day": profile.gst_nil_late_fee_per_day,
        "interest_rate": profile.gst_interest_rate_percent / Decimal("100"),
        "source_reference": _source_reference(override),
    }


def resolve_gstr3b_rule(company, selected_period_start):
    profile = get_statutory_profile(company)
    if not profile.gst_registered:
        return {"enabled": False}

    if profile.gst_return_frequency == CompanyStatutoryProfile.GST_FREQUENCY_QRMP:
        period_start, period_end, _, _ = _quarter_bounds(selected_period_start)
        due = period_due(period_end.replace(day=1), profile.gstr3b_qrmp_due_day, 1)
    else:
        period_start = selected_period_start
        period_end = month_end(selected_period_start)
        due = period_due(period_start, profile.gstr3b_monthly_due_day, 1)

    override = _active_override(company, StatutoryRuleOverride.RULE_GSTR3B, period_start, period_end)
    return {
        "enabled": True,
        "period_start": period_start,
        "period_end": period_end,
        "period_label": _period_label(period_start, period_end),
        "due_date": override.override_due_date if override and override.override_due_date else due,
        "late_fee_per_day": _override_money(override, "late_fee_per_day", profile.gst_late_fee_per_day),
        "interest_rate": _override_rate(override, "interest_rate_percent", profile.gst_interest_rate_percent),
        "source_reference": _source_reference(override),
    }


def resolve_tds_deposit_rule(company, transaction_date):
    profile = get_statutory_profile(company)
    if not profile.tds_applicable:
        return {"enabled": False}

    if transaction_date.month == 3:
        due = date(transaction_date.year, 4, min(profile.tds_march_deposit_due_day, 30))
    elif transaction_date.month == 12:
        due = date(transaction_date.year + 1, 1, min(profile.tds_deposit_due_day, 31))
    else:
        next_month = add_months(transaction_date.replace(day=1), 1)
        due = next_month.replace(day=min(profile.tds_deposit_due_day, calendar.monthrange(next_month.year, next_month.month)[1]))

    override = _active_override(company, StatutoryRuleOverride.RULE_TDS_DEPOSIT, transaction_date)
    return {
        "enabled": True,
        "due_date": override.override_due_date if override and override.override_due_date else due,
        "interest_rate_per_month": _override_rate(
            override,
            "interest_rate_percent",
            profile.tds_deposit_interest_rate_percent_per_month,
        ),
        "source_reference": _source_reference(override),
    }


def _tds_return_rule_type(form_type):
    if form_type == TDSReturnWorkpaper.FORM_24Q:
        return StatutoryRuleOverride.RULE_TDS_RETURN_24Q
    if form_type == TDSReturnWorkpaper.FORM_27Q:
        return StatutoryRuleOverride.RULE_TDS_RETURN_27Q
    return StatutoryRuleOverride.RULE_TDS_RETURN_26Q


def _tds_form_enabled(profile, form_type):
    if form_type == TDSReturnWorkpaper.FORM_24Q:
        return profile.tds_24q_enabled
    if form_type == TDSReturnWorkpaper.FORM_27Q:
        return profile.tds_27q_enabled
    return profile.tds_26q_enabled


def resolve_tds_return_rule(company, fy_start, quarter, form_type=TDSReturnWorkpaper.FORM_26Q):
    profile = get_statutory_profile(company)
    if not profile.tds_applicable or not _tds_form_enabled(profile, form_type):
        return {"enabled": False}

    due = return_due_date(fy_start, quarter)
    if quarter == TDSReturnWorkpaper.Q1:
        period_start, period_end = date(fy_start, 4, 1), date(fy_start, 6, 30)
    elif quarter == TDSReturnWorkpaper.Q2:
        period_start, period_end = date(fy_start, 7, 1), date(fy_start, 9, 30)
    elif quarter == TDSReturnWorkpaper.Q3:
        period_start, period_end = date(fy_start, 10, 1), date(fy_start, 12, 31)
    else:
        period_start, period_end = date(fy_start + 1, 1, 1), date(fy_start + 1, 3, 31)

    override = _active_override(company, _tds_return_rule_type(form_type), period_start, period_end)
    return {
        "enabled": True,
        "due_date": override.override_due_date if override and override.override_due_date else due,
        "late_fee_per_day": _override_money(override, "late_fee_per_day", profile.tds_return_late_fee_per_day),
        "source_reference": _source_reference(override),
    }


def resolve_msme_rule(company):
    profile = get_statutory_profile(company)
    return {
        "enabled": profile.msme_watch_enabled,
        "default_credit_days": profile.msme_default_credit_days,
        "interest_rate": profile.msme_interest_rate_percent / Decimal("100"),
    }
