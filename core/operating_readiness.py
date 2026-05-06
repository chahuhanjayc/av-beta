import csv
from datetime import timedelta
from urllib.parse import urlencode

from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from integrations.models import IntegrationConnector
from ledger.models import Ledger
from migration.models import ImportSession
from vouchers.models import Voucher

from .models import (
    AuditLog,
    Company,
    CompanyStatutoryProfile,
    ComplianceFiling,
    PracticeTask,
    UserCompanyAccess,
)


REFERENCE_PREFIX = "OPREADY"
SEVERITY_WEIGHT = {"critical": 18, "warning": 8, "info": 3}
SEVERITY_PRIORITY = {
    "critical": PracticeTask.PRIORITY_CRITICAL,
    "warning": PracticeTask.PRIORITY_HIGH,
    "info": PracticeTask.PRIORITY_NORMAL,
}
TASK_TYPE_BY_CATEGORY = {
    "gst": PracticeTask.TYPE_GST,
    "tds": PracticeTask.TYPE_TDS,
    "client": PracticeTask.TYPE_DOCUMENT,
    "migration": PracticeTask.TYPE_OTHER,
    "integration": PracticeTask.TYPE_OTHER,
    "control": PracticeTask.TYPE_OTHER,
    "workflow": PracticeTask.TYPE_OTHER,
}


