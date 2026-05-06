import calendar
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from .compliance_workflow import sync_task_for_filing
from .filing_readiness import build_filing_readiness
from .models import ComplianceFiling, FilingReview, PracticeTask


SUPPORTED_REVIEW_TYPES = (FilingReview.TYPE_GST_MONTHLY,)


def review_type_choices():
    return [
        choice
        for choice in FilingReview.REVIEW_TYPE_CHOICES
        if choice[0] in SUPPORTED_REVIEW_TYPES
    ]


def normalise_review_type(value):
    if value in SUPPORTED_REVIEW_TYPES:
        return value
    return FilingReview.TYPE_GST_MONTHLY


def build_filing_review(company, period_start, period_end, review_type=None):
    review_type = normalise_review_type(review_type)
    report = build_filing_readiness(company, period_start, period_end)
    review = (
        FilingReview.objects.filter(
            company=company,
            review_type=review_type,
            period_start=period_start,
            period_end=period_end,
        )
        .select_related("prepared_by", "reviewed_by", "approved_by", "sent_back_by")
        .first()
    )
    waivers = review.waived_blockers if review else {}
    detail_checks = [_decorate_check(check, waivers.get(check.code)) for check in report["checks"]]
    issue_checks = [item for item in detail_checks if item["is_issue"]]
    waived_count = sum(1 for item in issue_checks if item["is_waived"])
    unwaived_critical_count = sum(
        1 for item in issue_checks if item["severity"] == "critical" and not item["is_waived"]
    )
    unwaived_warning_count = sum(
        1 for item in issue_checks if item["severity"] == "warning" and not item["is_waived"]
    )
    ready_to_file = unwaived_critical_count == 0

    return {
        "company": company,
        "period_start": period_start,
        "period_end": period_end,
        "period_value": period_start.strftime("%Y-%m"),
        "review_type": review_type,
        "review": review,
        "report": report,
        "checks": detail_checks,
        "issue_checks": issue_checks,
        "readiness_score": report["score"],
        "risk_score": 100 - report["score"],
        "critical_count": report["critical_count"],
        "warning_count": report["warning_count"],
        "waived_count": waived_count,
        "unwaived_critical_count": unwaived_critical_count,
        "unwaived_warning_count": unwaived_warning_count,
        "ready_to_file": ready_to_file,
        "approval_label": "Ready to File" if ready_to_file else "Blocked",
    }


def build_filing_review_rows(companies, period_start, period_end, review_type=None):
    rows = [
        build_filing_review(company, period_start, period_end, review_type)
        for company in companies
    ]
    rows.sort(
        key=lambda row: (
            0 if row["unwaived_critical_count"] else 1,
            -row["risk_score"],
            row["company"].name.lower(),
        )
    )
    return rows


@transaction.atomic
def start_review(summary, user, notes=""):
    review = _ensure_review(summary, user)
    review.status = FilingReview.STATUS_UNDER_REVIEW
    review.notes = notes.strip()
    _refresh_review_snapshot(review, summary)
    review.save()
    return review


@transaction.atomic
def mark_reviewed(summary, user, notes=""):
    review = _ensure_review(summary, user)
    review.status = FilingReview.STATUS_REVIEWED
    review.reviewed_by = user
    review.reviewed_at = timezone.now()
    review.notes = notes.strip()
    _refresh_review_snapshot(review, summary)
    review.save()
    return review


@transaction.atomic
def send_back_review(summary, user, notes=""):
    review = _ensure_review(summary, user)
    review.status = FilingReview.STATUS_SENT_BACK
    review.sent_back_by = user
    review.sent_back_at = timezone.now()
    review.approved_by = None
    review.approved_at = None
    review.notes = notes.strip()
    _refresh_review_snapshot(review, summary)
    review.save()
    _create_send_back_task(review, user, notes)
    return review


@transaction.atomic
def approve_review(summary, user, notes=""):
    review = _ensure_review(summary, user)
    refreshed = build_filing_review(
        review.company,
        review.period_start,
        review.period_end,
        review.review_type,
    )
    if refreshed["unwaived_critical_count"]:
        raise ValueError("Critical blockers must be cleared or waived before approval.")

    review.status = FilingReview.STATUS_APPROVED
    if not review.reviewed_by_id:
        review.reviewed_by = user
        review.reviewed_at = timezone.now()
    review.approved_by = user
    review.approved_at = timezone.now()
    review.notes = notes.strip()
    _refresh_review_snapshot(review, refreshed)
    review.save()
    ready_count = _mark_related_filings_ready(review, user)
    return review, ready_count


