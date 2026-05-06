import csv
from datetime import timedelta
from urllib.parse import urlencode

from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from portal.models import BalanceConfirmation, ClientDocumentRequest, PortalUser
from vouchers.models import Voucher

from .models import ClientEngagement, Company, PracticeTask, UserCompanyAccess
from .pilot_launch import build_company_pilot_launch_row


CLIENT_SUCCESS_TASK_PREFIX = "CLIENTSUCCESS:"
SEVERITY_WEIGHT = {"critical": 22, "warning": 9, "info": 3}
SEVERITY_PRIORITY = {
    "critical": PracticeTask.PRIORITY_CRITICAL,
    "warning": PracticeTask.PRIORITY_HIGH,
    "info": PracticeTask.PRIORITY_NORMAL,
}


def build_client_success_cockpit(user, params=None):
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
        build_company_success_row(
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
        "champion": sum(1 for row in rows if row["band_key"] == "champion"),
        "healthy": sum(1 for row in rows if row["band_key"] == "healthy"),
        "at_risk": sum(1 for row in rows if row["band_key"] == "at_risk"),
        "critical": sum(1 for row in rows if row["band_key"] == "critical"),
        "critical_gates": sum(row["critical_count"] for row in rows),
        "warning_gates": sum(row["warning_count"] for row in rows),
        "overdue_documents": sum(row["overdue_document_requests"] for row in rows),
        "uploaded_documents": sum(row["uploaded_document_requests"] for row in rows),
        "portal_users": sum(row["portal_user_count"] for row in rows),
        "renewals_due": sum(1 for row in rows if row["renewal_due_soon"]),
        "manageable_clients": sum(1 for row in rows if row["can_manage"]),
    }
    return {
        "rows": rows,
        "totals": totals,
        "q": q,
        "band_filter": band_filter,
        "band_options": [
            ("all", "All Clients"),
            ("critical", "Critical"),
            ("at_risk", "At Risk"),
            ("healthy", "Healthy"),
            ("champion", "Champion"),
        ],
        "export_query": urlencode({key: value for key, value in {"q": q, "band": band_filter}.items() if value and value != "all"}),
    }