def _companies_for_user(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return (
        Company.objects.filter(user_access__user=user)
        .distinct()
        .order_by("name")
    )


def _can_manage_company(user, company):
    if user.is_superuser:
        return True
    return UserCompanyAccess.objects.filter(
        user=user,
        company=company,
        role__in=["Admin", "Accountant"],
    ).exists()


def _statutory_profile(company):
    try:
        return company.statutory_profile, True
    except CompanyStatutoryProfile.DoesNotExist:
        return CompanyStatutoryProfile(company=company), False


def _company_scoped_url(company, url_name, *args, query=None):
    target = reverse(url_name, args=args)
    if query:
        target = f"{target}?{urlencode(query)}"
    return f"{reverse('core:switch_company', args=[company.pk])}?{urlencode({'next': target})}"


def _connector(connectors, connector_type):
    return connectors.get(connector_type)


def _connector_ready(connectors, connector_type):
    connector = _connector(connectors, connector_type)
    return bool(connector and connector.is_ready), connector


def _connector_detail(connector, fallback):
    if not connector:
        return fallback
    return f"{connector.label}: {connector.get_status_display()} / {connector.get_mode_display()}"


def _active_subscription(company):
    now = timezone.now()
    return ClientSubscription.objects.filter(
        company=company,
        status=ClientSubscription.STATUS_ACTIVE,
        subscription_end__gte=now,
    ).exists()


def _latest_import(company):
    return ImportSession.objects.filter(company=company).order_by("-created_at").first()


def _latest_import_is_approved(import_session):
    if not import_session:
        return False
    return (
        import_session.status == "confirmed"
        or import_session.approval_status == ImportSession.APPROVAL_APPROVED
    )


def _readiness_band(score):
    if score >= 90:
        return "Ready"
    if score >= 75:
        return "Almost Ready"
    if score >= 60:
        return "Needs Work"
    return "Blocked"


def _badge_class(score):
    if score >= 90:
        return "bg-success"
    if score >= 75:
        return "bg-primary"
    if score >= 60:
        return "bg-warning text-dark"
    return "bg-danger"


def _gap_sort_key(check):
    severity_rank = {"critical": 0, "warning": 1, "info": 2}.get(check["severity"], 3)
    return severity_rank, check["category"], check["title"]


def build_company_operating_readiness(company, user, today=None):
    today = today or timezone.localdate()
    profile, profile_saved = _statutory_profile(company)
    connectors = {
        connector.connector_type: connector
        for connector in IntegrationConnector.objects.filter(company=company)
    }
    latest_import = _latest_import(company)
    ledger_count = Ledger.objects.filter(company=company).count()
    voucher_count = Voucher.objects.filter(company=company).count()
    upcoming_cutoff = today + timedelta(days=90)
    has_upcoming_workflows = ComplianceFiling.objects.filter(
        company=company,
        due_date__gte=today,
        due_date__lte=upcoming_cutoff,
    ).exists()
    checks = []

    def add_check(code, title, passed, *, severity, category, detail, action_label, action_url):
        checks.append({
            "code": code,
            "reference": f"{REFERENCE_PREFIX}:{company.pk}:{code}",
            "title": title,
            "passed": bool(passed),
            "severity": severity,
            "category": category,
            "detail": detail,
            "action_label": action_label,
            "action_url": action_url,
        })

    settings_url = _company_scoped_url(company, "core:app_settings")
    company_settings_url = _company_scoped_url(company, "core:company_settings")
    integrations_url = _company_scoped_url(company, "integrations:dashboard")
    migration_url = _company_scoped_url(company, "migration:upload")
    calendar_url = _company_scoped_url(company, "core:compliance_calendar")
    audit_url = _company_scoped_url(company, "core:audit_log")

    add_check(
        "statutory_profile",
        "Statutory profile saved",
        profile_saved,
        severity="warning",
        category="control",
        detail="Client-specific GST, TDS, MSME and due-date rules are saved.",
        action_label="Open App Settings",
        action_url=settings_url,
    )
    add_check(
        "company_identity",
        "Company identity configured",
        bool(company.short_code and company.financial_year_start),
        severity="warning",
        category="client",
        detail="Voucher prefix and financial year start are required for clean migration and numbering.",
        action_label="Open Company Settings",
        action_url=company_settings_url,
    )
    add_check(
        "client_subscription",
        "Client subscription active",
        _active_subscription(company),
        severity="warning",
        category="client",
        detail="Client workspace is active for portal access, reminders, and support tracking.",
        action_label="Open Client 360",
        action_url=reverse("core:client_360", args=[company.pk]),
    )
    add_check(
        "whatsapp_intake",
        "WhatsApp intake configured",
        bool(company.whatsapp_intake_number),
        severity="warning",
        category="client",
        detail="Client document intake number is configured for bill and evidence capture.",
        action_label="Open App Settings",
        action_url=settings_url,
    )
    add_check(
        "invoice_email",
        "Invoice email sender configured",
        bool(company.invoice_email_from_address or company.invoice_email_reply_to),
        severity="info",
        category="client",
        detail="Single-click invoice email has a company sender or reply-to address.",
        action_label="Open App Settings",
        action_url=settings_url,
    )

    if profile.gst_registered:
        gst_ready, gst_connector = _connector_ready(connectors, IntegrationConnector.TYPE_GST)
        eway_ready, eway_connector = _connector_ready(connectors, IntegrationConnector.TYPE_EWAY)
        add_check(
            "gstin",
            "GSTIN available",
            bool(company.gstin),
            severity="critical",
            category="gst",
            detail="GST-registered clients need GSTIN for filing packs, GST portal workflows, and invoice validation.",
            action_label="Open Company Settings",
            action_url=company_settings_url,
        )
        add_check(
            "gst_connector",
            "GST connector ready",
            gst_ready,
            severity="warning",
            category="integration",
            detail=_connector_detail(gst_connector, "GST connector is missing or not ready."),
            action_label="Open Integrations",
            action_url=integrations_url,
        )
        add_check(
            "eway_connector",
            "E-way connector ready",
            eway_ready,
            severity="info",
            category="integration",
            detail=_connector_detail(eway_connector, "E-way connector is missing or not ready."),
            action_label="Open Integrations",
            action_url=integrations_url,
        )
        if company.e_invoice_enabled:
            irp_ready, irp_connector = _connector_ready(connectors, IntegrationConnector.TYPE_IRP)
            add_check(
                "irp_connector",
                "IRP connector ready",
                irp_ready,
                severity="warning",
                category="integration",
                detail=_connector_detail(irp_connector, "IRP/e-invoice connector is missing or not ready."),
                action_label="Open Integrations",
                action_url=integrations_url,
            )

    if profile.tds_applicable:
        traces_ready, traces_connector = _connector_ready(connectors, IntegrationConnector.TYPE_TRACES)
        add_check(
            "tan",
            "TAN available",
            bool(company.tan),
            severity="critical",
            category="tds",
            detail="TDS-enabled clients need TAN for TRACES, challan, Form 16/16A and return workflows.",
            action_label="Open Company Settings",
            action_url=company_settings_url,
        )
        add_check(
            "tds_forms",
            "TDS return forms selected",
            any([profile.tds_24q_enabled, profile.tds_26q_enabled, profile.tds_27q_enabled]),
            severity="critical",
            category="tds",
            detail="At least one quarterly TDS form must be enabled for statutory planning.",
            action_label="Open App Settings",
            action_url=settings_url,
        )
        add_check(
            "traces_connector",
            "TRACES connector ready",
            traces_ready,
            severity="warning",
            category="integration",
            detail=_connector_detail(traces_connector, "TRACES connector is missing or not ready."),
            action_label="Open Integrations",
            action_url=integrations_url,
        )

    tally_ready, tally_connector = _connector_ready(connectors, IntegrationConnector.TYPE_TALLY)
    add_check(
        "tally_sync",
        "Tally sync path ready",
        tally_ready,
        severity="warning",
        category="migration",
        detail=_connector_detail(tally_connector, "Tally sync/import connector is missing or not ready."),
        action_label="Open Integrations",
        action_url=integrations_url,
    )
    add_check(
        "migration_evidence",
        "Migration evidence approved",
        _latest_import_is_approved(latest_import) or (ledger_count > 5 and voucher_count > 0),
        severity="warning",
        category="migration",
        detail=(
            f"Latest import session #{latest_import.pk}: {latest_import.get_status_display()}"
            if latest_import else "No approved import session or meaningful migrated data found."
        ),
        action_label="Open Migration",
        action_url=migration_url,
    )
    add_check(
        "upcoming_workflows",
        "Upcoming filing workflows prepared",
        has_upcoming_workflows,
        severity="critical",
        category="workflow",
        detail="GST/TDS compliance filings exist for the next 90 days.",
        action_label="Open Calendar",
        action_url=calendar_url,
    )
    add_check(
        "audit_activity",
        "Audit trail active",
        AuditLog.objects.filter(company=company).exists(),
        severity="info",
        category="control",
        detail="At least one company-scoped audit event exists for accountability.",
        action_label="Open Audit Trail",
        action_url=audit_url,
    )

    failed = [check for check in checks if not check["passed"]]
    penalty = sum(SEVERITY_WEIGHT[check["severity"]] for check in failed)
    score = max(0, min(100, 100 - penalty))
    top_gaps = sorted(failed, key=_gap_sort_key)[:5]
    critical_count = sum(1 for check in failed if check["severity"] == "critical")
    warning_count = sum(1 for check in failed if check["severity"] == "warning")
    info_count = sum(1 for check in failed if check["severity"] == "info")

    return {
        "company": company,
        "score": score,
        "band": _readiness_band(score),
        "badge_class": _badge_class(score),
        "checks": checks,
        "failed_checks": failed,
        "top_gaps": top_gaps,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "info_count": info_count,
        "passed_count": len(checks) - len(failed),
        "total_checks": len(checks),
        "ledger_count": ledger_count,
        "voucher_count": voucher_count,
        "latest_import": latest_import,
        "connector_ready_count": sum(1 for connector in connectors.values() if connector.is_ready),
        "connector_count": len(connectors),
        "can_manage": _can_manage_company(user, company),
    }


def build_operating_readiness(user, params=None):
    params = params or {}
    rows = [
        build_company_operating_readiness(company, user)
        for company in _companies_for_user(user)
    ]
    q = (params.get("q") or "").strip().lower()
    band_filter = (params.get("band") or "all").strip()

    if q:
        rows = [
            row for row in rows
            if q in row["company"].name.lower() or q in (row["company"].gstin or "").lower()
        ]
    if band_filter != "all":
        rows = [row for row in rows if row["band"].lower().replace(" ", "_") == band_filter]

    rows.sort(key=lambda row: (row["score"], -row["critical_count"], row["company"].name))
    totals = {
        "clients": len(rows),
        "avg_score": round(sum(row["score"] for row in rows) / len(rows)) if rows else 0,
        "ready": sum(1 for row in rows if row["band"] == "Ready"),
        "almost_ready": sum(1 for row in rows if row["band"] == "Almost Ready"),
        "needs_work": sum(1 for row in rows if row["band"] == "Needs Work"),
        "blocked": sum(1 for row in rows if row["band"] == "Blocked"),
        "critical_gaps": sum(row["critical_count"] for row in rows),
        "warning_gaps": sum(row["warning_count"] for row in rows),
        "manageable_clients": sum(1 for row in rows if row["can_manage"]),
    }
    return {
        "rows": rows,
        "totals": totals,
        "q": q,
        "band_filter": band_filter,
        "band_options": [
            ("all", "All Clients"),
            ("blocked", "Blocked"),
            ("needs_work", "Needs Work"),
            ("almost_ready", "Almost Ready"),
            ("ready", "Ready"),
        ],
        "export_query": urlencode({key: value for key, value in {"q": q, "band": band_filter}.items() if value and value != "all"}),
    }


def create_operating_readiness_tasks(rows, user):
    today = timezone.localdate()
    created = 0
    existing = 0
    skipped = 0

    for row in rows:
        if not row["can_manage"]:
            skipped += len([check for check in row["failed_checks"] if check["severity"] != "info"])
            continue
        for check in row["failed_checks"]:
            if check["severity"] == "info":
                continue
            existing_task = PracticeTask.objects.filter(
                company=row["company"],
                reference=check["reference"],
            ).exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED]).first()
            if existing_task:
                existing += 1
                continue
            due_days = 3 if check["severity"] == "critical" else 7
            PracticeTask.objects.create(
                company=row["company"],
                title=f"Operating readiness: {check['title']}",
                task_type=TASK_TYPE_BY_CATEGORY.get(check["category"], PracticeTask.TYPE_OTHER),
                priority=SEVERITY_PRIORITY[check["severity"]],
                status=PracticeTask.STATUS_OPEN,
                due_date=today + timedelta(days=due_days),
                assigned_to=user,
                created_by=user,
                reference=check["reference"],
                description=f"{check['detail']}\n\nAction: {check['action_label']} - {check['action_url']}",
            )
            created += 1

    return {"created": created, "existing": existing, "skipped": skipped}


def operating_readiness_csv_response(rows):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="client-operating-readiness.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "GSTIN",
        "Score",
        "Band",
        "Critical Gaps",
        "Warning Gaps",
        "Top Gaps",
        "Ledgers",
        "Vouchers",
        "Ready Connectors",
        "Total Connectors",
    ])
    for row in rows:
        writer.writerow([
            row["company"].name,
            row["company"].gstin or "",
            row["score"],
            row["band"],
            row["critical_count"],
            row["warning_count"],
            "; ".join(check["title"] for check in row["top_gaps"]),
            row["ledger_count"],
            row["voucher_count"],
            row["connector_ready_count"],
            row["connector_count"],
        ])
    return response