@transaction.atomic
def reopen_review(summary, user, notes=""):
    review = _ensure_review(summary, user)
    review.status = FilingReview.STATUS_REOPENED
    review.approved_by = None
    review.approved_at = None
    review.notes = notes.strip()
    _refresh_review_snapshot(review, summary)
    review.save()
    return review


@transaction.atomic
def waive_blocker(summary, code, user, note):
    note = note.strip()
    if not note:
        raise ValueError("Waiver note is required.")

    review = _ensure_review(summary, user)
    if review.status == FilingReview.STATUS_APPROVED:
        raise ValueError("Reopen the review before changing waivers.")

    item = _find_issue(summary, code)
    waivers = dict(review.waived_blockers or {})
    waivers[code] = {
        "code": item["code"],
        "title": item["title"],
        "severity": item["severity"],
        "count": item["count"],
        "note": note,
        "waived_by_id": user.pk,
        "waived_by": getattr(user, "email", str(user)),
        "waived_at": timezone.now().isoformat(),
    }
    review.waived_blockers = waivers
    review.save(update_fields=["waived_blockers", "updated_at"])
    refreshed = build_filing_review(review.company, review.period_start, review.period_end, review.review_type)
    _refresh_review_snapshot(review, refreshed)
    review.save()
    return review


@transaction.atomic
def unwaive_blocker(summary, code, user):
    review = _ensure_review(summary, user)
    if review.status == FilingReview.STATUS_APPROVED:
        raise ValueError("Reopen the review before changing waivers.")

    waivers = dict(review.waived_blockers or {})
    if code not in waivers:
        raise ValueError("This blocker is not waived.")
    waivers.pop(code)
    review.waived_blockers = waivers
    review.save(update_fields=["waived_blockers", "updated_at"])
    refreshed = build_filing_review(review.company, review.period_start, review.period_end, review.review_type)
    _refresh_review_snapshot(review, refreshed)
    review.save()
    return review


@transaction.atomic
def create_review_blocker_tasks(summary, user):
    created = 0
    existing = 0
    today = timezone.localdate()
    for item in summary["issue_checks"]:
        if item["is_waived"]:
            continue
        reference = f"FREV:{summary['company'].pk}:{summary['period_value']}:{item['code']}"
        due_days = 2 if item["severity"] == "critical" else 7
        _, was_created = PracticeTask.objects.get_or_create(
            company=summary["company"],
            reference=reference,
            defaults={
                "title": f"Review blocker {summary['period_value']}: {item['title']}",
                "task_type": item["task_type"],
                "priority": item["priority"],
                "status": PracticeTask.STATUS_OPEN,
                "due_date": today + timedelta(days=due_days),
                "period_start": summary["period_start"],
                "period_end": summary["period_end"],
                "created_by": user,
                "description": f"{item['description']}\nAction: {item['action_label']}",
            },
        )
        if was_created:
            created += 1
        else:
            existing += 1
    return {"created": created, "existing": existing}


def _decorate_check(check, waiver):
    is_issue = check.is_issue
    is_waived = bool(is_issue and waiver)
    return {
        "check": check,
        "code": check.code,
        "title": check.title,
        "severity": check.severity,
        "count": check.count,
        "amount": check.amount,
        "description": check.description,
        "action_label": check.action_label,
        "action_url": check.action_url,
        "task_type": check.task_type,
        "priority": check.priority,
        "is_issue": is_issue,
        "is_waived": is_waived,
        "waiver": waiver if is_waived else None,
    }


def _ensure_review(summary, user):
    review, _ = FilingReview.objects.get_or_create(
        company=summary["company"],
        review_type=summary["review_type"],
        period_start=summary["period_start"],
        period_end=summary["period_end"],
        defaults={"prepared_by": user},
    )
    if not review.prepared_by_id:
        review.prepared_by = user
    return review


