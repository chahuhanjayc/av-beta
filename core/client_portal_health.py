import csv
from datetime import timedelta
from urllib.parse import urlencode

from django.db.models import Q
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from ledger.models import Ledger
from portal.models import BalanceConfirmation, ClientDocumentRequest, PortalUser

from .models import Company, PracticeTask, UserCompanyAccess


CLIENT_PORTAL_HEALTH_TASK_PREFIX = "PORTALHEALTH:"
SEVERITY_WEIGHT = {"critical": 24, "warning": 10, "info": 4}
SEVERITY_PRIORITY = {
    "critical": PracticeTask.PRIORITY_CRITICAL,
    "warning": PracticeTask.PRIORITY_HIGH,
    "info": PracticeTask.PRIORITY_NORMAL,
}


def build_client_portal_health(user, params=None):
    params = params or {}
    today = timezone.localdate()
    rows = [
        build_company_portal_health_row(company, user, today=today)
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
        rows = [row for row in rows if row["band_key"] == band_filter]

    rows.sort(key=lambda row: (
        row["sort_rank"],
        row["score"],
        -row["critical_count"],
        -row["overdue_request_count"],
        row["company"].name,
    ))
    totals = {
        "clients": len(rows),
        "avg_score": round(sum(row["score"] for row in rows) / len(rows)) if rows else 0,
        "ready": sum(1 for row in rows if row["band_key"] == "ready"),
        "watch": sum(1 for row in rows if row["band_key"] == "watch"),
        "needs_chase": sum(1 for row in rows if row["band_key"] == "needs_chase"),
        "disconnected": sum(1 for row in rows if row["band_key"] == "disconnected"),
        "portal_users": sum(row["active_portal_user_count"] for row in rows),
        "email_contacts": sum(row["email_contact_count"] for row in rows),
        "whatsapp_contacts": sum(row["whatsapp_contact_count"] for row in rows),
        "open_requests": sum(row["open_request_count"] for row in rows),
        "overdue_requests": sum(row["overdue_request_count"] for row in rows),
        "stale_uploaded": sum(row["stale_uploaded_count"] for row in rows),
        "missing_delivery": sum(row["missing_delivery_count"] for row in rows),
        "manageable_clients": sum(1 for row in rows if row["can_manage"]),
    }
    return {
        "rows": rows,
        "totals": totals,
        "q": q,
        "band_filter": band_filter,
        "band_options": [
            ("all", "All Clients"),
            ("disconnected", "Disconnected"),
            ("needs_chase", "Needs Chase"),
            ("watch", "Watch"),
            ("ready", "Ready"),
        ],
        "export_query": urlencode({key: value for key, value in {"q": q, "band": band_filter}.items() if value and value != "all"}),
    }


def build_company_portal_health_row(company, user, *, today=None):
    today = today or timezone.localdate()
    now = timezone.now()
    last_30 = today - timedelta(days=30)
    reminder_cutoff = now - timedelta(days=7)
    stale_upload_cutoff = now - timedelta(days=2)

    portal_users = list(
        PortalUser.objects.filter(linked_ledger__company=company)
        .select_related("linked_ledger", "linked_ledger__account_group")
        .order_by("-is_active", "name")
    )
    active_portal_users = [contact for contact in portal_users if contact.is_active]
    contact_previews = [_portal_contact_payload(contact) for contact in portal_users[:5]]
    email_contact_count = sum(1 for contact in active_portal_users if _portal_contact_email(contact))
    whatsapp_contact_count = sum(1 for contact in active_portal_users if _portal_contact_whatsapp(contact))
    inactive_portal_user_count = len(portal_users) - len(active_portal_users)

    ledger_contact_q = (Q(email__isnull=False) & ~Q(email="")) | (Q(whatsapp_number__isnull=False) & ~Q(whatsapp_number=""))
    ledger_contacts = Ledger.objects.filter(company=company, is_active=True).filter(ledger_contact_q).distinct()
    ledger_contact_count = ledger_contacts.count()
    uninvited_contact_count = ledger_contacts.filter(portal_users__isnull=True).count()

    requests = ClientDocumentRequest.objects.filter(company=company).select_related(
        "portal_user",
        "portal_user__linked_ledger",
        "related_task",
    )
    active_requests = requests.exclude(
        status__in=[ClientDocumentRequest.STATUS_CLOSED, ClientDocumentRequest.STATUS_CANCELLED]
    )
    open_requests = active_requests.filter(status=ClientDocumentRequest.STATUS_OPEN)
    uploaded_requests = active_requests.filter(status=ClientDocumentRequest.STATUS_UPLOADED)
    open_request_count = open_requests.count()
    uploaded_request_count = uploaded_requests.count()
    overdue_request_count = open_requests.filter(due_date__lt=today).count()
    stale_uploaded_count = uploaded_requests.filter(
        Q(uploaded_at__isnull=True) | Q(uploaded_at__lt=stale_upload_cutoff)
    ).count()
    missing_delivery_q = (
        (Q(recipient_email__isnull=True) | Q(recipient_email=""))
        & (Q(recipient_whatsapp_number__isnull=True) | Q(recipient_whatsapp_number=""))
    )
    missing_token_q = Q(token__isnull=True) | Q(token="")
    missing_delivery_count = active_requests.filter(missing_token_q | missing_delivery_q).count()
    unreminded_overdue_count = open_requests.filter(
        due_date__lt=today,
    ).filter(
        Q(last_reminded_at__isnull=True) | Q(last_reminded_at__lt=reminder_cutoff)
    ).count()
    total_request_count = requests.count()
    closed_request_count = requests.filter(status=ClientDocumentRequest.STATUS_CLOSED).count()
    responded_request_count = requests.filter(
        status__in=[ClientDocumentRequest.STATUS_UPLOADED, ClientDocumentRequest.STATUS_CLOSED]
    ).count()
    response_rate = round((responded_request_count / total_request_count) * 100) if total_request_count else 0
    recent_upload = requests.filter(uploaded_at__date__gte=last_30).order_by("-uploaded_at").first()
    recent_confirmation = BalanceConfirmation.objects.filter(
        portal_user__linked_ledger__company=company,
        confirmed_at__date__gte=last_30,
    ).order_by("-confirmed_at").first()
    recent_activity = bool(recent_upload or recent_confirmation)
    journey_tested = responded_request_count > 0 or bool(recent_confirmation)

    gates = []

    def add_gate(code, title, passed, *, severity, detail, action_label, action_url):
        gates.append({
            "code": code,
            "reference": f"{CLIENT_PORTAL_HEALTH_TASK_PREFIX}{company.pk}:{code}",
            "title": title,
            "passed": bool(passed),
            "severity": severity,
            "detail": detail,
            "action_label": action_label,
            "action_url": action_url,
        })

    client_requests_url = f"{reverse('portal:client_requests')}?{urlencode({'company': company.pk, 'status': 'active'})}"
    create_request_url = f"{reverse('portal:client_request_create')}?{urlencode({'company': company.pk})}"
    reminders_url = f"{reverse('portal:client_request_reminders')}?{urlencode({'company': company.pk})}"
    client_360_url = reverse("core:client_360", args=[company.pk])
    app_settings_url = reverse("core:app_settings")

    add_gate(
        "active_portal_contact",
        "Active portal contact exists",
        len(active_portal_users) > 0,
        severity="critical",
        detail=f"{len(active_portal_users)} active portal contact(s); {inactive_portal_user_count} inactive.",
        action_label="Open Client 360",
        action_url=client_360_url,
    )
    add_gate(
        "email_delivery_ready",
        "Email delivery ready",
        bool(company.invoice_email_from_address and email_contact_count > 0),
        severity="critical",
        detail=f"Sender email {'configured' if company.invoice_email_from_address else 'missing'}; {email_contact_count} active contact email(s).",
        action_label="Open App Settings",
        action_url=app_settings_url,
    )
    add_gate(
        "request_links_deliverable",
        "Request links deliverable",
        missing_delivery_count == 0,
        severity="critical",
        detail=f"{missing_delivery_count} active request(s) have no recipient channel or upload token.",
        action_label="Open Client Requests",
        action_url=client_requests_url,
    )
    add_gate(
        "whatsapp_delivery_ready",
        "WhatsApp delivery ready",
        bool(company.whatsapp_intake_number and whatsapp_contact_count > 0),
        severity="warning",
        detail=f"Intake number {'configured' if company.whatsapp_intake_number else 'missing'}; {whatsapp_contact_count} active contact WhatsApp number(s).",
        action_label="Open App Settings",
        action_url=app_settings_url,
    )
    add_gate(
        "ledger_contacts_invited",
        "Ledger contacts invited",
        uninvited_contact_count == 0,
        severity="warning",
        detail=f"{uninvited_contact_count} ledger contact(s) have email/WhatsApp but no portal login.",
        action_label="Open Client 360",
        action_url=client_360_url,
    )
    add_gate(
        "overdue_chase_clean",
        "Overdue request chase clean",
        overdue_request_count == 0,
        severity="warning",
        detail=f"{overdue_request_count} open request(s) are overdue; {unreminded_overdue_count} need a fresh reminder.",
        action_label="Open Reminders",
        action_url=reminders_url,
    )
    add_gate(
        "uploaded_review_sla",
        "Uploaded documents reviewed",
        stale_uploaded_count == 0,
        severity="warning",
        detail=f"{uploaded_request_count} upload(s) await review; {stale_uploaded_count} are older than 2 days.",
        action_label="Open Client Requests",
        action_url=client_requests_url,
    )
    add_gate(
        "response_loop_proven",
        "Client response loop proven",
        journey_tested and (total_request_count < 3 or response_rate >= 50),
        severity="warning",
        detail=f"{responded_request_count}/{total_request_count} request(s) responded; recent portal activity {'yes' if recent_activity else 'no'}.",
        action_label="Create Test Request",
        action_url=create_request_url,
    )
    add_gate(
        "reminder_loop_fresh",
        "Reminder loop fresh",
        unreminded_overdue_count == 0,
        severity="info",
        detail=f"{unreminded_overdue_count} overdue request(s) have no reminder in the last 7 days.",
        action_label="Open Reminders",
        action_url=reminders_url,
    )

    failed = [gate for gate in gates if not gate["passed"]]
    critical_count = sum(1 for gate in failed if gate["severity"] == "critical")
    warning_count = sum(1 for gate in failed if gate["severity"] == "warning")
    penalty = sum(SEVERITY_WEIGHT[gate["severity"]] for gate in failed)
    score = max(0, min(100, 100 - penalty))
    band_key, band, badge_class, sort_rank = _portal_health_band(score, critical_count, warning_count)

    return {
        "company": company,
        "score": score,
        "band": band,
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
        "active_portal_user_count": len(active_portal_users),
        "inactive_portal_user_count": inactive_portal_user_count,
        "contact_previews": contact_previews,
        "email_contact_count": email_contact_count,
        "whatsapp_contact_count": whatsapp_contact_count,
        "ledger_contact_count": ledger_contact_count,
        "uninvited_contact_count": uninvited_contact_count,
        "open_request_count": open_request_count,
        "uploaded_request_count": uploaded_request_count,
        "closed_request_count": closed_request_count,
        "overdue_request_count": overdue_request_count,
        "stale_uploaded_count": stale_uploaded_count,
        "missing_delivery_count": missing_delivery_count,
        "unreminded_overdue_count": unreminded_overdue_count,
        "total_request_count": total_request_count,
        "response_rate": response_rate,
        "recent_activity": recent_activity,
        "latest_upload_at": recent_upload.uploaded_at if recent_upload else None,
        "latest_confirmation_at": recent_confirmation.confirmed_at if recent_confirmation else None,
        "invoice_email_ready": bool(company.invoice_email_from_address),
        "whatsapp_intake_ready": bool(company.whatsapp_intake_number),
        "can_manage": _can_manage_company(user, company),
    }


def create_client_portal_health_tasks(rows, user):
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
            PracticeTask.objects.create(
                company=row["company"],
                title=f"Client portal: {gate['title']}",
                task_type=(
                    PracticeTask.TYPE_DOCUMENT
                    if any(token in gate["code"] for token in ("portal", "request", "uploaded", "reminder", "ledger"))
                    else PracticeTask.TYPE_OTHER
                ),
                priority=SEVERITY_PRIORITY[gate["severity"]],
                status=PracticeTask.STATUS_OPEN,
                due_date=today + timedelta(days=1 if gate["severity"] == "critical" else 3),
                assigned_to=user,
                created_by=user,
                reference=gate["reference"],
                description=f"{gate['detail']}\n\nAction: {gate['action_label']} - {gate['action_url']}",
            )
            created += 1
    return {"created": created, "existing": existing, "skipped": skipped}


def client_portal_health_csv_response(rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="client-portal-health.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "GSTIN",
        "Portal Score",
        "Portal Band",
        "Critical Gates",
        "Warning Gates",
        "Top Gates",
        "Active Portal Users",
        "Email Contacts",
        "WhatsApp Contacts",
        "Ledger Contacts",
        "Uninvited Ledger Contacts",
        "Open Requests",
        "Overdue Requests",
        "Uploaded Awaiting Review",
        "Stale Uploaded",
        "Missing Delivery",
        "Unreminded Overdue",
        "Response Rate",
        "Recent Activity",
        "Invoice Email Ready",
        "WhatsApp Intake Ready",
    ])
    for row in rows:
        writer.writerow([
            row["company"].name,
            row["company"].gstin or "",
            row["score"],
            row["band"],
            row["critical_count"],
            row["warning_count"],
            "; ".join(gate["title"] for gate in row["top_gates"]),
            row["active_portal_user_count"],
            row["email_contact_count"],
            row["whatsapp_contact_count"],
            row["ledger_contact_count"],
            row["uninvited_contact_count"],
            row["open_request_count"],
            row["overdue_request_count"],
            row["uploaded_request_count"],
            row["stale_uploaded_count"],
            row["missing_delivery_count"],
            row["unreminded_overdue_count"],
            row["response_rate"],
            "Yes" if row["recent_activity"] else "No",
            "Yes" if row["invoice_email_ready"] else "No",
            "Yes" if row["whatsapp_intake_ready"] else "No",
        ])
    return response


