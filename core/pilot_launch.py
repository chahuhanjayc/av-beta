import csv
from datetime import timedelta
from urllib.parse import urlencode

from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from portal.models import ClientDocumentRequest, PortalUser

from .models import AuditLog, ClientEngagement, Company, PracticeTask, UserCompanyAccess
from .operating_readiness import build_company_operating_readiness


PILOT_LAUNCH_TASK_PREFIX = "PILOTLAUNCH:"
SEVERITY_WEIGHT = {"critical": 20, "warning": 8, "info": 3}
SEVERITY_PRIORITY = {
    "critical": PracticeTask.PRIORITY_CRITICAL,
    "warning": PracticeTask.PRIORITY_HIGH,
    "info": PracticeTask.PRIORITY_NORMAL,
}


def build_pilot_launch_control(user, params=None):
    params = params or {}
    today = timezone.localdate()
    companies = list(_companies_for_user(user))
    subscriptions = {
        item.company_id: item
        for item in ClientSubscription.objects.filter(company__in=companies).select_related("primary_user")
    }
    engagements = {
        item.company_id: item
        for item in ClientEngagement.objects.filter(company__in=companies).select_related("partner_owner", "manager_owner")
    }
    rows = [
        build_company_pilot_launch_row(
            company,
            user,
            today=today,
            subscription=subscriptions.get(company.pk),
            engagement=engagements.get(company.pk),
        )
        for company in companies
    ]

    q = (params.get("q") or "").strip().lower()
    band_filter = (params.get("band") or "all").strip()
    if q:
        rows = [
            row for row in rows
            if q in row["company"].name.lower() or q in (row["company"].gstin or "").lower()
        ]
    if band_filter != "all":
        rows = [row for row in rows if row["band_key"] == band_filter]

    rows.sort(key=lambda row: (
        row["sort_rank"],
        row["score"],
        -row["critical_count"],
        -row["warning_count"],
        row["company"].name,
    ))
    totals = {
        "clients": len(rows),
        "avg_score": round(sum(row["score"] for row in rows) / len(rows)) if rows else 0,
        "pilot_ready": sum(1 for row in rows if row["band_key"] == "pilot_ready"),
        "launch_watch": sum(1 for row in rows if row["band_key"] == "launch_watch"),
        "setup_needed": sum(1 for row in rows if row["band_key"] == "setup_needed"),
        "blocked": sum(1 for row in rows if row["band_key"] == "blocked"),
        "critical_gates": sum(row["critical_count"] for row in rows),
        "warning_gates": sum(row["warning_count"] for row in rows),
        "overdue_documents": sum(row["overdue_document_requests"] for row in rows),
        "portal_users": sum(row["portal_user_count"] for row in rows),
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
            ("setup_needed", "Setup Needed"),
            ("launch_watch", "Launch Watch"),
            ("pilot_ready", "Pilot Ready"),
        ],
        "export_query": urlencode({key: value for key, value in {"q": q, "band": band_filter}.items() if value and value != "all"}),
    }