def _refresh_review_snapshot(review, summary):
    review.readiness_score = summary["readiness_score"]
    review.risk_score = summary["risk_score"]
    review.blocker_snapshot = {
        "readiness": summary["report"]["snapshot"],
        "approval": {
            "ready_to_file": summary["ready_to_file"],
            "unwaived_critical_count": summary["unwaived_critical_count"],
            "unwaived_warning_count": summary["unwaived_warning_count"],
            "waived_count": summary["waived_count"],
        },
        "checks": [
            {
                "code": item["code"],
                "title": item["title"],
                "severity": item["severity"],
                "count": item["count"],
                "amount": str(item["amount"]),
                "is_waived": item["is_waived"],
            }
            for item in summary["checks"]
        ],
        "waivers": review.waived_blockers or {},
        "generated_at": timezone.now().isoformat(),
    }


def _find_issue(summary, code):
    for item in summary["issue_checks"]:
        if item["code"] == code:
            return item
    raise ValueError("Only active review issues can be waived.")


def _create_send_back_task(review, user, notes):
    reference = f"FREV:{review.pk}:SEND_BACK"
    description = notes.strip() or "Review sent back for correction before filing approval."
    task, was_created = PracticeTask.objects.get_or_create(
        company=review.company,
        reference=reference,
        defaults={
            "title": f"Resolve filing review send-back: {review.period_start:%b %Y}",
            "task_type": PracticeTask.TYPE_GST,
            "priority": PracticeTask.PRIORITY_HIGH,
            "status": PracticeTask.STATUS_OPEN,
            "due_date": timezone.localdate() + timedelta(days=2),
            "period_start": review.period_start,
            "period_end": review.period_end,
            "created_by": user,
            "description": description,
        },
    )
    if not was_created:
        task.status = PracticeTask.STATUS_OPEN
        task.description = description
        task.due_date = timezone.localdate() + timedelta(days=2)
        task.save(update_fields=["status", "description", "due_date", "updated_at"])
    return task


def _mark_related_filings_ready(review, user):
    if review.review_type != FilingReview.TYPE_GST_MONTHLY:
        return 0
    _ensure_gst_filings(review.company, review.period_start, review.period_end, user)
    filings = ComplianceFiling.objects.filter(
        company=review.company,
        filing_type__in=[
            ComplianceFiling.TYPE_GST_IMS,
            ComplianceFiling.TYPE_GSTR1,
            ComplianceFiling.TYPE_GSTR3B,
        ],
        period_start=review.period_start,
        period_end=review.period_end,
    ).exclude(status__in=[ComplianceFiling.STATUS_FILED, ComplianceFiling.STATUS_CANCELLED])

    updated = 0
    for filing in filings:
        filing.status = ComplianceFiling.STATUS_READY_FOR_REVIEW
        filing.reviewer = filing.reviewer or user
        filing.review_notes = _append_note(
            filing.review_notes,
            f"Approved for filing by {getattr(user, 'email', user)} through Review Center on {timezone.localtime().strftime('%Y-%m-%d %H:%M')}.",
        )
        filing.save(update_fields=["status", "reviewer", "review_notes", "updated_at"])
        sync_task_for_filing(filing, user=user)
        updated += 1
    return updated


def _ensure_gst_filings(company, period_start, period_end, user):
    templates = [
        (ComplianceFiling.TYPE_GST_IMS, "GST IMS Review", _period_due_date(period_start, 10)),
        (ComplianceFiling.TYPE_GSTR1, "GSTR-1", _period_due_date(period_start, 11)),
        (ComplianceFiling.TYPE_GSTR3B, "GSTR-3B", _period_due_date(period_start, 20)),
    ]
    for filing_type, label, due_date in templates:
        filing, was_created = ComplianceFiling.objects.get_or_create(
            company=company,
            filing_type=filing_type,
            period_start=period_start,
            period_end=period_end,
            defaults={
                "title": f"{label} - {period_start:%b %Y}",
                "status": ComplianceFiling.STATUS_NOT_STARTED,
                "priority": PracticeTask.PRIORITY_NORMAL,
                "due_date": due_date,
                "created_by": user,
                "source": ComplianceFiling.SOURCE_CALENDAR,
            },
        )
        if was_created:
            sync_task_for_filing(filing, user=user)


def _period_due_date(period_start, day, month_offset=1):
    due_month = _add_months(period_start, month_offset)
    return due_month.replace(day=min(day, calendar.monthrange(due_month.year, due_month.month)[1]))


def _add_months(value, months):
    month_index = (value.month - 1) + months
    year = value.year + (month_index // 12)
    month = (month_index % 12) + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _append_note(existing, note):
    existing = (existing or "").strip()
    if not existing:
        return note
    return f"{existing}\n\n{note}"
