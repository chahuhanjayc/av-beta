import csv
from urllib.parse import urlencode

from django.db.models import Q
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from .models import Company, MarketProofExternalEvidence, PracticeTask, UserCompanyAccess


EXTERNAL_EVIDENCE_TASK_PREFIX = "EXTPROOF:"
REQUIRED_MARKET_EVIDENCE_CATEGORIES = (
    MarketProofExternalEvidence.CATEGORY_PROVIDER,
    MarketProofExternalEvidence.CATEGORY_PILOT,
    MarketProofExternalEvidence.CATEGORY_CASE_STUDY,
    MarketProofExternalEvidence.CATEGORY_STATUTORY,
    MarketProofExternalEvidence.CATEGORY_BACKUP,
    MarketProofExternalEvidence.CATEGORY_SECURITY,
)


def build_market_external_evidence_register(user, params=None):
    params = params or {}
    companies = list(_companies_for_user(user))
    manageable_company_ids = set(_manageable_companies_for_user(user).values_list("pk", flat=True))
    q = (params.get("q") or "").strip()
    company_filter = (params.get("company") or "all").strip()
    category_filter = (params.get("category") or "all").strip()
    status_filter = (params.get("status") or "open").strip()

    items = (
        MarketProofExternalEvidence.objects.filter(company__in=companies)
        .select_related("company", "owner", "verified_by", "created_by", "follow_up_task")
        .order_by("status", "due_date", "company__name", "category")
    )
    if q:
        items = items.filter(title__icontains=q) | items.filter(company__name__icontains=q) | items.filter(
            company__gstin__icontains=q
        ) | items.filter(evidence_reference__icontains=q) | items.filter(artifact_sha256__icontains=q)
    if company_filter != "all":
        try:
            items = items.filter(company_id=int(company_filter))
        except (TypeError, ValueError):
            company_filter = "all"
    if category_filter != "all":
        items = items.filter(category=category_filter)
    if status_filter == "open":
        items = items.filter(Q(expires_on__lt=timezone.localdate()) | ~Q(status=MarketProofExternalEvidence.STATUS_VERIFIED))
    elif status_filter != "all":
        items = items.filter(status=status_filter)

    evidence_items = list(items)
    rows = [
        {
            "item": item,
            "can_manage": item.company_id in manageable_company_ids,
            "missing_items": evidence_missing_items(item),
            "tasks_url": f"{reverse('core:practice_tasks')}?{urlencode({'company': item.company_id, 'status': 'open'})}",
        }
        for item in evidence_items
    ]
    company_signals = [build_external_evidence_signals(company) for company in companies]
    totals = {
        "items": len(rows),
        "verified": sum(1 for row in rows if row["item"].is_verified),
        "requested": sum(1 for row in rows if row["item"].status == MarketProofExternalEvidence.STATUS_REQUESTED),
        "received": sum(1 for row in rows if row["item"].status == MarketProofExternalEvidence.STATUS_RECEIVED),
        "rejected": sum(1 for row in rows if row["item"].status == MarketProofExternalEvidence.STATUS_REJECTED),
        "expired": sum(1 for row in rows if row["item"].is_expired or row["item"].status == MarketProofExternalEvidence.STATUS_EXPIRED),
        "missing_items": sum(len(row["missing_items"]) for row in rows),
        "companies_complete": sum(1 for signal in company_signals if signal["missing_required_count"] == 0),
        "companies_blocked": sum(1 for signal in company_signals if signal["missing_required_count"] > 0),
        "required_missing": sum(signal["missing_required_count"] for signal in company_signals),
        "manageable_clients": len(manageable_company_ids),
    }
    return {
        "rows": rows,
        "totals": totals,
        "q": q,
        "company_filter": company_filter,
        "category_filter": category_filter,
        "status_filter": status_filter,
        "company_options": companies,
        "category_options": [("all", "All Categories")] + list(MarketProofExternalEvidence.CATEGORY_CHOICES),
        "status_options": [("open", "Open / Unverified"), ("all", "All Statuses")] + list(MarketProofExternalEvidence.STATUS_CHOICES),
        "required_categories": required_category_rows(),
        "company_signals": company_signals,
        "export_query": market_external_evidence_filter_query(params),
    }


