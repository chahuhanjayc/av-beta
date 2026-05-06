from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from .close_workbench import build_close_workbench
from .models import (
    BankStatementRow,
    ComplianceFiling,
    ComplianceNotice,
    GSTPeriodReview,
    PracticeTask,
)
from vouchers.quality import build_voucher_quality_report
from vouchers.models import Voucher


@dataclass
class ReadinessCheck:
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


def build_filing_readiness(company, period_start, period_end):
    period_value = period_start.strftime("%Y-%m")
    close_report = build_close_workbench(company, period_start, period_end)
    quality_report = build_voucher_quality_report(
        company,
        start_date=period_start,
        end_date=period_end,
        status="all",
    )
    gst = _gst_metrics(company, period_start, period_end)
    bank = _bank_metrics(company, period_start, period_end)
    documents = _document_metrics(company, period_start, period_end)
    tasks = _task_metrics(company, period_start, period_end)
    review = GSTPeriodReview.objects.filter(
        company=company,
        period_start=period_start,
        period_end=period_end,
    ).select_related("prepared_by", "reviewed_by").first()

    checks = [
        _close_check(company, period_value, close_report),
        _voucher_quality_check(company, period_start, period_end, quality_report),
        _gst_check(company, period_value, gst),
        _bank_check(company, bank),
        _document_check(company, documents),
        _task_queue_check(company, tasks),
    ]

    issue_checks = [check for check in checks if check.is_issue]
    critical_count = sum(1 for check in issue_checks if check.severity == "critical")
    warning_count = sum(1 for check in issue_checks if check.severity == "warning")
    score = max(0, 100 - (critical_count * 15) - (warning_count * 7))
    signed_off = bool(review and review.status == GSTPeriodReview.STATUS_SIGNED_OFF)

    if critical_count:
        status = "Blocked"
    elif warning_count:
        status = "Review pending"
    elif signed_off:
        status = "Signed off"
    else:
        status = "Ready for sign-off"

    return {
        "company": company,
        "period_start": period_start,
        "period_end": period_end,
        "period_value": period_value,
        "score": score,
        "status": status,
        "signed_off": signed_off,
        "review": review,
        "checks": checks,
        "issues": issue_checks,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "ok_count": sum(1 for check in checks if check.severity == "ok"),
        "close": close_report,
        "voucher_quality": quality_report,
        "gst": gst,
        "bank": bank,
        "documents": documents,
        "tasks": tasks,
        "task_reference_prefix": f"READY:{company.pk}:{period_value}:",
        "snapshot": _snapshot(company, period_start, period_end, score, status, checks, gst, bank, documents, tasks, quality_report),
    }


def create_filing_readiness_tasks(report, user):
    created = []
    existing = []
    today = timezone.localdate()

    for issue in report["issues"]:
        reference = f"{report['task_reference_prefix']}{issue.code}"
        due_days = 2 if issue.severity == "critical" else 7
        task, was_created = PracticeTask.objects.get_or_create(
            company=report["company"],
            reference=reference,
            defaults={
                "title": f"Filing readiness {report['period_value']}: {issue.title}",
                "task_type": issue.task_type,
                "priority": issue.priority,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": today + timedelta(days=due_days),
                "period_start": report["period_start"],
                "period_end": report["period_end"],
                "created_by": user,
                "description": f"{issue.description}\nAction: {issue.action_label}",
            },
        )
        (created if was_created else existing).append(task)

    return {"created": len(created), "existing": len(existing), "created_tasks": created, "existing_tasks": existing}


