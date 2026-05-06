"""
core/context_processors.py

Injects:
  - current_company  → the active Company object (or None)
  - user_companies   → all companies the logged-in user can access
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from .models import (
    CompanyStatutoryProfile,
    ComplianceFiling,
    ComplianceNotice,
    PracticeTask,
    UserCompanyAccess,
)


def _add_notification(items, level, icon, title, detail, url, count=1):
    items.append({
        "level": level,
        "icon": icon,
        "title": title,
        "detail": detail,
        "url": url,
        "count": count,
    })


def _setup_gap_count(company):
    try:
        statutory_profile = company.statutory_profile
    except CompanyStatutoryProfile.DoesNotExist:
        statutory_profile = None

    checks = [
        bool(company.gstin) or bool(statutory_profile and not statutory_profile.gst_registered),
        bool(company.tan) or bool(statutory_profile and not statutory_profile.tds_applicable),
        bool(statutory_profile),
        bool(company.whatsapp_intake_number),
        bool(company.invoice_email_from_address),
        bool(company.portal_token),
        any([company.bank_name, company.account_number, company.ifsc_code, company.upi_id]),
    ]
    return len([ready for ready in checks if not ready])


def _notification_summary(company):
    today = timezone.localdate()
    due_soon = today + timedelta(days=7)
    items = []

    setup_gaps = _setup_gap_count(company)
    if setup_gaps:
        _add_notification(
            items,
            "warning",
            "bi-ui-checks-grid",
            "Setup wizard needs attention",
            f"{setup_gaps} setup item(s) are incomplete.",
            reverse("core:setup_wizard"),
            setup_gaps,
        )

    open_tasks = PracticeTask.objects.filter(company=company).exclude(
        status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED]
    )
    overdue_tasks = open_tasks.filter(due_date__lt=today).count()
    blocked_tasks = open_tasks.filter(status=PracticeTask.STATUS_BLOCKED).count()
    critical_tasks = open_tasks.filter(priority=PracticeTask.PRIORITY_CRITICAL).count()
    if overdue_tasks or blocked_tasks or critical_tasks:
        _add_notification(
            items,
            "danger" if overdue_tasks or blocked_tasks else "warning",
            "bi-kanban",
            "Practice work needs review",
            f"{overdue_tasks} overdue, {blocked_tasks} blocked, {critical_tasks} critical task(s).",
            reverse("core:practice_tasks"),
            overdue_tasks + blocked_tasks + critical_tasks,
        )

    open_filings = ComplianceFiling.objects.filter(company=company).exclude(
        status__in=[ComplianceFiling.STATUS_FILED, ComplianceFiling.STATUS_CANCELLED]
    )
    overdue_filings = open_filings.filter(due_date__lt=today).count()
    due_filings = open_filings.filter(due_date__gte=today, due_date__lte=due_soon).count()
    ready_filings = open_filings.filter(status=ComplianceFiling.STATUS_READY_FOR_REVIEW).count()
    if overdue_filings or due_filings or ready_filings:
        _add_notification(
            items,
            "danger" if overdue_filings else "warning",
            "bi-calendar2-week",
            "Statutory filings are active",
            f"{overdue_filings} overdue, {due_filings} due soon, {ready_filings} ready for review.",
            reverse("core:compliance_filings"),
            overdue_filings + due_filings + ready_filings,
        )

    open_notices = ComplianceNotice.objects.filter(company=company).exclude(status=ComplianceNotice.STATUS_CLOSED)
    overdue_notices = open_notices.filter(response_due_date__lt=today).count()
    escalated_notices = open_notices.filter(status=ComplianceNotice.STATUS_ESCALATED).count()
    due_notices = open_notices.filter(response_due_date__gte=today, response_due_date__lte=due_soon).count()
    if overdue_notices or escalated_notices or due_notices:
        _add_notification(
            items,
            "danger" if overdue_notices or escalated_notices else "warning",
            "bi-envelope-exclamation",
            "Notice response watch",
            f"{overdue_notices} overdue, {escalated_notices} escalated, {due_notices} due soon.",
            reverse("core:compliance_notices"),
            overdue_notices + escalated_notices + due_notices,
        )

    try:
        from portal.models import ClientDocumentRequest
    except ImportError:
        ClientDocumentRequest = None

    if ClientDocumentRequest:
        client_requests = ClientDocumentRequest.objects.filter(company=company)
        open_client_requests = client_requests.filter(status=ClientDocumentRequest.STATUS_OPEN)
        overdue_requests = open_client_requests.filter(due_date__lt=today).count()
        uploaded_requests = client_requests.filter(status=ClientDocumentRequest.STATUS_UPLOADED).count()
        if overdue_requests or uploaded_requests:
            _add_notification(
                items,
                "warning",
                "bi-inbox",
                "Client documents need action",
                f"{overdue_requests} overdue request(s), {uploaded_requests} uploaded item(s).",
                reverse("portal:client_requests"),
                overdue_requests + uploaded_requests,
            )

    count = sum(item["count"] for item in items)
    return {
        "count": count,
        "items": items[:6],
        "has_items": bool(items),
    }


def current_company(request):
    context = {
        "current_company": getattr(request, "current_company", None),
        "current_company_role": getattr(request, "current_company_role", None),
        "user_companies": [],
        "notification_summary": {"count": 0, "items": [], "has_items": False},
    }

    if request.user.is_authenticated:
        context["user_companies"] = (
            UserCompanyAccess.objects.filter(user=request.user)
            .select_related("company")
            .order_by("company__name")
        )
        if context["current_company"]:
            context["notification_summary"] = _notification_summary(context["current_company"])

    return context