def build_company_pilot_launch_row(company, user, *, today=None, subscription=None, engagement=None):
    today = today or timezone.localdate()
    operating = build_company_operating_readiness(company, user, today=today)
    access_count = UserCompanyAccess.objects.filter(company=company).count()
    manage_access_count = UserCompanyAccess.objects.filter(company=company, role__in=["Admin", "Accountant"]).count()
    portal_user_count = PortalUser.objects.filter(linked_ledger__company=company, is_active=True).distinct().count()
    overdue_documents = ClientDocumentRequest.objects.filter(
        company=company,
        status=ClientDocumentRequest.STATUS_OPEN,
        due_date__lt=today,
    ).count()
    uploaded_documents = ClientDocumentRequest.objects.filter(
        company=company,
        status=ClientDocumentRequest.STATUS_UPLOADED,
    ).count()
    open_tasks = PracticeTask.objects.filter(company=company).exclude(
        status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED]
    )
    open_task_count = open_tasks.count()
    overdue_task_count = open_tasks.filter(due_date__lt=today).count()
    launch_task_count = open_tasks.filter(reference__startswith=PILOT_LAUNCH_TASK_PREFIX).count()

    gates = []

    def add_gate(code, title, passed, *, severity, detail, action_label, action_url):
        gates.append({
            "code": code,
            "reference": f"{PILOT_LAUNCH_TASK_PREFIX}{company.pk}:{code}",
            "title": title,
            "passed": bool(passed),
            "severity": severity,
            "detail": detail,
            "action_label": action_label,
            "action_url": action_url,
        })

    app_settings_url = _company_scoped_url(company, "core:app_settings")
    company_settings_url = _company_scoped_url(company, "core:company_settings")
    operating_url = _company_scoped_url(company, "core:client_operating_readiness")
    engagement_url = reverse("core:client_engagement_update", args=[company.pk])
    client_360_url = reverse("core:client_360", args=[company.pk])
    portal_url = f"{reverse('portal:client_requests')}?{urlencode({'company': company.pk})}"
    tasks_url = f"{reverse('core:practice_tasks')}?{urlencode({'company': company.pk, 'status': 'open'})}"

    subscription_live = bool(
        subscription
        and subscription.status in {ClientSubscription.STATUS_ACTIVE, ClientSubscription.STATUS_TRIAL}
        and subscription.subscription_end.date() >= today
    )
    engagement_live = bool(
        engagement and engagement.status in {ClientEngagement.STATUS_ACTIVE, ClientEngagement.STATUS_ONBOARDING}
    )
    owner_assigned = bool(engagement and (engagement.partner_owner_id or engagement.manager_owner_id))
    scope_documented = bool(engagement and engagement.scope_summary.strip())
    communication_ready = bool(company.whatsapp_intake_number and (company.invoice_email_from_address or company.invoice_email_reply_to))

    add_gate(
        "operating_readiness",
        "Operating readiness above launch floor",
        operating["score"] >= 75 and operating["critical_count"] == 0,
        severity="critical",
        detail=f"Operating readiness is {operating['score']}% with {operating['critical_count']} critical gap(s).",
        action_label="Open Operating Readiness",
        action_url=operating_url,
    )
    add_gate(
        "data_baseline",
        "Books data baseline loaded",
        operating["ledger_count"] > 5 and operating["voucher_count"] > 0,
        severity="critical",
        detail=f"{operating['ledger_count']} ledger(s), {operating['voucher_count']} voucher(s) found.",
        action_label="Open Company 360",
        action_url=client_360_url,
    )
    add_gate(
        "subscription_live",
        "Subscription or trial active",
        subscription_live,
        severity="critical",
        detail="Client workspace must be active before inviting users and running pilot support.",
        action_label="Open Client 360",
        action_url=client_360_url,
    )
    add_gate(
        "engagement_live",
        "Engagement live or onboarding",
        engagement_live,
        severity="critical",
        detail="Commercial scope must be active or in onboarding before pilot launch.",
        action_label="Open Engagement",
        action_url=engagement_url,
    )
    add_gate(
        "scope_documented",
        "Scope documented",
        scope_documented,
        severity="warning",
        detail="Scope summary should define modules, filings, migration limits, and support ownership.",
        action_label="Open Engagement",
        action_url=engagement_url,
    )
    add_gate(
        "owner_assigned",
        "Partner or manager owner assigned",
        owner_assigned,
        severity="warning",
        detail="Pilot needs a named CA owner for unblock decisions and client escalation.",
        action_label="Open Engagement",
        action_url=engagement_url,
    )
    add_gate(
        "team_access",
        "CA team access configured",
        manage_access_count >= 1 and access_count >= 1,
        severity="warning",
        detail=f"{access_count} user access record(s), {manage_access_count} admin/accountant record(s).",
        action_label="Open Security Control",
        action_url=_company_scoped_url(company, "core:security_control"),
    )
    add_gate(
        "portal_contacts",
        "Client portal contacts created",
        portal_user_count >= 1,
        severity="warning",
        detail=f"{portal_user_count} active portal contact(s) linked to client ledgers.",
        action_label="Open Client Requests",
        action_url=portal_url,
    )
    add_gate(
        "communication_ready",
        "Client communication channels ready",
        communication_ready,
        severity="warning",
        detail="WhatsApp intake and invoice email sender/reply-to should both be configured.",
        action_label="Open App Settings",
        action_url=app_settings_url,
    )
    add_gate(
        "company_identity",
        "Company identity complete",
        bool(company.short_code and company.financial_year_start),
        severity="warning",
        detail="Short code and financial year start are needed for pilot support and clean numbering.",
        action_label="Open Company Settings",
        action_url=company_settings_url,
    )
    add_gate(
        "client_chase_clear",
        "Client document chase not overdue",
        overdue_documents == 0,
        severity="warning",
        detail=f"{overdue_documents} overdue document request(s), {uploaded_documents} uploaded awaiting review.",
        action_label="Open Request Reminders",
        action_url=f"{reverse('portal:client_request_reminders')}?{urlencode({'company': company.pk})}",
    )
    add_gate(
        "open_work_under_control",
        "Open work under control",
        overdue_task_count == 0 and open_task_count <= 12,
        severity="info",
        detail=f"{open_task_count} open task(s), {overdue_task_count} overdue task(s), {launch_task_count} launch task(s).",
        action_label="Open Work Queue",
        action_url=tasks_url,
    )

    failed = [gate for gate in gates if not gate["passed"]]
    critical_count = sum(1 for gate in failed if gate["severity"] == "critical")
    warning_count = sum(1 for gate in failed if gate["severity"] == "warning")
    penalty = sum(SEVERITY_WEIGHT[gate["severity"]] for gate in failed)
    score = max(0, min(100, 100 - penalty))
    band_key, band_label, badge_class, sort_rank = _launch_band(score, critical_count, warning_count)

    return {
        "company": company,
        "subscription": subscription,
        "engagement": engagement,
        "operating": operating,
        "score": score,
        "band": band_label,
        "band_key": band_key,
        "badge_class": badge_class,
        "sort_rank": sort_rank,
        "gates": gates,
        "failed_gates": failed,
        "top_gates": sorted(failed, key=_gate_sort_key)[:5],
        "critical_count": critical_count,
        "warning_count": warning_count,
        "passed_count": len(gates) - len(failed),
        "total_gates": len(gates),
        "access_count": access_count,
        "manage_access_count": manage_access_count,
        "portal_user_count": portal_user_count,
        "overdue_document_requests": overdue_documents,
        "uploaded_document_requests": uploaded_documents,
        "open_task_count": open_task_count,
        "overdue_task_count": overdue_task_count,
        "launch_task_count": launch_task_count,
        "can_manage": _can_manage_company(user, company),
    }