def save_filing_readiness_review(report, user, status, notes):
    allowed = {
        GSTPeriodReview.STATUS_IN_REVIEW,
        GSTPeriodReview.STATUS_SIGNED_OFF,
        GSTPeriodReview.STATUS_REOPENED,
    }
    if status not in allowed:
        raise ValueError("Invalid review status.")
    if status == GSTPeriodReview.STATUS_SIGNED_OFF and report["critical_count"]:
        raise ValueError("Critical readiness blockers must be cleared before sign-off.")

    review, _ = GSTPeriodReview.objects.get_or_create(
        company=report["company"],
        period_start=report["period_start"],
        period_end=report["period_end"],
        defaults={"prepared_by": user},
    )
    review.status = status
    review.risk_score = 100 - report["score"]
    review.summary_snapshot = {
        **(review.summary_snapshot or {}),
        "filing_readiness": report["snapshot"],
    }
    review.notes = notes.strip()
    if not review.prepared_by_id:
        review.prepared_by = user
    if status == GSTPeriodReview.STATUS_SIGNED_OFF:
        review.reviewed_by = user
        review.reviewed_at = timezone.now()
    elif status == GSTPeriodReview.STATUS_REOPENED:
        review.reviewed_by = None
        review.reviewed_at = None
    review.save()
    return review


def _check(
    *,
    code,
    title,
    severity,
    count,
    description,
    action_label,
    action_url,
    task_type,
    priority,
    amount=Decimal("0.00"),
):
    return ReadinessCheck(
        code=code,
        title=title,
        severity=severity,
        count=count,
        amount=amount or Decimal("0.00"),
        description=description,
        action_label=action_label,
        action_url=action_url,
        task_type=task_type,
        priority=priority,
    )


def _switch_url(company, target_url):
    return f"{reverse('core:switch_company', args=[company.pk])}?{urlencode({'next': target_url})}"


def _close_check(company, period_value, close_report):
    issue_count = close_report["critical_count"] + close_report["warning_count"]
    if close_report["critical_count"]:
        severity = "critical"
    elif close_report["warning_count"]:
        severity = "warning"
    else:
        severity = "ok"
    return _check(
        code="close_workbench",
        title="Books close readiness",
        severity=severity,
        count=issue_count,
        description=(
            f"{close_report['critical_count']} critical and {close_report['warning_count']} warning close checks remain."
            if issue_count else "Books close checks are clear for this period."
        ),
        action_label="Open Close Workbench",
        action_url=reverse("core:accounting_close") + f"?period={period_value}&company={company.pk}",
        task_type=PracticeTask.TYPE_AUDIT,
        priority=PracticeTask.PRIORITY_CRITICAL if severity == "critical" else PracticeTask.PRIORITY_HIGH,
    )


def _voucher_quality_check(company, period_start, period_end, quality_report):
    issue_count = quality_report["critical_count"] + quality_report["warning_count"]
    if quality_report["critical_count"]:
        severity = "critical"
    elif quality_report["warning_count"]:
        severity = "warning"
    else:
        severity = "ok"
    params = urlencode({
        "start_date": period_start.isoformat(),
        "end_date": period_end.isoformat(),
        "status": "all",
    })
    return _check(
        code="voucher_quality",
        title="Voucher quality",
        severity=severity,
        count=issue_count,
        description=(
            f"{quality_report['critical_count']} critical and {quality_report['warning_count']} warning voucher quality issues remain."
            if issue_count else "Voucher quality checks are clear for this period."
        ),
        action_label="Open Voucher Quality",
        action_url=_switch_url(company, f"{reverse('vouchers:quality')}?{params}"),
        task_type=PracticeTask.TYPE_AUDIT,
        priority=PracticeTask.PRIORITY_CRITICAL if severity == "critical" else PracticeTask.PRIORITY_HIGH,
    )


def _gst_check(company, period_value, gst):
    critical = gst["missing_in_books"] + gst["rejected_2b"] + gst["overdue_filings"] + gst["draft_gst_vouchers"]
    warnings = gst["missing_in_portal"] + gst["pending_2b"] + gst["open_filings"] + gst["overdue_notices"]
    if critical:
        severity = "critical"
    elif warnings:
        severity = "warning"
    else:
        severity = "ok"
    return _check(
        code="gst_readiness",
        title="GST filing readiness",
        severity=severity,
        count=critical + warnings,
        description=(
            f"{critical} critical GST blockers and {warnings} GST review items remain."
            if critical or warnings else "GST filings, 2B actions, notices, and draft GST vouchers are clear."
        ),
        action_label="Open GST Workbench",
        action_url=reverse("core:gst_workbench_detail", args=[company.pk, period_value]),
        task_type=PracticeTask.TYPE_GST,
        priority=PracticeTask.PRIORITY_CRITICAL if severity == "critical" else PracticeTask.PRIORITY_HIGH,
    )


