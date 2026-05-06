from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import Sum
from django.urls import reverse

from .models import BankStatementRow, CompanySettings, GSTPeriodReview, PracticeTask
from vouchers.models import Voucher


@dataclass
class CloseCheck:
    code: str
    title: str
    severity: str
    count: int
    description: str
    action_label: str
    action_url: str
    task_type: str
    priority: str
    amount: Decimal = Decimal("0.00")

    @property
    def is_issue(self):
        return self.severity in {"critical", "warning"}


def _currency(value):
    return value or Decimal("0.00")


def _switch_url(company, target_url):
    return f"{reverse('core:switch_company', args=[company.pk])}?{urlencode({'next': target_url})}"


def _voucher_url(company, period_start, period_end, **extra):
    params = {
        "start_date": period_start.isoformat(),
        "end_date": period_end.isoformat(),
    }
    params.update({key: value for key, value in extra.items() if value})
    return _switch_url(company, f"{reverse('vouchers:list')}?{urlencode(params)}")


def _fy_label(period_end):
    fy_start = period_end.year if period_end.month >= 4 else period_end.year - 1
    return f"{fy_start}-{str(fy_start + 1)[-2:]}"


def _check(
    *,
    code,
    title,
    severity,
    count=0,
    amount=Decimal("0.00"),
    description,
    action_label,
    action_url,
    task_type=PracticeTask.TYPE_OTHER,
    priority=PracticeTask.PRIORITY_NORMAL,
):
    return CloseCheck(
        code=code,
        title=title,
        severity=severity,
        count=count,
        amount=_currency(amount),
        description=description,
        action_label=action_label,
        action_url=action_url,
        task_type=task_type,
        priority=priority,
    )