def build_company_success_row(company, user, *, today=None, subscription=None, engagement=None):
    today = today or timezone.localdate()
    last_30 = today - timedelta(days=30)
    launch = build_company_pilot_launch_row(
        company,
        user,
        today=today,
        subscription=subscription,
        engagement=engagement,
    )
    portal_users = PortalUser.objects.filter(linked_ledger__company=company, is_active=True).distinct()
    portal_user_count = portal_users.count()
    requests = ClientDocumentRequest.objects.filter(company=company)
    active_requests = requests.exclude(
        status__in=[ClientDocumentRequest.STATUS_CLOSED, ClientDocumentRequest.STATUS_CANCELLED]
    )
    open_documents = active_requests.filter(status=ClientDocumentRequest.STATUS_OPEN).count()
    overdue_documents = active_requests.filter(status=ClientDocumentRequest.STATUS_OPEN, due_date__lt=today).count()
    uploaded_documents = active_requests.filter(status=ClientDocumentRequest.STATUS_UPLOADED).count()
    closed_documents = requests.filter(status=ClientDocumentRequest.STATUS_CLOSED).count()
    total_requests = requests.count()
    document_response_rate = round((closed_documents / total_requests) * 100) if total_requests else 0
    latest_upload = requests.filter(uploaded_at__isnull=False).order_by("-uploaded_at").first()
    latest_confirmation = BalanceConfirmation.objects.filter(
        portal_user__linked_ledger__company=company,
    ).order_by("-confirmed_at").first()
    recent_portal_activity = bool(
        latest_upload and latest_upload.uploaded_at.date() >= last_30
    ) or bool(
        latest_confirmation and latest_confirmation.confirmed_at.date() >= last_30
    )

    tasks = PracticeTask.objects.filter(company=company).exclude(
        status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED]
    )
    open_tasks = tasks.count()
    overdue_tasks = tasks.filter(due_date__lt=today).count()
    critical_tasks = tasks.filter(priority=PracticeTask.PRIORITY_CRITICAL).count()
    success_tasks = tasks.filter(reference__startswith=CLIENT_SUCCESS_TASK_PREFIX).count()
    voucher_count_30 = Voucher.objects.filter(company=company, date__gte=last_30, date__lte=today).count()
    subscription_days_left = None
    usage_percent = 0
    if subscription:
        subscription_days_left = (subscription.subscription_end.date() - today).days
        usage_percent = subscription.usage_percentage()
    renewal_due_soon = bool(engagement and engagement.renewal_date and engagement.renewal_date <= today + timedelta(days=30))
    high_risk_stale_review = bool(
        engagement
        and engagement.risk_rating in {ClientEngagement.RISK_HIGH, ClientEngagement.RISK_CRITICAL}
        and (not engagement.last_reviewed_at or engagement.last_reviewed_at < today - timedelta(days=30))
    )

    gates = []

    def add_gate(code, title, passed, *, severity, detail, action_label, action_url):
        gates.append({
            "code": code,
            "reference": f"{CLIENT_SUCCESS_TASK_PREFIX}{company.pk}:{code}",
            "title": title,
            "passed": bool(passed),
            "severity": severity,
            "detail": detail,
            "action_label": action_label,
            "action_url": action_url,
        })

    client_360_url = reverse("core:client_360", args=[company.pk])
    engagement_url = reverse("core:client_engagement_update", args=[company.pk])
    requests_url = f"{reverse('portal:client_requests')}?{urlencode({'company': company.pk})}"
    reminders_url = f"{reverse('portal:client_request_reminders')}?{urlencode({'company': company.pk})}"
    tasks_url = f"{reverse('core:practice_tasks')}?{urlencode({'company': company.pk, 'status': 'open'})}"
    launch_url = reverse("core:client_pilot_launch")

    add_gate(
        "launch_not_blocked",
        "Launch is not blocked",
        launch["critical_count"] == 0 and launch["score"] >= 75,
        severity="critical",
        detail=f"Launch score is {launch['score']}% with {launch['critical_count']} critical gate(s).",
        action_label="Open Pilot Launch",
        action_url=launch_url,
    )
    add_gate(
        "subscription_safe",
        "Subscription safe",
        bool(subscription and subscription.status in {ClientSubscription.STATUS_ACTIVE, ClientSubscription.STATUS_TRIAL} and subscription_days_left is not None and subscription_days_left >= 7),
        severity="critical",
        detail=f"Subscription status is {subscription.status if subscription else 'missing'}; {subscription_days_left if subscription_days_left is not None else '-'} day(s) left.",
        action_label="Open Client 360",
        action_url=client_360_url,
    )
    add_gate(
        "engagement_owned",
        "Engagement owned",
        bool(engagement and (engagement.partner_owner_id or engagement.manager_owner_id)),
        severity="warning",
        detail="Every live client needs a named partner or manager owner.",
        action_label="Open Engagement",
        action_url=engagement_url,
    )
    add_gate(
        "portal_adoption",
        "Portal adoption active",
        portal_user_count >= 1 and (recent_portal_activity or total_requests == 0 or document_response_rate >= 50),
        severity="warning",
        detail=f"{portal_user_count} portal user(s), {document_response_rate}% closed request rate, recent activity {'yes' if recent_portal_activity else 'no'}.",
        action_label="Open Client Requests",
        action_url=requests_url,
    )
    add_gate(
        "document_chase_clean",
        "Document chase clean",
        overdue_documents == 0 and uploaded_documents <= 10,
        severity="warning",
        detail=f"{overdue_documents} overdue request(s), {uploaded_documents} uploaded request(s) awaiting review.",
        action_label="Open Reminders",
        action_url=reminders_url,
    )
    add_gate(
        "support_debt_clear",
        "Support debt under control",
        overdue_tasks == 0 and critical_tasks == 0,
        severity="warning",
        detail=f"{open_tasks} open task(s), {overdue_tasks} overdue, {critical_tasks} critical.",
        action_label="Open Work Queue",
        action_url=tasks_url,
    )
    add_gate(
        "usage_within_plan",
        "Usage within plan",
        usage_percent < 90,
        severity="info",
        detail=f"Subscription usage is {usage_percent}%.",
        action_label="Open Client 360",
        action_url=client_360_url,
    )
    add_gate(
        "renewal_not_due",
        "Renewal not due immediately",
        not renewal_due_soon,
        severity="warning",
        detail=f"Renewal date is {engagement.renewal_date.isoformat() if engagement and engagement.renewal_date else '-'}; subscription days left {subscription_days_left if subscription_days_left is not None else '-'}.",
        action_label="Open Engagement",
        action_url=engagement_url,
    )
    add_gate(
        "high_risk_reviewed",
        "High-risk client recently reviewed",
        not high_risk_stale_review,
        severity="warning",
        detail=f"Risk rating is {engagement.get_risk_rating_display() if engagement else 'missing'}; last reviewed {engagement.last_reviewed_at.isoformat() if engagement and engagement.last_reviewed_at else '-'}.",
        action_label="Open Engagement",
        action_url=engagement_url,
    )
    add_gate(
        "usage_signal",
        "Usage signal visible",
        voucher_count_30 > 0 or total_requests > 0 or recent_portal_activity,
        severity="info",
        detail=f"{voucher_count_30} voucher(s) in last 30 days and {total_requests} document request(s).",
        action_label="Open Client 360",
        action_url=client_360_url,
    )

    failed = [gate for gate in gates if not gate["passed"]]
    critical_count = sum(1 for gate in failed if gate["severity"] == "critical")
    warning_count = sum(1 for gate in failed if gate["severity"] == "warning")
    penalty = sum(SEVERITY_WEIGHT[gate["severity"]] for gate in failed)
    score = max(0, min(100, 100 - penalty))
    band_key, band, badge_class, sort_rank = _success_band(score, critical_count, warning_count)
    return {
        "company": company,
        "subscription": subscription,
        "engagement": engagement,
        "launch": launch,
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
        "portal_user_count": portal_user_count,
        "document_response_rate": document_response_rate,
        "open_document_requests": open_documents,
        "overdue_document_requests": overdue_documents,
        "uploaded_document_requests": uploaded_documents,
        "closed_document_requests": closed_documents,
        "open_task_count": open_tasks,
        "overdue_task_count": overdue_tasks,
        "critical_task_count": critical_tasks,
        "success_task_count": success_tasks,
        "voucher_count_30": voucher_count_30,
        "usage_percent": usage_percent,
        "subscription_days_left": subscription_days_left,
        "renewal_due_soon": renewal_due_soon,
        "recent_portal_activity": recent_portal_activity,
        "can_manage": _can_manage_company(user, company),
    }