def _bank_check(company, bank):
    severity = "critical" if bank["unreconciled_count"] else ("warning" if bank["duplicate_count"] else "ok")
    return _check(
        code="bank_reconciliation",
        title="Bank reconciliation",
        severity=severity,
        count=bank["unreconciled_count"] + bank["duplicate_count"],
        amount=bank["unreconciled_amount"],
        description=(
            f"{bank['unreconciled_count']} unreconciled rows and {bank['duplicate_count']} possible duplicate rows remain."
            if severity != "ok" else "Bank statement rows are reconciled and duplicate flags are clear."
        ),
        action_label="Open Bank Reconciliation",
        action_url=_switch_url(company, reverse("core:bank_statement_list")),
        task_type=PracticeTask.TYPE_BANK,
        priority=PracticeTask.PRIORITY_CRITICAL if severity == "critical" else PracticeTask.PRIORITY_HIGH,
    )


def _document_check(company, documents):
    if documents["overdue_open"]:
        severity = "critical"
    elif documents["open"] or documents["uploaded"]:
        severity = "warning"
    else:
        severity = "ok"
    return _check(
        code="client_documents",
        title="Client document chase",
        severity=severity,
        count=documents["open"] + documents["uploaded"],
        description=(
            f"{documents['open']} open requests, {documents['overdue_open']} overdue requests, and {documents['uploaded']} uploads awaiting review."
            if severity != "ok" else "No client document requests are blocking this period."
        ),
        action_label="Open Client Requests",
        action_url=f"{reverse('portal:client_requests')}?{urlencode({'company': company.pk, 'status': 'active'})}",
        task_type=PracticeTask.TYPE_DOCUMENT,
        priority=PracticeTask.PRIORITY_CRITICAL if severity == "critical" else PracticeTask.PRIORITY_HIGH,
    )


def _task_queue_check(company, tasks):
    if tasks["blocked"] or tasks["overdue"]:
        severity = "critical"
    elif tasks["open"]:
        severity = "warning"
    else:
        severity = "ok"
    return _check(
        code="period_tasks",
        title="Period work queue",
        severity=severity,
        count=tasks["open"],
        description=(
            f"{tasks['open']} open tasks remain, including {tasks['blocked']} blocked and {tasks['overdue']} overdue."
            if severity != "ok" else "No open practice tasks remain for this period."
        ),
        action_label="Open Work Queue",
        action_url=_switch_url(company, f"{reverse('core:practice_tasks')}?status=open"),
        task_type=PracticeTask.TYPE_OTHER,
        priority=PracticeTask.PRIORITY_CRITICAL if severity == "critical" else PracticeTask.PRIORITY_HIGH,
    )


def _gst_metrics(company, period_start, period_end):
    from gstr2b.models import PortalGSTR2BEntry
    from reports.utils import get_gst_report

    today = timezone.localdate()
    portal_qs = PortalGSTR2BEntry.objects.filter(
        company=company,
        invoice_date__gte=period_start,
        invoice_date__lte=period_end,
    )
    filings = ComplianceFiling.objects.filter(
        company=company,
        filing_type__in=[
            ComplianceFiling.TYPE_GST_IMS,
            ComplianceFiling.TYPE_GSTR1,
            ComplianceFiling.TYPE_GSTR3B,
        ],
        period_start=period_start,
        period_end=period_end,
    )
    open_filings = filings.exclude(status__in=[ComplianceFiling.STATUS_FILED, ComplianceFiling.STATUS_CANCELLED])
    open_notices = ComplianceNotice.objects.filter(
        company=company,
        notice_type=ComplianceNotice.TYPE_GST,
    ).exclude(status=ComplianceNotice.STATUS_CLOSED)
    gst_report = get_gst_report(company, period_start, period_end)

    return {
        "matched_2b": portal_qs.filter(match_status="matched").count(),
        "missing_in_books": portal_qs.filter(match_status="missing_in_books").count(),
        "missing_in_portal": Voucher.objects.filter(
            company=company,
            voucher_type="Purchase",
            status="APPROVED",
            is_itc_claimed=False,
            date__gte=period_start,
            date__lte=period_end,
        ).count(),
        "pending_2b": portal_qs.filter(action_status="pending").count(),
        "rejected_2b": portal_qs.filter(action_status="rejected").count(),
        "draft_gst_vouchers": Voucher.objects.filter(
            company=company,
            voucher_type__in=["Sales", "Purchase"],
            date__gte=period_start,
            date__lte=period_end,
        ).exclude(status="APPROVED").count(),
        "open_filings": open_filings.count(),
        "overdue_filings": open_filings.filter(due_date__lt=today).count(),
        "open_notices": open_notices.count(),
        "overdue_notices": open_notices.filter(response_due_date__lt=today).count(),
        "output_tax": gst_report["tot_out_tax"],
        "itc": gst_report["tot_itc"],
        "net_tax_payable": gst_report["net_tax_payable"],
    }


