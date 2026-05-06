import csv
from datetime import timedelta
from urllib.parse import urlencode

from django.db.models import Avg, Q
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from .models import Company, PilotFeedback, PracticeTask, UserCompanyAccess


PILOT_FEEDBACK_TASK_PREFIX = "PILOTFEEDBACK:"


def build_pilot_feedback_register(user, params=None):
    params = params or {}
    today = timezone.localdate()
    companies = _companies_for_user(user)
    manageable_company_ids = set(_manageable_companies_for_user(user).values_list("pk", flat=True))
    q = (params.get("q") or "").strip()
    company_filter = (params.get("company") or "all").strip()
    status_filter = (params.get("status") or "active").strip()
    severity_filter = (params.get("severity") or "all").strip()
    sentiment_filter = (params.get("sentiment") or "all").strip()
    type_filter = (params.get("type") or "all").strip()
    competitor_filter = (params.get("competitor") or "all").strip()

    items = (
        PilotFeedback.objects.filter(company__in=companies)
        .select_related("company", "assigned_to", "recorded_by", "follow_up_task")
        .order_by("status", "-occurred_on", "company__name")
    )

    if q:
        items = items.filter(
            Q(company__name__icontains=q)
            | Q(company__gstin__icontains=q)
            | Q(summary__icontains=q)
            | Q(detail__icontains=q)
            | Q(client_contact__icontains=q)
            | Q(evidence_reference__icontains=q)
        )
    if company_filter != "all":
        try:
            items = items.filter(company_id=int(company_filter))
        except (TypeError, ValueError):
            company_filter = "all"
    if status_filter == "active":
        items = items.exclude(status__in=[PilotFeedback.STATUS_RESOLVED, PilotFeedback.STATUS_DISMISSED])
    elif status_filter != "all":
        items = items.filter(status=status_filter)
    if severity_filter != "all":
        items = items.filter(severity=severity_filter)
    if sentiment_filter != "all":
        items = items.filter(sentiment=sentiment_filter)
    if type_filter != "all":
        items = items.filter(feedback_type=type_filter)
    if competitor_filter != "all":
        items = items.filter(competitor_reference=competitor_filter)

    rows = [
        {
            "feedback": item,
            "can_manage": item.company_id in manageable_company_ids,
            "tasks_url": f"{reverse('core:practice_tasks')}?{urlencode({'company': item.company_id, 'status': 'open'})}",
        }
        for item in items
    ]
    last_30 = today - timedelta(days=30)
    open_items = [row["feedback"] for row in rows if row["feedback"].is_open]
    blocker_items = [item for item in open_items if item.is_blocker]
    confidence_values = [row["feedback"].confidence_score for row in rows]
    resolved_recent = [
        row["feedback"]
        for row in rows
        if row["feedback"].status == PilotFeedback.STATUS_RESOLVED
        and (
            (row["feedback"].resolved_at and row["feedback"].resolved_at.date() >= last_30)
            or row["feedback"].occurred_on >= last_30
        )
    ]
    totals = {
        "items": len(rows),
        "open": len(open_items),
        "blockers": len(blocker_items),
        "negative_open": sum(1 for item in open_items if item.sentiment == PilotFeedback.SENTIMENT_NEGATIVE),
        "recent_30": sum(1 for row in rows if row["feedback"].occurred_on >= last_30),
        "resolved_30": len(resolved_recent),
        "avg_confidence": round(sum(confidence_values) / len(confidence_values), 1) if confidence_values else 0,
        "conversion_signals": sum(
            1
            for row in rows
            if row["feedback"].feedback_type == PilotFeedback.TYPE_CONVERSION_SIGNAL
            or (
                row["feedback"].sentiment == PilotFeedback.SENTIMENT_POSITIVE
                and row["feedback"].confidence_score >= 8
            )
        ),
        "tally_mentions": sum(1 for row in rows if row["feedback"].competitor_reference == PilotFeedback.COMPETITOR_TALLY),
        "with_follow_up": sum(1 for row in rows if row["feedback"].follow_up_task_id),
        "manageable_clients": len(manageable_company_ids),
    }
    return {
        "rows": rows,
        "totals": totals,
        "q": q,
        "company_filter": company_filter,
        "status_filter": status_filter,
        "severity_filter": severity_filter,
        "sentiment_filter": sentiment_filter,
        "type_filter": type_filter,
        "competitor_filter": competitor_filter,
        "company_options": companies,
        "status_options": [("active", "Active")] + [("all", "All Statuses")] + PilotFeedback.STATUS_CHOICES,
        "severity_options": [("all", "All Severities")] + PilotFeedback.SEVERITY_CHOICES,
        "sentiment_options": [("all", "All Sentiments")] + PilotFeedback.SENTIMENT_CHOICES,
        "type_options": [("all", "All Types")] + PilotFeedback.FEEDBACK_TYPE_CHOICES,
        "competitor_options": [("all", "All Tools")] + PilotFeedback.COMPETITOR_CHOICES,
        "export_query": pilot_feedback_filter_query(params),
    }