def market_external_evidence_filter_query(params):
    values = {
        "q": (params.get("q") or "").strip(),
        "company": (params.get("company") or "all").strip(),
        "category": (params.get("category") or "all").strip(),
        "status": (params.get("status") or "open").strip(),
    }
    defaults = {"q": "", "company": "all", "category": "all", "status": "open"}
    return urlencode({key: value for key, value in values.items() if value != defaults[key]})


def evidence_missing_items(item):
    missing = []
    if not item.evidence_reference.strip() and not item.artifact_sha256.strip() and not item.evidence_url.strip():
        missing.append("evidence reference, URL, or SHA-256")
    if item.status != MarketProofExternalEvidence.STATUS_VERIFIED:
        missing.append("verification")
    if item.is_expired or item.status == MarketProofExternalEvidence.STATUS_EXPIRED:
        missing.append("fresh evidence")
    return missing


def build_external_evidence_signals(company, *, today=None):
    today = today or timezone.localdate()
    items = list(MarketProofExternalEvidence.objects.filter(company=company).select_related("verified_by", "owner"))
    verified = [item for item in items if item.is_verified]
    verified_categories = {item.category for item in verified}
    missing_required = [category for category in REQUIRED_MARKET_EVIDENCE_CATEGORIES if category not in verified_categories]
    latest_verified = sorted(
        [item for item in verified if item.verified_at],
        key=lambda item: item.verified_at,
        reverse=True,
    )
    due_items = [
        item
        for item in items
        if not item.is_verified and item.due_date and item.due_date <= today + timezone.timedelta(days=7)
    ]
    completion_score = round(((len(REQUIRED_MARKET_EVIDENCE_CATEGORIES) - len(missing_required)) / len(REQUIRED_MARKET_EVIDENCE_CATEGORIES)) * 100)
    return {
        "evidence_count": len(items),
        "verified_count": len(verified),
        "verified_categories": sorted(verified_categories),
        "verified_category_count": len(verified_categories),
        "required_category_count": len(REQUIRED_MARKET_EVIDENCE_CATEGORIES),
        "missing_required_categories": missing_required,
        "missing_required_labels": [category_label(category) for category in missing_required],
        "missing_required_count": len(missing_required),
        "completion_score": completion_score,
        "received_unverified_count": sum(1 for item in items if item.status == MarketProofExternalEvidence.STATUS_RECEIVED),
        "rejected_count": sum(1 for item in items if item.status == MarketProofExternalEvidence.STATUS_REJECTED),
        "expired_count": sum(1 for item in items if item.is_expired or item.status == MarketProofExternalEvidence.STATUS_EXPIRED),
        "open_count": sum(1 for item in items if not item.is_verified),
        "due_soon_count": len(due_items),
        "latest_verified_at": latest_verified[0].verified_at.isoformat() if latest_verified else "",
        "latest_verified_title": latest_verified[0].title if latest_verified else "",
        "complete": len(missing_required) == 0,
    }


def required_category_rows():
    verified_target = "Required for Market Ready"
    return [
        {
            "category": category,
            "label": category_label(category),
            "target": verified_target,
        }
        for category in REQUIRED_MARKET_EVIDENCE_CATEGORIES
    ]


def category_label(category):
    return dict(MarketProofExternalEvidence.CATEGORY_CHOICES).get(category, category)