def create_client_success_tasks(rows, user):
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
                title=f"Client success: {gate['title']}",
                task_type=PracticeTask.TYPE_DOCUMENT if "document" in gate["code"] or "portal" in gate["code"] else PracticeTask.TYPE_OTHER,
                priority=SEVERITY_PRIORITY[gate["severity"]],
                status=PracticeTask.STATUS_OPEN,
                due_date=today + timedelta(days=1 if gate["severity"] == "critical" else 4),
                assigned_to=user,
                created_by=user,
                reference=gate["reference"],
                description=f"{gate['detail']}\n\nAction: {gate['action_label']} - {gate['action_url']}",
            )
            created += 1
    return {"created": created, "existing": existing, "skipped": skipped}


def client_success_csv_response(rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="client-success-cockpit.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "GSTIN",
        "Success Score",
        "Success Band",
        "Launch Score",
        "Critical Gates",
        "Warning Gates",
        "Top Gates",
        "Portal Users",
        "Document Response Rate",
        "Overdue Documents",
        "Uploaded Documents",
        "Open Tasks",
        "Overdue Tasks",
        "Voucher Count 30 Days",
        "Subscription Days Left",
        "Usage Percent",
    ])
    for row in rows:
        writer.writerow([
            row["company"].name,
            row["company"].gstin or "",
            row["score"],
            row["band"],
            row["launch"]["score"],
            row["critical_count"],
            row["warning_count"],
            "; ".join(gate["title"] for gate in row["top_gates"]),
            row["portal_user_count"],
            row["document_response_rate"],
            row["overdue_document_requests"],
            row["uploaded_document_requests"],
            row["open_task_count"],
            row["overdue_task_count"],
            row["voucher_count_30"],
            row["subscription_days_left"] if row["subscription_days_left"] is not None else "",
            row["usage_percent"],
        ])
    return response


def _companies_for_user(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return Company.objects.filter(user_access__user=user).distinct().order_by("name")


def _can_manage_company(user, company):
    if user.is_superuser:
        return True
    return UserCompanyAccess.objects.filter(user=user, company=company, role__in=["Admin", "Accountant"]).exists()


def _success_band(score, critical_count, warning_count):
    if critical_count:
        return "critical", "Critical", "bg-danger", 0
    if score >= 92 and warning_count == 0:
        return "champion", "Champion", "bg-success", 3
    if score >= 78:
        return "healthy", "Healthy", "bg-primary", 2
    if score >= 60:
        return "at_risk", "At Risk", "bg-warning text-dark", 1
    return "critical", "Critical", "bg-danger", 0


def _gate_sort_key(gate):
    return (
        {"critical": 0, "warning": 1, "info": 2}.get(gate["severity"], 3),
        gate["title"],
    )