def build_close_workbench(company, period_start, period_end):
    period_value = period_start.strftime("%Y-%m")
    voucher_base = Voucher.objects.filter(company=company, date__gte=period_start, date__lte=period_end)
    checks = []

    draft_vouchers = voucher_base.exclude(status="APPROVED")
    draft_count = draft_vouchers.count()
    checks.append(_check(
        code="voucher_approval",
        title="Voucher approval",
        severity="critical" if draft_count else "ok",
        count=draft_count,
        description=(
            f"{draft_count} vouchers are draft, pending, or rejected in this period."
            if draft_count else "All vouchers in this period are approved."
        ),
        action_label="Open Vouchers",
        action_url=_voucher_url(company, period_start, period_end),
        task_type=PracticeTask.TYPE_OTHER,
        priority=PracticeTask.PRIORITY_CRITICAL,
    ))

    missing_number_count = voucher_base.filter(number="").count()
    checks.append(_check(
        code="missing_voucher_numbers",
        title="Voucher numbering",
        severity="warning" if missing_number_count else "ok",
        count=missing_number_count,
        description=(
            f"{missing_number_count} vouchers do not have a final voucher number."
            if missing_number_count else "Voucher numbering is complete for the period."
        ),
        action_label="Review Vouchers",
        action_url=_voucher_url(company, period_start, period_end),
        task_type=PracticeTask.TYPE_AUDIT,
        priority=PracticeTask.PRIORITY_HIGH,
    ))

    unreconciled_rows = BankStatementRow.objects.filter(
        statement__company=company,
        date__gte=period_start,
        date__lte=period_end,
        is_reconciled=False,
    )
    bank_amounts = unreconciled_rows.aggregate(total_debit=Sum("debit"), total_credit=Sum("credit"))
    bank_count = unreconciled_rows.count()
    bank_amount = _currency(bank_amounts["total_debit"]) + _currency(bank_amounts["total_credit"])
    checks.append(_check(
        code="bank_reconciliation",
        title="Bank reconciliation",
        severity="critical" if bank_count else "ok",
        count=bank_count,
        amount=bank_amount,
        description=(
            f"{bank_count} bank statement rows remain unreconciled."
            if bank_count else "Bank statement rows are reconciled for the period."
        ),
        action_label="Open Bank Rec",
        action_url=_switch_url(company, reverse("core:bank_statement_list")),
        task_type=PracticeTask.TYPE_BANK,
        priority=PracticeTask.PRIORITY_CRITICAL,
    ))

    unclaimed_itc = voucher_base.filter(voucher_type="Purchase", status="APPROVED", is_itc_claimed=False)
    unclaimed_itc_count = unclaimed_itc.count()
    unclaimed_itc_amount = _currency(unclaimed_itc.aggregate(total=Sum("total_tax"))["total"])
    checks.append(_check(
        code="unclaimed_itc",
        title="ITC claim status",
        severity="warning" if unclaimed_itc_count else "ok",
        count=unclaimed_itc_count,
        amount=unclaimed_itc_amount,
        description=(
            f"{unclaimed_itc_count} approved purchase vouchers still have unclaimed ITC."
            if unclaimed_itc_count else "Purchase ITC is marked claimed or not applicable."
        ),
        action_label="Open GST Workbench",
        action_url=reverse("core:gst_workbench_detail", args=[company.pk, period_value]),
        task_type=PracticeTask.TYPE_GST,
        priority=PracticeTask.PRIORITY_HIGH,
    ))

    try:
        from gstr2b.models import PortalGSTR2BEntry

        portal_entries = PortalGSTR2BEntry.objects.filter(
            company=company,
            invoice_date__gte=period_start,
            invoice_date__lte=period_end,
        )
        portal_exception_count = portal_entries.filter(
            match_status__in=["missing_in_books", "missing_in_portal"]
        ).count()
        pending_2b_count = portal_entries.filter(action_status="pending").count()
    except Exception:
        portal_exception_count = 0
        pending_2b_count = 0

    gst_issue_count = portal_exception_count + pending_2b_count
    checks.append(_check(
        code="gst_2b_exceptions",
        title="GST 2B exceptions",
        severity="critical" if portal_exception_count else ("warning" if pending_2b_count else "ok"),
        count=gst_issue_count,
        description=(
            f"{portal_exception_count} 2B mismatches and {pending_2b_count} pending 2B actions need review."
            if gst_issue_count else "GSTR-2B exceptions are clear for the period."
        ),
        action_label="Open GST Workbench",
        action_url=reverse("core:gst_workbench_detail", args=[company.pk, period_value]),
        task_type=PracticeTask.TYPE_GST,
        priority=PracticeTask.PRIORITY_CRITICAL if portal_exception_count else PracticeTask.PRIORITY_HIGH,
    ))

    gst_review = GSTPeriodReview.objects.filter(
        company=company,
        period_start=period_start,
        period_end=period_end,
        status=GSTPeriodReview.STATUS_SIGNED_OFF,
    ).first()
    checks.append(_check(
        code="gst_signoff",
        title="GST review sign-off",
        severity="warning" if not gst_review else "ok",
        count=0 if gst_review else 1,
        description=(
            "GST review has been signed off for this period."
            if gst_review else "GST review is not signed off yet."
        ),
        action_label="Open GST Workbench",
        action_url=reverse("core:gst_workbench_detail", args=[company.pk, period_value]),
        task_type=PracticeTask.TYPE_GST,
        priority=PracticeTask.PRIORITY_HIGH,
    ))

    stock_check = _stock_close_check(company, period_end)
    checks.append(stock_check)

    receivables = Voucher.objects.filter(
        company=company,
        voucher_type__in=["Sales", "Sales Return"],
        status="APPROVED",
        due_date__lte=period_end,
        outstanding_amount__gt=0,
    )
    receivable_amount = _currency(receivables.aggregate(total=Sum("outstanding_amount"))["total"])
    checks.append(_check(
        code="receivables_overdue",
        title="Receivables overdue",
        severity="warning" if receivables.exists() else "ok",
        count=receivables.count(),
        amount=receivable_amount,
        description=(
            "Customer invoices due by period end are still outstanding."
            if receivables.exists() else "No overdue receivables are blocking the close."
        ),
        action_label="Open Outstanding",
        action_url=_switch_url(company, reverse("vouchers:outstanding")),
        task_type=PracticeTask.TYPE_OTHER,
        priority=PracticeTask.PRIORITY_NORMAL,
    ))

    payables = Voucher.objects.filter(
        company=company,
        voucher_type__in=["Purchase", "Purchase Return"],
        status="APPROVED",
        due_date__lte=period_end,
        outstanding_amount__gt=0,
    )
    payable_amount = _currency(payables.aggregate(total=Sum("outstanding_amount"))["total"])
    checks.append(_check(
        code="payables_overdue",
        title="Payables overdue",
        severity="warning" if payables.exists() else "ok",
        count=payables.count(),
        amount=payable_amount,
        description=(
            "Vendor bills due by period end are still outstanding."
            if payables.exists() else "No overdue payables are blocking the close."
        ),
        action_label="Open Outstanding",
        action_url=_switch_url(company, reverse("vouchers:outstanding")),
        task_type=PracticeTask.TYPE_OTHER,
        priority=PracticeTask.PRIORITY_NORMAL,
    ))

    checks.append(_tds_close_check(company, period_end))
    checks.append(_payroll_close_check(company, period_start))
    checks.append(_depreciation_close_check(company, period_end))

    settings = CompanySettings.objects.filter(company=company).first()
    checks.extend(_lock_checks(company, period_start, period_end, settings))

    issue_checks = [check for check in checks if check.is_issue]
    critical_count = sum(1 for check in issue_checks if check.severity == "critical")
    warning_count = sum(1 for check in issue_checks if check.severity == "warning")
    score = max(0, 100 - (critical_count * 12) - (warning_count * 6))
    if critical_count:
        close_status = "Blocked"
    elif warning_count:
        close_status = "Review Pending"
    else:
        close_status = "Ready to Lock"

    return {
        "company": company,
        "period_start": period_start,
        "period_end": period_end,
        "period_value": period_value,
        "score": score,
        "close_status": close_status,
        "checks": checks,
        "issues": issue_checks,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "ok_count": sum(1 for check in checks if check.severity == "ok"),
        "voucher_count": voucher_base.count(),
        "approved_voucher_count": voucher_base.filter(status="APPROVED").count(),
        "task_reference_prefix": f"CLOSE:{company.pk}:{period_value}:",
    }


