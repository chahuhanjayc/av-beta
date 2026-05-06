import csv
from datetime import date
from urllib.parse import urlencode

from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from integrations.models import IntegrationConnector, IntegrationRequestLog, StatutoryExportLog
from migration.models import ImportSession
from migration.views import _enhance_quality_report_with_mapping

from .models import Company, ComplianceFiling, FilingReview, PracticeTask


SEVERITY_RANK = {
    "critical": 0,
    "high": 1,
    "warning": 2,
    "info": 3,
}


def _accessible_companies(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return Company.objects.filter(user_access__user=user).distinct().order_by("name")


def _switch_url(company, next_path):
    return f"{reverse('core:switch_company', args=[company.pk])}?{urlencode({'next': next_path})}"


def _item(*, category, company, severity, status, title, description, action_label, action_url, count=1, due_date=None, owner="", reference="", created_at=None):
    return {
        "category": category,
        "company": company,
        "severity": severity,
        "severity_rank": SEVERITY_RANK.get(severity, 4),
        "status": status,
        "title": title,
        "description": description,
        "action_label": action_label,
        "action_url": action_url,
        "count": count,
        "due_date": due_date,
        "owner": owner,
        "reference": reference,
        "created_at": created_at,
    }


def _period_value(period_start):
    if not period_start:
        return timezone.localdate().strftime("%Y-%m")
    return period_start.strftime("%Y-%m")


def _filing_review_url(company, period_start, review_type):
    next_path = (
        f"{reverse('core:filing_review_center')}?"
        f"{urlencode({'period': _period_value(period_start), 'company': company.pk, 'review_type': review_type})}"
    )
    return _switch_url(company, next_path)


def _filing_list_url(company, status):
    next_path = f"{reverse('core:compliance_filings')}?{urlencode({'company': company.pk, 'status': status})}"
    return _switch_url(company, next_path)


def _integration_url(company, service=None, status=None, cockpit=None):
    if cockpit == "e_invoice":
        next_path = f"{reverse('integrations:e_invoice_cockpit')}?status=failed"
    elif cockpit == "e_way_bill":
        next_path = f"{reverse('integrations:e_way_bill_cockpit')}?status=failed"
    else:
        query = {"type": "requests"}
        if service:
            query["service"] = service
        if status:
            query["request_status"] = status
        next_path = f"{reverse('integrations:evidence_center')}?{urlencode(query)}"
    return _switch_url(company, next_path)


def _connector_url(company):
    return _switch_url(company, reverse("integrations:dashboard"))


def _add_migration_items(items, companies):
    sessions = (
        ImportSession.objects.filter(company__in=companies)
        .exclude(status="confirmed")
        .select_related("company", "user", "approved_by")
        .order_by("-created_at")[:200]
    )
    for session in sessions:
        report = _enhance_quality_report_with_mapping(session)
        gate = report.get("approval_gate") or {}
        if not gate.get("required"):
            continue
        preview_url = _switch_url(session.company, reverse("migration:preview", args=[session.pk]))
        if not gate.get("can_confirm"):
            blocker_count = len(gate.get("blockers") or [])
            has_critical = any(blocker.get("severity") == "critical" for blocker in gate.get("blockers") or [])
            severity = "critical" if has_critical or gate.get("stale") else "high"
            items.append(_item(
                category="Tally Migration",
                company=session.company,
                severity=severity,
                status=gate.get("status_label", "Required"),
                title="Tally import approval required",
                description=(
                    f"Session #{session.pk} is blocked by {blocker_count} approval blocker(s). "
                    f"Risk score {report.get('sync_risk', {}).get('score', 100)} / 100."
                ),
                action_label="Review Import",
                action_url=preview_url,
                count=blocker_count or 1,
                owner=session.user.email if session.user else "",
                reference=f"IMPORT-{session.pk}",
                created_at=session.created_at,
            ))
        elif session.approval_status == ImportSession.APPROVAL_APPROVED:
            items.append(_item(
                category="Tally Migration",
                company=session.company,
                severity="info",
                status="Approved",
                title="Approved import awaiting confirmation",
                description=f"Session #{session.pk} has CA approval evidence and is ready for final import.",
                action_label="Confirm Import",
                action_url=preview_url,
                count=1,
                owner=session.approved_by.email if session.approved_by else "",
                reference=session.approval_evidence_hash[:16],
                created_at=session.approved_at or session.created_at,
            ))


def _add_filing_review_items(items, companies):
    reviews = (
        FilingReview.objects.filter(company__in=companies)
        .exclude(status=FilingReview.STATUS_APPROVED)
        .select_related("company", "prepared_by", "reviewed_by")
        .order_by("period_start", "company__name")[:200]
    )
    for review in reviews:
        approval = (review.blocker_snapshot or {}).get("approval") or {}
        unwaived_critical = approval.get("unwaived_critical_count", 0)
        unwaived_warning = approval.get("unwaived_warning_count", 0)
        if review.status == FilingReview.STATUS_REVIEWED:
            severity = "critical" if unwaived_critical else "high"
            title = "Filing review awaiting approval"
        elif review.status in {FilingReview.STATUS_UNDER_REVIEW, FilingReview.STATUS_REOPENED}:
            severity = "warning"
            title = "Filing review in progress"
        elif review.status == FilingReview.STATUS_SENT_BACK:
            severity = "high"
            title = "Filing review sent back"
        else:
            continue
        items.append(_item(
            category="Filing Review",
            company=review.company,
            severity=severity,
            status=review.get_status_display(),
            title=title,
            description=(
                f"{review.get_review_type_display()} {_period_value(review.period_start)}. "
                f"{unwaived_critical} critical and {unwaived_warning} warning blocker(s)."
            ),
            action_label="Open Review",
            action_url=_filing_review_url(review.company, review.period_start, review.review_type),
            count=max(1, unwaived_critical + unwaived_warning),
            due_date=review.period_end,
            owner=review.reviewed_by.email if review.reviewed_by else review.prepared_by.email if review.prepared_by else "",
            reference=f"FREV-{review.pk}",
            created_at=review.updated_at,
        ))


def _add_compliance_filing_items(items, companies, today):
    filings = (
        ComplianceFiling.objects.filter(company__in=companies)
        .filter(status__in=[
            ComplianceFiling.STATUS_READY_FOR_REVIEW,
            ComplianceFiling.STATUS_BLOCKED,
            ComplianceFiling.STATUS_CLIENT_PENDING,
        ])
        .select_related("company", "assigned_to", "reviewer")
        .order_by("due_date", "company__name")[:250]
    )
    for filing in filings:
        overdue = bool(filing.due_date and filing.due_date < today)
        if filing.status == ComplianceFiling.STATUS_READY_FOR_REVIEW:
            severity = "critical" if overdue else "high"
            title = "Compliance filing ready for CA review"
            filter_status = "ready_for_review"
        elif filing.status == ComplianceFiling.STATUS_BLOCKED:
            severity = "critical" if overdue else "high"
            title = "Compliance filing blocked"
            filter_status = "blocked"
        else:
            severity = "warning"
            title = "Compliance filing waiting on client"
            filter_status = "client_pending"
        items.append(_item(
            category="Compliance Filing",
            company=filing.company,
            severity=severity,
            status=filing.get_status_display(),
            title=title,
            description=f"{filing.title} is due {filing.due_date or 'without a due date'}.",
            action_label="Open Filing",
            action_url=_filing_list_url(filing.company, filter_status),
            count=1,
            due_date=filing.due_date,
            owner=filing.reviewer.email if filing.reviewer else filing.assigned_to.email if filing.assigned_to else "",
            reference=filing.arn_ack_number or filing.source_reference or f"FILING-{filing.pk}",
            created_at=filing.updated_at,
        ))


def _add_integration_items(items, companies):
    connector_statuses = [
        IntegrationConnector.STATUS_BLOCKED,
        IntegrationConnector.STATUS_NEEDS_SETUP,
    ]
    connectors = (
        IntegrationConnector.objects.filter(company__in=companies, status__in=connector_statuses)
        .select_related("company")
        .order_by("company__name", "connector_type")[:200]
    )
    for connector in connectors:
        severity = "critical" if connector.status == IntegrationConnector.STATUS_BLOCKED else "warning"
        items.append(_item(
            category="Integration",
            company=connector.company,
            severity=severity,
            status=connector.get_status_display(),
            title=f"{connector.label} connector needs attention",
            description=connector.last_error or "Connector setup is incomplete for statutory workflows.",
            action_label="Configure",
            action_url=_connector_url(connector.company),
            count=1,
            owner=connector.username or "",
            reference=connector.get_connector_type_display(),
            created_at=connector.updated_at,
        ))

    logs = (
        IntegrationRequestLog.objects.filter(
            company__in=companies,
            status__in=[IntegrationRequestLog.STATUS_FAILED, IntegrationRequestLog.STATUS_CONFIG_ERROR],
        )
        .filter(service__in=[
            IntegrationRequestLog.SERVICE_E_INVOICE,
            IntegrationRequestLog.SERVICE_E_WAY_BILL,
            IntegrationRequestLog.SERVICE_GST_RETURN,
            IntegrationRequestLog.SERVICE_TRACES,
        ])
        .select_related("company", "voucher", "requested_by")
        .order_by("-created_at")[:100]
    )
    for log in logs:
        cockpit = None
        if log.service == IntegrationRequestLog.SERVICE_E_INVOICE:
            cockpit = "e_invoice"
        elif log.service == IntegrationRequestLog.SERVICE_E_WAY_BILL:
            cockpit = "e_way_bill"
        severity = "critical" if log.status == IntegrationRequestLog.STATUS_CONFIG_ERROR else "high"
        items.append(_item(
            category="Integration",
            company=log.company,
            severity=severity,
            status=log.get_status_display(),
            title=f"{log.get_service_display()} failure",
            description=log.error_message or f"{log.provider or 'Provider'} returned {log.response_code or 'an error'}.",
            action_label="Open Evidence",
            action_url=_integration_url(log.company, log.service, log.status, cockpit),
            count=1,
            owner=log.requested_by.email if log.requested_by else "",
            reference=log.voucher.number if log.voucher else str(log.request_id)[:12],
            created_at=log.created_at,
        ))


def _add_evidence_items(items, companies):
    rejected = (
        StatutoryExportLog.objects.filter(company__in=companies, status=StatutoryExportLog.STATUS_REJECTED)
        .select_related("company", "generated_by")
        .order_by("-created_at")[:100]
    )
    for export in rejected:
        items.append(_item(
            category="Statutory Evidence",
            company=export.company,
            severity="high",
            status=export.get_status_display(),
            title=f"{export.get_export_type_display()} rejected",
            description=f"{export.file_name} needs regeneration or portal correction.",
            action_label="Open Evidence",
            action_url=_integration_url(export.company),
            count=export.row_count or 1,
            due_date=export.period_end,
            owner=export.generated_by.email if export.generated_by else "",
            reference=export.file_sha256[:16],
            created_at=export.created_at,
        ))


def build_ca_approval_inbox(user, params=None):
    params = params or {}
    today = timezone.localdate()
    companies = list(_accessible_companies(user))
    items = []

    _add_migration_items(items, companies)
    _add_filing_review_items(items, companies)
    _add_compliance_filing_items(items, companies, today)
    _add_integration_items(items, companies)
    _add_evidence_items(items, companies)

    category_filter = (params.get("category") or "all").strip() or "all"
    severity_filter = (params.get("severity") or "all").strip() or "all"
    company_filter = (params.get("company") or "all").strip() or "all"
    q = (params.get("q") or "").strip().lower()

    filtered = items
    if category_filter != "all":
        filtered = [item for item in filtered if item["category"] == category_filter]
    if severity_filter != "all":
        filtered = [item for item in filtered if item["severity"] == severity_filter]
    if company_filter != "all" and company_filter.isdigit():
        filtered = [item for item in filtered if item["company"].pk == int(company_filter)]
    if q:
        filtered = [
            item for item in filtered
            if q in " ".join([
                item["title"],
                item["description"],
                item["company"].name,
                item.get("reference") or "",
                item.get("owner") or "",
            ]).lower()
        ]

    filtered.sort(key=lambda item: (
        item["severity_rank"],
        item["due_date"] or date.max,
        -(item.get("count") or 0),
        item["company"].name.lower(),
        item["title"].lower(),
    ))

    categories = sorted({item["category"] for item in items})
    totals = {
        "total": len(items),
        "visible": len(filtered),
        "critical": sum(1 for item in items if item["severity"] == "critical"),
        "high": sum(1 for item in items if item["severity"] == "high"),
        "warning": sum(1 for item in items if item["severity"] == "warning"),
        "info": sum(1 for item in items if item["severity"] == "info"),
        "migration": sum(1 for item in items if item["category"] == "Tally Migration"),
        "filings": sum(1 for item in items if item["category"] in {"Filing Review", "Compliance Filing"}),
        "integrations": sum(1 for item in items if item["category"] == "Integration"),
        "evidence": sum(1 for item in items if item["category"] == "Statutory Evidence"),
    }

    return {
        "items": filtered,
        "all_items": items,
        "totals": totals,
        "companies": companies,
        "categories": categories,
        "category_filter": category_filter,
        "severity_filter": severity_filter,
        "company_filter": company_filter,
        "q": params.get("q", ""),
        "today": today,
        "export_query": _export_query(params),
        "title": "CA Approval Inbox",
    }


def _export_query(params):
    query = {}
    for key in ("category", "severity", "company", "q"):
        value = params.get(key)
        if value:
            query[key] = value
    query["export"] = "csv"
    return urlencode(query)


def approval_inbox_csv_response(items, today):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="ca-approval-inbox-{today:%Y%m%d}.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "Category",
        "Severity",
        "Status",
        "Title",
        "Description",
        "Count",
        "Due Date",
        "Owner",
        "Reference",
        "Action",
    ])
    for item in items:
        writer.writerow([
            item["company"].name,
            item["category"],
            item["severity"],
            item["status"],
            item["title"],
            item["description"],
            item["count"],
            item["due_date"].isoformat() if item["due_date"] else "",
            item.get("owner", ""),
            item.get("reference", ""),
            item["action_label"],
        ])
    return response
