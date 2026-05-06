import csv
from datetime import timedelta
from urllib.parse import urlencode

from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from portal.models import ClientDocumentRequest, PortalUser
from vouchers.models import Voucher

from .client_success import build_company_success_row
from .models import AuditLog, ClientEngagement, Company, PracticeTask, UserCompanyAccess
from .pilot_feedback import build_pilot_feedback_signals
from .pilot_launch import build_company_pilot_launch_row


PILOT_ADOPTION_TASK_PREFIX = "PILOTADOPT:"
SEVERITY_WEIGHT = {"critical": 22, "warning": 9, "info": 3}
SEVERITY_PRIORITY = {
    "critical": PracticeTask.PRIORITY_CRITICAL,
    "warning": PracticeTask.PRIORITY_HIGH,
    "info": PracticeTask.PRIORITY_NORMAL,
}


def build_pilot_adoption_evidence(user, params=None):
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
        build_company_pilot_adoption_row(
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
        -row["evidence_gap_count"],
        row["company"].name,
    ))
    totals = {
        "clients": len(rows),
        "avg_score": round(sum(row["score"] for row in rows) / len(rows)) if rows else 0,
        "scale_ready": sum(1 for row in rows if row["band_key"] == "scale_ready"),
        "pilot_healthy": sum(1 for row in rows if row["band_key"] == "pilot_healthy"),
        "needs_evidence": sum(1 for row in rows if row["band_key"] == "needs_evidence"),
        "blocked": sum(1 for row in rows if row["band_key"] == "blocked"),
        "critical_gates": sum(row["critical_count"] for row in rows),
        "warning_gates": sum(row["warning_count"] for row in rows),
        "recent_activity": sum(row["recent_audit_count"] for row in rows),
        "feedback_signals": sum(row["feedback_signals"]["recent_feedback_count"] for row in rows),
        "feedback_blockers": sum(row["feedback_signals"]["open_blocker_count"] for row in rows),
        "closed_loop_tasks": sum(row["closed_recent_task_count"] for row in rows),
        "responded_requests": sum(row["responded_request_count"] for row in rows),
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
            ("needs_evidence", "Needs Evidence"),
            ("pilot_healthy", "Pilot Healthy"),
            ("scale_ready", "Scale Ready"),
        ],
        "export_query": urlencode({key: value for key, value in {"q": q, "band": band_filter}.items() if value and value != "all"}),
    }