def create_external_evidence_follow_up(item, user):
    if item.is_verified:
        return None, False
    reference = f"{EXTERNAL_EVIDENCE_TASK_PREFIX}{item.pk}"
    task = (
        PracticeTask.objects.filter(company=item.company, reference=reference)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
        .first()
    )
    if task:
        if not item.follow_up_task_id:
            item.follow_up_task = task
            item.save(update_fields=["follow_up_task", "updated_at"])
        return task, False
    task = PracticeTask.objects.create(
        company=item.company,
        title=f"External market proof: {item.get_category_display()}",
        task_type=_task_type_for_category(item.category),
        priority=PracticeTask.PRIORITY_CRITICAL if item.category in REQUIRED_MARKET_EVIDENCE_CATEGORIES else PracticeTask.PRIORITY_HIGH,
        status=PracticeTask.STATUS_OPEN,
        due_date=item.due_date or timezone.localdate() + timezone.timedelta(days=3),
        assigned_to=item.owner or user,
        created_by=user,
        reference=reference,
        description=(
            f"Complete and verify external evidence for {item.company.name}.\n"
            f"Category: {item.get_category_display()}\n"
            f"Status: {item.get_status_display()}\n"
            f"Evidence: {item.evidence_reference or item.evidence_url or item.artifact_sha256 or '-'}\n"
            f"Notes: {item.notes or '-'}"
        ),
    )
    item.follow_up_task = task
    item.save(update_fields=["follow_up_task", "updated_at"])
    return task, True


def verify_external_evidence(item, user):
    item.status = MarketProofExternalEvidence.STATUS_VERIFIED
    item.verified_by = user
    item.verified_at = timezone.now()
    item.save(update_fields=["status", "verified_by", "verified_at", "updated_at"])
    _close_follow_up_task(item, user)
    return item


def reopen_external_evidence(item):
    item.status = MarketProofExternalEvidence.STATUS_RECEIVED
    item.verified_by = None
    item.verified_at = None
    item.save(update_fields=["status", "verified_by", "verified_at", "updated_at"])
    return item


def reject_external_evidence(item, user):
    item.status = MarketProofExternalEvidence.STATUS_REJECTED
    item.verified_by = None
    item.verified_at = None
    item.save(update_fields=["status", "verified_by", "verified_at", "updated_at"])
    create_external_evidence_follow_up(item, user)
    return item


def market_external_evidence_csv_response(rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="market-external-evidence.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "GSTIN",
        "Category",
        "Status",
        "Source",
        "Title",
        "Evidence Reference",
        "Artifact SHA-256",
        "Evidence URL",
        "Due Date",
        "Expires On",
        "Verified By",
        "Verified At",
        "Missing Items",
    ])
    for row in rows:
        item = row["item"]
        writer.writerow([
            item.company.name,
            item.company.gstin or "",
            item.get_category_display(),
            item.get_status_display(),
            item.get_source_display(),
            item.title,
            item.evidence_reference,
            item.artifact_sha256,
            item.evidence_url,
            item.due_date.isoformat() if item.due_date else "",
            item.expires_on.isoformat() if item.expires_on else "",
            getattr(item.verified_by, "email", "") if item.verified_by else "",
            item.verified_at.isoformat() if item.verified_at else "",
            "; ".join(row["missing_items"]),
        ])
    return response


def _close_follow_up_task(item, user):
    if not item.follow_up_task_id:
        return False
    task = item.follow_up_task
    if task.status in {PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED}:
        return False
    task.status = PracticeTask.STATUS_DONE
    task.completed_by = user
    task.completed_at = timezone.now()
    task.description = f"{task.description}\n\nClosed because external market evidence was verified.".strip()
    task.save(update_fields=["status", "completed_by", "completed_at", "description", "updated_at"])
    return True


def _task_type_for_category(category):
    if category in {MarketProofExternalEvidence.CATEGORY_PROVIDER, MarketProofExternalEvidence.CATEGORY_STATUTORY}:
        return PracticeTask.TYPE_GST
    if category in {MarketProofExternalEvidence.CATEGORY_BACKUP, MarketProofExternalEvidence.CATEGORY_SECURITY}:
        return PracticeTask.TYPE_AUDIT
    if category == MarketProofExternalEvidence.CATEGORY_PILOT:
        return PracticeTask.TYPE_DOCUMENT
    return PracticeTask.TYPE_OTHER


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