def create_close_tasks(report, user):
    created = []
    existing = []
    for issue in report["issues"]:
        reference = f"{report['task_reference_prefix']}{issue.code}"
        task, was_created = PracticeTask.objects.get_or_create(
            company=report["company"],
            reference=reference,
            defaults={
                "title": f"Close {report['period_value']}: {issue.title}",
                "task_type": issue.task_type,
                "priority": issue.priority,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": report["period_end"],
                "period_start": report["period_start"],
                "period_end": report["period_end"],
                "created_by": user,
                "description": issue.description,
            },
        )
        (created if was_created else existing).append(task)
    return {"created": len(created), "existing": len(existing), "created_tasks": created, "existing_tasks": existing}


def _stock_close_check(company, period_end):
    try:
        from inventory.models import StockItem

        negative = []
        low = []
        for item in StockItem.objects.filter(company=company, is_active=True).order_by("name"):
            closing_qty = item.closing_quantity(end_date=period_end)
            if closing_qty < 0:
                negative.append(item)
            elif item.is_low_stock(end_date=period_end):
                low.append(item)
        if negative:
            return _check(
                code="negative_stock",
                title="Negative stock",
                severity="critical",
                count=len(negative),
                description=f"{len(negative)} active stock items have negative closing quantity.",
                action_label="Open Stock Summary",
                action_url=_switch_url(company, reverse("inventory:summary")),
                task_type=PracticeTask.TYPE_AUDIT,
                priority=PracticeTask.PRIORITY_CRITICAL,
            )
        return _check(
            code="stock_review",
            title="Inventory review",
            severity="warning" if low else "ok",
            count=len(low),
            description=(
                f"{len(low)} active stock items are below their threshold."
                if low else "Inventory quantities are not negative."
            ),
            action_label="Open Stock Summary",
            action_url=_switch_url(company, reverse("inventory:summary")),
            task_type=PracticeTask.TYPE_AUDIT,
            priority=PracticeTask.PRIORITY_NORMAL,
        )
    except Exception:
        return _check(
            code="stock_review",
            title="Inventory review",
            severity="ok",
            description="Inventory module is not available for this workspace.",
            action_label="Open Dashboard",
            action_url=_switch_url(company, reverse("core:dashboard")),
        )


def _tds_close_check(company, period_end):
    try:
        from tds.models import TDSEntry

        entries = TDSEntry.objects.filter(company=company, transaction_date__lte=period_end, is_deposited=False)
        amount = _currency(entries.aggregate(total=Sum("tds_amount"))["total"])
        count = entries.count()
        return _check(
            code="tds_payable",
            title="TDS payable",
            severity="warning" if count else "ok",
            count=count,
            amount=amount,
            description=(
                f"{count} TDS entries are not deposited up to period end."
                if count else "No undeposited TDS is pending up to period end."
            ),
            action_label="Open TDS",
            action_url=_switch_url(company, reverse("tds:entry_list")),
            task_type=PracticeTask.TYPE_TDS,
            priority=PracticeTask.PRIORITY_HIGH,
        )
    except Exception:
        return _check(
            code="tds_payable",
            title="TDS payable",
            severity="ok",
            description="TDS module is not available for this workspace.",
            action_label="Open Dashboard",
            action_url=_switch_url(company, reverse("core:dashboard")),
            task_type=PracticeTask.TYPE_TDS,
        )