def _portal_contact_payload(contact):
    ledger = contact.linked_ledger
    return {
        "name": contact.name,
        "ledger_name": ledger.name if ledger else "",
        "email": _portal_contact_email(contact),
        "whatsapp": _portal_contact_whatsapp(contact),
        "status_label": "Active" if contact.is_active else "Inactive",
        "status_class": "bg-success-subtle text-success" if contact.is_active else "bg-secondary-subtle text-secondary",
    }


def _portal_contact_email(contact):
    return contact.email or (contact.linked_ledger.email if contact.linked_ledger else "") or ""


def _portal_contact_whatsapp(contact):
    return (contact.linked_ledger.whatsapp_number if contact.linked_ledger else "") or ""


def _companies_for_user(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return Company.objects.filter(user_access__user=user).distinct().order_by("name")


def _can_manage_company(user, company):
    if user.is_superuser:
        return True
    return UserCompanyAccess.objects.filter(user=user, company=company, role__in=["Admin", "Accountant"]).exists()


def _portal_health_band(score, critical_count, warning_count):
    if critical_count:
        return "disconnected", "Disconnected", "bg-danger", 0
    if score >= 90 and warning_count == 0:
        return "ready", "Ready", "bg-success", 3
    if score >= 75:
        return "watch", "Watch", "bg-primary", 2
    return "needs_chase", "Needs Chase", "bg-warning text-dark", 1


def _gate_sort_key(gate):
    return (
        {"critical": 0, "warning": 1, "info": 2}.get(gate["severity"], 3),
        gate["title"],
    )