def _bank_metrics(company, period_start, period_end):
    rows = BankStatementRow.objects.filter(
        statement__company=company,
        date__gte=period_start,
        date__lte=period_end,
    )
    unreconciled = rows.filter(is_reconciled=False)
    amounts = unreconciled.aggregate(total_debit=Sum("debit"), total_credit=Sum("credit"))
    return {
        "unreconciled_count": unreconciled.count(),
        "duplicate_count": rows.filter(potential_duplicate=True).count(),
        "unreconciled_amount": (amounts["total_debit"] or Decimal("0.00")) + (amounts["total_credit"] or Decimal("0.00")),
    }


def _document_metrics(company, period_start, period_end):
    from portal.models import ClientDocumentRequest

    today = timezone.localdate()
    requests = ClientDocumentRequest.objects.filter(
        company=company,
        created_at__date__lte=period_end,
    ).exclude(status=ClientDocumentRequest.STATUS_CANCELLED)
    open_requests = requests.filter(status=ClientDocumentRequest.STATUS_OPEN)
    uploaded = requests.filter(status=ClientDocumentRequest.STATUS_UPLOADED)
    return {
        "open": open_requests.count(),
        "overdue_open": open_requests.filter(due_date__lt=today).count(),
        "uploaded": uploaded.count(),
    }


def _task_metrics(company, period_start, period_end):
    today = timezone.localdate()
    tasks = PracticeTask.objects.filter(
        company=company,
        period_start=period_start,
        period_end=period_end,
    ).exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
    return {
        "open": tasks.count(),
        "blocked": tasks.filter(status=PracticeTask.STATUS_BLOCKED).count(),
        "overdue": tasks.filter(due_date__lt=today).count(),
        "critical": tasks.filter(priority=PracticeTask.PRIORITY_CRITICAL).count(),
    }


def _snapshot(company, period_start, period_end, score, status, checks, gst, bank, documents, tasks, quality_report):
    return {
        "company_id": company.pk,
        "company": company.name,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "score": score,
        "status": status,
        "checks": [
            {
                "code": check.code,
                "severity": check.severity,
                "count": check.count,
                "amount": str(check.amount),
            }
            for check in checks
        ],
        "gst": {
            "missing_in_books": gst["missing_in_books"],
            "missing_in_portal": gst["missing_in_portal"],
            "pending_2b": gst["pending_2b"],
            "open_filings": gst["open_filings"],
            "net_tax_payable": str(gst["net_tax_payable"]),
        },
        "bank": {
            "unreconciled_count": bank["unreconciled_count"],
            "duplicate_count": bank["duplicate_count"],
            "unreconciled_amount": str(bank["unreconciled_amount"]),
        },
        "documents": documents,
        "tasks": tasks,
        "voucher_quality": {
            "score": quality_report["score"],
            "critical_count": quality_report["critical_count"],
            "warning_count": quality_report["warning_count"],
        },
        "generated_at": timezone.now().isoformat(),
    }