def _payroll_close_check(company, period_start):
    try:
        from payroll.models import Employee, PayrollRun

        active_employees = Employee.objects.filter(company=company, is_active=True).count()
        run = PayrollRun.objects.filter(company=company, month=period_start.month, year=period_start.year).first()
        if not active_employees:
            severity = "ok"
            count = 0
            description = "No active employees are configured for this company."
        elif not run:
            severity = "warning"
            count = active_employees
            description = "Active employees exist, but no payroll run exists for this month."
        elif run.status != PayrollRun.STATUS_FINALIZED:
            severity = "warning"
            count = 1
            description = f"Payroll run exists but is {run.status}, not finalized."
        else:
            severity = "ok"
            count = 0
            description = "Payroll run is finalized and posted."
        return _check(
            code="payroll_close",
            title="Payroll close",
            severity=severity,
            count=count,
            description=description,
            action_label="Open Payroll",
            action_url=_switch_url(company, reverse("payroll:payroll_run_list")),
            task_type=PracticeTask.TYPE_OTHER,
            priority=PracticeTask.PRIORITY_HIGH,
        )
    except Exception:
        return _check(
            code="payroll_close",
            title="Payroll close",
            severity="ok",
            description="Payroll module is not available for this workspace.",
            action_label="Open Dashboard",
            action_url=_switch_url(company, reverse("core:dashboard")),
        )


def _depreciation_close_check(company, period_end):
    try:
        from fixedassets.models import AssetDepreciation, FixedAsset

        if period_end.month != 3:
            return _check(
                code="fixed_asset_depreciation",
                title="Fixed asset depreciation",
                severity="ok",
                description="Annual depreciation check is due at financial year close.",
                action_label="Open Assets",
                action_url=_switch_url(company, reverse("fixedassets:asset_register")),
                task_type=PracticeTask.TYPE_AUDIT,
            )

        fy = _fy_label(period_end)
        active_assets = FixedAsset.objects.filter(
            company=company,
            status=FixedAsset.STATUS_ACTIVE,
            purchase_date__lte=period_end,
        )
        depreciated_ids = AssetDepreciation.objects.filter(
            asset__company=company,
            financial_year=fy,
        ).values_list("asset_id", flat=True)
        pending_count = active_assets.exclude(pk__in=depreciated_ids).count()
        return _check(
            code="fixed_asset_depreciation",
            title="Fixed asset depreciation",
            severity="warning" if pending_count else "ok",
            count=pending_count,
            description=(
                f"{pending_count} active assets do not have depreciation posted for {fy}."
                if pending_count else f"Fixed asset depreciation is posted for {fy}."
            ),
            action_label="Open Asset Register",
            action_url=_switch_url(company, reverse("fixedassets:asset_register")),
            task_type=PracticeTask.TYPE_AUDIT,
            priority=PracticeTask.PRIORITY_HIGH,
        )
    except Exception:
        return _check(
            code="fixed_asset_depreciation",
            title="Fixed asset depreciation",
            severity="ok",
            description="Fixed assets module is not available for this workspace.",
            action_label="Open Dashboard",
            action_url=_switch_url(company, reverse("core:dashboard")),
            task_type=PracticeTask.TYPE_AUDIT,
        )


def _lock_checks(company, period_start, period_end, settings):
    if not settings:
        return [_check(
            code="period_settings",
            title="Period lock settings",
            severity="warning",
            count=1,
            description="Company settings are missing, so period lock status cannot be verified.",
            action_label="Open Settings",
            action_url=_switch_url(company, reverse("core:company_settings")),
            task_type=PracticeTask.TYPE_AUDIT,
            priority=PracticeTask.PRIORITY_HIGH,
        )]

    lock_specs = [
        ("books_locked", "Books lock", settings.books_closed_until, "Books are not locked through this period."),
        ("bank_locked", "Bank lock", settings.bank_locked_until, "Bank entries are not locked through this period."),
        ("inventory_locked", "Inventory lock", settings.inventory_locked_until, "Inventory entries are not locked through this period."),
    ]
    checks = []
    for code, title, locked_until, warning_text in lock_specs:
        is_locked = bool(locked_until and locked_until >= period_end)
        checks.append(_check(
            code=code,
            title=title,
            severity="ok" if is_locked else "warning",
            count=0 if is_locked else 1,
            description=(
                f"{title} is active through {locked_until:%d %b %Y}."
                if is_locked else warning_text
            ),
            action_label="Open Settings",
            action_url=_switch_url(company, reverse("core:company_settings")),
            task_type=PracticeTask.TYPE_AUDIT,
            priority=PracticeTask.PRIORITY_HIGH,
        ))
    return checks