def pilot_feedback_filter_query(params):
    values = {
        "q": (params.get("q") or "").strip(),
        "company": (params.get("company") or "all").strip(),
        "status": (params.get("status") or "active").strip(),
        "severity": (params.get("severity") or "all").strip(),
        "sentiment": (params.get("sentiment") or "all").strip(),
        "type": (params.get("type") or "all").strip(),
        "competitor": (params.get("competitor") or "all").strip(),
    }
    defaults = {
        "q": "",
        "company": "all",
        "status": "active",
        "severity": "all",
        "sentiment": "all",
        "type": "all",
        "competitor": "all",
    }
    return urlencode({key: value for key, value in values.items() if value != defaults[key]})


def pilot_feedback_csv_response(rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="pilot-feedback-register.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "GSTIN",
        "Date",
        "Type",
        "Sentiment",
        "Confidence",
        "Severity",
        "Status",
        "Summary",
        "Client Contact",
        "Competitor / Current Tool",
        "Evidence Reference",
        "Assigned To",
        "Follow Up Task",
    ])
    for row in rows:
        item = row["feedback"]
        writer.writerow([
            item.company.name,
            item.company.gstin or "",
            item.occurred_on.isoformat(),
            item.get_feedback_type_display(),
            item.get_sentiment_display(),
            item.confidence_score,
            item.get_severity_display(),
            item.get_status_display(),
            item.summary,
            item.client_contact,
            item.get_competitor_reference_display(),
            item.evidence_reference,
            item.assigned_to.email if item.assigned_to else "",
            item.follow_up_task.title if item.follow_up_task else "",
        ])
    return response


def create_pilot_feedback_follow_up(feedback, user):
    task = (
        PracticeTask.objects.filter(company=feedback.company, reference=f"{PILOT_FEEDBACK_TASK_PREFIX}{feedback.pk}")
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
        .first()
    )
    if task:
        if feedback.follow_up_task_id != task.pk:
            feedback.follow_up_task = task
            feedback.save(update_fields=["follow_up_task", "updated_at"])
        return task, False

    today = timezone.localdate()
    task = PracticeTask.objects.create(
        company=feedback.company,
        title=f"Pilot feedback: {feedback.summary[:120]}",
        task_type=PracticeTask.TYPE_OTHER,
        priority=_task_priority_for_feedback(feedback),
        status=PracticeTask.STATUS_OPEN,
        due_date=today + timedelta(days=_due_days_for_feedback(feedback)),
        assigned_to=feedback.assigned_to or user,
        created_by=user,
        reference=f"{PILOT_FEEDBACK_TASK_PREFIX}{feedback.pk}",
        description=_task_description_for_feedback(feedback),
    )
    feedback.follow_up_task = task
    feedback.save(update_fields=["follow_up_task", "updated_at"])
    return task, True