def build_company_pilot_adoption_row(company, user, *, today=None, subscription=None, engagement=None):
    today = today or timezone.localdate()
    now = timezone.now()
    last_30_date = today - timedelta(days=30)
    last_30_dt = now - timedelta(days=30)
    last_14_dt = now - timedelta(days=14)

    launch = build_company_pilot_launch_row(company, user, today=today, subscription=subscription, engagement=engagement)
    success = build_company_success_row(company, user, today=today, subscription=subscription, engagement=engagement)

    portal_user_count = PortalUser.objects.filter(linked_ledger__company=company, is_active=True).distinct().count()
    requests = ClientDocumentRequest.objects.filter(company=company)
    total_request_count = requests.count()
    responded_request_count = requests.filter(
        status__in=[ClientDocumentRequest.STATUS_UPLOADED, ClientDocumentRequest.STATUS_CLOSED]
    ).count()
    closed_request_count = requests.filter(status=ClientDocumentRequest.STATUS_CLOSED).count()
    open_overdue_request_count = requests.filter(
        status=ClientDocumentRequest.STATUS_OPEN,
        due_date__lt=today,
    ).count()
    response_rate = round((responded_request_count / total_request_count) * 100) if total_request_count else 0

    recent_audit_count = AuditLog.objects.filter(company=company, timestamp__gte=last_30_dt).count()
    recent_active_users = AuditLog.objects.filter(
        company=company,
        timestamp__gte=last_30_dt,
        user__isnull=False,
    ).values("user_id").distinct().count()
    audit_14d_count = AuditLog.objects.filter(company=company, timestamp__gte=last_14_dt).count()
    voucher_count_30 = Voucher.objects.filter(company=company, date__gte=last_30_date, date__lte=today).count()
    feedback_signals = build_pilot_feedback_signals(company, today=today)

    tasks = PracticeTask.objects.filter(company=company)
    open_tasks = tasks.exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
    open_task_count = open_tasks.count()
    overdue_task_count = open_tasks.filter(due_date__lt=today).count()
    critical_task_count = open_tasks.filter(priority=PracticeTask.PRIORITY_CRITICAL).count()
    closed_recent_task_count = tasks.filter(
        status=PracticeTask.STATUS_DONE,
        completed_at__gte=last_30_dt,
    ).count()
    pilot_issue_task_count = tasks.filter(
        reference__startswith=PILOT_ADOPTION_TASK_PREFIX,
    ).count()

    access_count = UserCompanyAccess.objects.filter(company=company).count()
    manage_access_count = UserCompanyAccess.objects.filter(company=company, role__in=["Admin", "Accountant"]).count()
    owner_assigned = bool(engagement and (engagement.partner_owner_id or engagement.manager_owner_id))
    review_recent = bool(engagement and engagement.last_reviewed_at and engagement.last_reviewed_at >= last_30_date)
    scope_documented = bool(engagement and engagement.scope_summary.strip())
    commercial_signal = bool(
        engagement
        and (
            engagement.monthly_retainer > 0
            or engagement.status == ClientEngagement.STATUS_ACTIVE
        )
    )
    subscription_days_left = None
    subscription_safe = False
    if subscription:
        subscription_days_left = (subscription.subscription_end.date() - today).days
        subscription_safe = (
            subscription.status in {ClientSubscription.STATUS_ACTIVE, ClientSubscription.STATUS_TRIAL}
            and subscription_days_left >= 14
        )

    gates = []

    def add_gate(code, title, passed, *, severity, detail, action_label, action_url):
        gates.append({
            "code": code,
            "reference": f"{PILOT_ADOPTION_TASK_PREFIX}{company.pk}:{code}",
            "title": title,
            "passed": bool(passed),
            "severity": severity,
            "detail": detail,
            "action_label": action_label,
            "action_url": action_url,
        })

    launch_url = reverse("core:client_pilot_launch")
    success_url = reverse("core:client_success_cockpit")
    engagement_url = reverse("core:client_engagement_update", args=[company.pk])
    client_360_url = reverse("core:client_360", args=[company.pk])
    requests_url = f"{reverse('portal:client_requests')}?{urlencode({'company': company.pk, 'status': 'active'})}"
    reminders_url = f"{reverse('portal:client_request_reminders')}?{urlencode({'company': company.pk})}"
    tasks_url = f"{reverse('core:practice_tasks')}?{urlencode({'company': company.pk, 'status': 'open'})}"
    audit_url = reverse("core:audit_log")
    feedback_url = f"{reverse('core:pilot_feedback_register')}?{urlencode({'company': company.pk})}"

    add_gate(
        "launch_validated",
        "Launch validation passed",
        launch["critical_count"] == 0 and launch["score"] >= 75,
        severity="critical",
        detail=f"Launch score is {launch['score']}% with {launch['critical_count']} critical gate(s).",
        action_label="Open Pilot Launch",
        action_url=launch_url,
    )
    add_gate(
        "success_validated",
        "Success health validated",
        success["critical_count"] == 0 and success["score"] >= 78,
        severity="critical",
        detail=f"Success score is {success['score']}% with {success['critical_count']} critical gate(s).",
        action_label="Open Client Success",
        action_url=success_url,
    )
    add_gate(
        "commercial_access_safe",
        "Commercial access safe",
        subscription_safe,
        severity="critical",
        detail=f"Subscription status is {subscription.status if subscription else 'missing'}; {subscription_days_left if subscription_days_left is not None else '-'} day(s) left.",
        action_label="Open Client 360",
        action_url=client_360_url,
    )
    add_gate(
        "real_usage_visible",
        "Real usage visible",
        voucher_count_30 > 0 or audit_14d_count >= 3 or responded_request_count > 0,
        severity="critical",
        detail=f"{voucher_count_30} voucher(s), {audit_14d_count} audit event(s) in 14 days, {responded_request_count} request response(s).",
        action_label="Open Audit Trail",
        action_url=audit_url,
    )
    add_gate(
        "client_response_proven",
        "Client response proven",
        portal_user_count > 0 and responded_request_count > 0,
        severity="warning",
        detail=f"{portal_user_count} portal user(s), {responded_request_count}/{total_request_count} request(s) responded.",
        action_label="Open Client Requests",
        action_url=requests_url,
    )
    add_gate(
        "feedback_owner_assigned",
        "Feedback owner assigned",
        owner_assigned and scope_documented,
        severity="warning",
        detail="Pilot feedback needs a named partner/manager and documented scope.",
        action_label="Open Engagement",
        action_url=engagement_url,
    )
    add_gate(
        "feedback_review_recent",
        "Feedback reviewed recently",
        review_recent,
        severity="warning",
        detail=f"Last engagement review is {engagement.last_reviewed_at.isoformat() if engagement and engagement.last_reviewed_at else '-'}; expected within 30 days.",
        action_label="Open Engagement",
        action_url=engagement_url,
    )
    add_gate(
        "pilot_feedback_captured",
        "Pilot feedback captured",
        feedback_signals["recent_feedback_count"] > 0,
        severity="warning",
        detail=f"{feedback_signals['recent_feedback_count']} feedback signal(s) captured in 30 days; latest: {feedback_signals['latest_summary'] or '-'}",
        action_label="Open Feedback Register",
        action_url=feedback_url,
    )
    add_gate(
        "feedback_blockers_controlled",
        "Feedback blockers controlled",
        feedback_signals["open_blocker_count"] == 0,
        severity="critical",
        detail=f"{feedback_signals['open_blocker_count']} open high/critical feedback blocker(s), {feedback_signals['open_negative_count']} open negative signal(s).",
        action_label="Open Feedback Register",
        action_url=feedback_url,
    )
    add_gate(
        "client_confidence_visible",
        "Client confidence visible",
        feedback_signals["avg_confidence"] >= 7 and (
            feedback_signals["positive_signal_count"] > 0
            or feedback_signals["resolved_recent_count"] > 0
        ),
        severity="warning",
        detail=f"Average confidence is {feedback_signals['avg_confidence']}/10 with {feedback_signals['positive_signal_count']} positive conversion signal(s) and {feedback_signals['resolved_recent_count']} recent resolution(s).",
        action_label="Open Feedback Register",
        action_url=feedback_url,
    )
    add_gate(
        "issue_sla_controlled",
        "Issue SLA controlled",
        overdue_task_count == 0 and critical_task_count == 0 and open_overdue_request_count == 0,
        severity="critical",
        detail=f"{overdue_task_count} overdue task(s), {critical_task_count} critical task(s), {open_overdue_request_count} overdue request(s).",
        action_label="Open Work Queue",
        action_url=tasks_url,
    )
    add_gate(
        "closed_loop_evidence",
        "Closed-loop evidence exists",
        closed_recent_task_count > 0 or closed_request_count > 0,
        severity="warning",
        detail=f"{closed_recent_task_count} task(s) closed and {closed_request_count} request(s) closed in the pilot evidence window.",
        action_label="Open Client Requests",
        action_url=requests_url,
    )
    add_gate(
        "team_operational",
        "CA team operational",
        access_count >= 1 and manage_access_count >= 1 and recent_active_users >= 1,
        severity="warning",
        detail=f"{access_count} access record(s), {manage_access_count} manager/admin, {recent_active_users} recent active user(s).",
        action_label="Open Security Control",
        action_url=reverse("core:security_control"),
    )
    add_gate(
        "commercial_signal",
        "Conversion signal visible",
        commercial_signal,
        severity="warning",
        detail=f"Engagement status is {engagement.status if engagement else 'missing'}; retainer is {engagement.monthly_retainer if engagement else '-'}.",
        action_label="Open Engagement",
        action_url=engagement_url,
    )
    add_gate(
        "support_load_measurable",
        "Support load measurable",
        open_task_count + closed_recent_task_count + pilot_issue_task_count > 0,
        severity="info",
        detail=f"{open_task_count} open task(s), {closed_recent_task_count} recently closed, {pilot_issue_task_count} pilot adoption task(s).",
        action_label="Open Work Queue",
        action_url=tasks_url,
    )

    failed = [gate for gate in gates if not gate["passed"]]
    critical_count = sum(1 for gate in failed if gate["severity"] == "critical")
    warning_count = sum(1 for gate in failed if gate["severity"] == "warning")
    penalty = sum(SEVERITY_WEIGHT[gate["severity"]] for gate in failed)
    score = max(0, min(100, 100 - penalty))
    band_key, band, badge_class, sort_rank = _adoption_band(score, critical_count, warning_count)
    evidence_gap_count = critical_count + warning_count
    return {
        "company": company,
        "subscription": subscription,
        "engagement": engagement,
        "launch": launch,
        "success": success,
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
        "evidence_gap_count": evidence_gap_count,
        "passed_count": len(gates) - len(failed),
        "total_gates": len(gates),
        "portal_user_count": portal_user_count,
        "total_request_count": total_request_count,
        "responded_request_count": responded_request_count,
        "closed_request_count": closed_request_count,
        "response_rate": response_rate,
        "recent_audit_count": recent_audit_count,
        "audit_14d_count": audit_14d_count,
        "recent_active_users": recent_active_users,
        "voucher_count_30": voucher_count_30,
        "feedback_signals": feedback_signals,
        "open_task_count": open_task_count,
        "overdue_task_count": overdue_task_count,
        "critical_task_count": critical_task_count,
        "closed_recent_task_count": closed_recent_task_count,
        "pilot_issue_task_count": pilot_issue_task_count,
        "subscription_days_left": subscription_days_left,
        "commercial_signal": commercial_signal,
        "review_recent": review_recent,
        "can_manage": _can_manage_company(user, company),
    }