def create_pilot_launch_tasks(rows, user):
    today = timezone.localdate()
    created = 0
    existing = 0
    skipped = 0
    for row in rows:
        if not row["can_manage"]:
            skipped += sum(1 for gate in row["failed_gates"] if gate["severity"] != "info")
            continue
        for gate in row["failed_gates"]:
            if gate["severity"] == "info":
                continue
            task = PracticeTask.objects.filter(
                company=row["company"],
                reference=gate["reference"],
            ).exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED]).first()
            if task:
                existing += 1
                continue
            task = PracticeTask.objects.create(
                company=row["company"],
                title=f"Pilot launch: {gate['title']}",
                task_type=PracticeTask.TYPE_DOCUMENT if "portal" in gate["code"] or "chase" in gate["code"] else PracticeTask.TYPE_OTHER,
                priority=SEVERITY_PRIORITY[gate["severity"]],
                status=PracticeTask.STATUS_OPEN,
                due_date=today + timedelta(days=2 if gate["severity"] == "critical" else 5),
                assigned_to=user,
                created_by=user,
                reference=gate["reference"],
                description=f"{gate['detail']}\n\nAction: {gate['action_label']} - {gate['action_url']}",
            )
            created += 1
            AuditLog.objects.create(
                company=row["company"],
                user=user if getattr(user, "is_authenticated", False) else None,
                action=AuditLog.ACTION_CREATE,
                model_name="PracticeTask",
                record_id=task.pk,
                object_repr=task.title[:200],
                old_data={},
                new_data={
                    "source": "pilot_launch_control",
                    "reference": gate["reference"],
                    "gate": gate["code"],
                    "severity": gate["severity"],
                    "launch_score": row["score"],
                },
            )
    return {"created": created, "existing": existing, "skipped": skipped}


def pilot_launch_csv_response(rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="pilot-launch-control.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "GSTIN",
        "Launch Score",
        "Launch Band",
        "Operating Score",
        "Critical Gates",
        "Warning Gates",
        "Top Gates",
        "Subscription",
        "Engagement",
        "Portal Users",
        "Overdue Documents",
        "Open Tasks",
        "Overdue Tasks",
    ])
    for row in rows:
        writer.writerow([
            row["company"].name,
            row["company"].gstin or "",
            row["score"],
            row["band"],
            row["operating"]["score"],
            row["critical_count"],
            row["warning_count"],
            "; ".join(gate["title"] for gate in row["top_gates"]),
            row["subscription"].status if row["subscription"] else "missing",
            row["engagement"].status if row["engagement"] else "missing",
            row["portal_user_count"],
            row["overdue_document_requests"],
            row["open_task_count"],
            row["overdue_task_count"],
        ])
    return response


def _companies_for_user(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return Company.objects.filter(user_access__user=user).distinct().order_by("name")


def _can_manage_company(user, company):
    if user.is_superuser:
        return True
    return UserCompanyAccess.objects.filter(
        user=user,
        company=company,
        role__in=["Admin", "Accountant"],
    ).exists()


def _company_scoped_url(company, url_name, *args):
    target = reverse(url_name, args=args)
    return f"{reverse('core:switch_company', args=[company.pk])}?{urlencode({'next': target})}"


def _launch_band(score, critical_count, warning_count):
    if critical_count:
        return "blocked", "Blocked", "bg-danger", 0
    if score >= 90 and warning_count == 0:
        return "pilot_ready", "Pilot Ready", "bg-success", 3
    if score >= 75:
        return "launch_watch", "Launch Watch", "bg-primary", 2
    if score >= 60:
        return "setup_needed", "Setup Needed", "bg-warning text-dark", 1
    return "blocked", "Blocked", "bg-danger", 0


def _gate_sort_key(gate):
    return (
        {"critical": 0, "warning": 1, "info": 2}.get(gate["severity"], 3),
        gate["title"],
    )