def resolve_pilot_feedback(feedback, user):
    feedback.status = PilotFeedback.STATUS_RESOLVED
    feedback.resolved_at = timezone.now()
    feedback.save(update_fields=["status", "resolved_at", "updated_at"])
    task = feedback.follow_up_task
    if task and task.status not in {PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED}:
        task.status = PracticeTask.STATUS_DONE
        task.completed_by = user
        task.completed_at = timezone.now()
        task.save(update_fields=["status", "completed_by", "completed_at", "updated_at"])
    return feedback


def reopen_pilot_feedback(feedback):
    feedback.status = PilotFeedback.STATUS_OPEN
    feedback.resolved_at = None
    feedback.save(update_fields=["status", "resolved_at", "updated_at"])
    return feedback


def build_pilot_feedback_signals(company, *, today=None):
    today = today or timezone.localdate()
    now = timezone.now()
    last_30_date = today - timedelta(days=30)
    last_30_dt = now - timedelta(days=30)
    items = PilotFeedback.objects.filter(company=company)
    recent_items = items.filter(occurred_on__gte=last_30_date)
    open_items = items.exclude(status__in=[PilotFeedback.STATUS_RESOLVED, PilotFeedback.STATUS_DISMISSED])
    recent_avg = recent_items.aggregate(value=Avg("confidence_score"))["value"]
    overall_avg = items.aggregate(value=Avg("confidence_score"))["value"]
    latest = items.order_by("-occurred_on", "-created_at").first()
    return {
        "recent_feedback_count": recent_items.count(),
        "open_feedback_count": open_items.count(),
        "open_blocker_count": open_items.filter(severity__in=[PilotFeedback.SEVERITY_HIGH, PilotFeedback.SEVERITY_CRITICAL]).count(),
        "open_negative_count": open_items.filter(sentiment=PilotFeedback.SENTIMENT_NEGATIVE).count(),
        "resolved_recent_count": items.filter(status=PilotFeedback.STATUS_RESOLVED, resolved_at__gte=last_30_dt).count(),
        "positive_signal_count": recent_items.filter(
            Q(feedback_type=PilotFeedback.TYPE_CONVERSION_SIGNAL)
            | Q(sentiment=PilotFeedback.SENTIMENT_POSITIVE, confidence_score__gte=8)
        ).count(),
        "tally_replacement_count": items.filter(competitor_reference=PilotFeedback.COMPETITOR_TALLY).count(),
        "avg_confidence": round(float(recent_avg if recent_avg is not None else overall_avg or 0), 1),
        "latest_summary": latest.summary if latest else "",
    }


def _companies_for_user(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return Company.objects.filter(user_access__user=user).distinct().order_by("name")


def _manageable_companies_for_user(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return (
        Company.objects.filter(
            user_access__user=user,
            user_access__role__in=["Admin", "Accountant"],
        )
        .distinct()
        .order_by("name")
    )


def _task_priority_for_feedback(feedback):
    if feedback.severity == PilotFeedback.SEVERITY_CRITICAL:
        return PracticeTask.PRIORITY_CRITICAL
    if feedback.severity == PilotFeedback.SEVERITY_HIGH:
        return PracticeTask.PRIORITY_HIGH
    if feedback.severity == PilotFeedback.SEVERITY_LOW:
        return PracticeTask.PRIORITY_LOW
    return PracticeTask.PRIORITY_NORMAL


def _due_days_for_feedback(feedback):
    if feedback.severity == PilotFeedback.SEVERITY_CRITICAL:
        return 1
    if feedback.severity == PilotFeedback.SEVERITY_HIGH:
        return 2
    if feedback.severity == PilotFeedback.SEVERITY_MEDIUM:
        return 4
    return 7


def _task_description_for_feedback(feedback):
    parts = [
        f"Type: {feedback.get_feedback_type_display()}",
        f"Sentiment: {feedback.get_sentiment_display()}",
        f"Confidence: {feedback.confidence_score}/10",
        f"Client contact: {feedback.client_contact or '-'}",
        f"Competitor/current tool: {feedback.get_competitor_reference_display()}",
        f"Evidence: {feedback.evidence_reference or '-'}",
        "",
        feedback.detail or feedback.summary,
    ]
    return "\n".join(parts)