def create_pilot_adoption_tasks(rows, user):
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
                title=f"Pilot adoption: {gate['title']}",
                task_type=PracticeTask.TYPE_DOCUMENT if "client_response" in gate["code"] or "closed_loop" in gate["code"] else PracticeTask.TYPE_OTHER,
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


def pilot_adoption_csv_response(rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="pilot-adoption-evidence.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "GSTIN",
        "Adoption Score",
        "Adoption Band",
        "Launch Score",
        "Success Score",
        "Critical Gates",
        "Warning Gates",
        "Top Gates",
        "Portal Users",
        "Responded Requests",
        "Response Rate",
        "Audit Events 30 Days",
        "Audit Events 14 Days",
        "Recent Active Users",
        "Vouchers 30 Days",
        "Open Tasks",
        "Overdue Tasks",
        "Closed Tasks 30 Days",
        "Feedback Signals 30 Days",
        "Open Feedback Blockers",
        "Avg Feedback Confidence",
        "Positive Conversion Signals",
        "Tally Replacement Mentions",
        "Subscription Days Left",
        "Commercial Signal",
    ])
    for row in rows:
        writer.writerow([
            row["company"].name,
            row["company"].gstin or "",
            row["score"],
            row["band"],
            row["launch"]["score"],
            row["success"]["score"],
            row["critical_count"],
            row["warning_count"],
            "; ".join(gate["title"] for gate in row["top_gates"]),
            row["portal_user_count"],
            row["responded_request_count"],
            row["response_rate"],
            row["recent_audit_count"],
            row["audit_14d_count"],
            row["recent_active_users"],
            row["voucher_count_30"],
            row["open_task_count"],
            row["overdue_task_count"],
            row["closed_recent_task_count"],
            row["feedback_signals"]["recent_feedback_count"],
            row["feedback_signals"]["open_blocker_count"],
            row["feedback_signals"]["avg_confidence"],
            row["feedback_signals"]["positive_signal_count"],
            row["feedback_signals"]["tally_replacement_count"],
            row["subscription_days_left"] if row["subscription_days_left"] is not None else "",
            "Yes" if row["commercial_signal"] else "No",
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


def _adoption_band(score, critical_count, warning_count):
    if critical_count:
        return "blocked", "Blocked", "bg-danger", 0
    if score >= 92 and warning_count == 0:
        return "scale_ready", "Scale Ready", "bg-success", 3
    if score >= 78:
        return "pilot_healthy", "Pilot Healthy", "bg-primary", 2
    if score >= 60:
        return "needs_evidence", "Needs Evidence", "bg-warning text-dark", 1
    return "blocked", "Blocked", "bg-danger", 0


def _gate_sort_key(gate):
    return (
        {"critical": 0, "warning": 1, "info": 2}.get(gate["severity"], 3),
        gate["title"],
    )
