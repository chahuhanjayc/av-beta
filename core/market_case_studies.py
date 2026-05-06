import csv
from urllib.parse import urlencode

from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from .models import Company, MarketProofCaseStudy, PracticeTask, UserCompanyAccess


CASE_STUDY_TASK_PREFIX = "CASEPROOF:"


def build_market_case_study_register(user, params=None):
    params = params or {}
    companies = _companies_for_user(user)
    manageable_company_ids = set(_manageable_companies_for_user(user).values_list("pk", flat=True))
    q = (params.get("q") or "").strip()
    company_filter = (params.get("company") or "all").strip()
    status_filter = (params.get("status") or "active").strip()
    outcome_filter = (params.get("outcome") or "all").strip()
    source_filter = (params.get("source") or "all").strip()

    items = (
        MarketProofCaseStudy.objects.filter(company__in=companies)
        .select_related("company", "owner", "approved_by", "created_by")
        .order_by("status", "-updated_at", "company__name")
    )
    if q:
        items = items.filter(
            company__name__icontains=q
        ) | items.filter(
            company__gstin__icontains=q
        ) | items.filter(
            title__icontains=q
        ) | items.filter(
            testimonial_quote__icontains=q
        ) | items.filter(
            evidence_reference__icontains=q
        ) | items.filter(
            consent_reference__icontains=q
        )
    if company_filter != "all":
        try:
            items = items.filter(company_id=int(company_filter))
        except (TypeError, ValueError):
            company_filter = "all"
    if status_filter == "active":
        items = items.exclude(status__in=[MarketProofCaseStudy.STATUS_PUBLISHED, MarketProofCaseStudy.STATUS_ON_HOLD])
    elif status_filter != "all":
        items = items.filter(status=status_filter)
    if outcome_filter != "all":
        items = items.filter(outcome=outcome_filter)
    if source_filter != "all":
        items = items.filter(migration_source=source_filter)

    case_studies = list(items)
    rows = [
        {
            "case_study": item,
            "can_manage": item.company_id in manageable_company_ids,
            "missing_items": case_study_missing_items(item),
            "tasks_url": f"{reverse('core:practice_tasks')}?{urlencode({'company': item.company_id, 'status': 'open'})}",
        }
        for item in case_studies
    ]
    totals = {
        "items": len(rows),
        "publishable": sum(1 for row in rows if row["case_study"].is_publishable),
        "approved": sum(1 for row in rows if row["case_study"].is_approved),
        "published": sum(1 for row in rows if row["case_study"].status == MarketProofCaseStudy.STATUS_PUBLISHED),
        "consented": sum(1 for row in rows if row["case_study"].publish_consent),
        "with_quote": sum(1 for row in rows if row["case_study"].testimonial_quote.strip()),
        "with_metrics": sum(1 for row in rows if row["case_study"].has_metric_proof),
        "tally_replacements": sum(1 for row in rows if row["case_study"].migration_source == MarketProofCaseStudy.SOURCE_TALLY),
        "converted": sum(
            1
            for row in rows
            if row["case_study"].outcome in {
                MarketProofCaseStudy.OUTCOME_CONVERTED,
                MarketProofCaseStudy.OUTCOME_PAID,
                MarketProofCaseStudy.OUTCOME_EXPANDED,
            }
        ),
        "missing_total": sum(len(row["missing_items"]) for row in rows),
        "manageable_clients": len(manageable_company_ids),
    }
    return {
        "rows": rows,
        "totals": totals,
        "q": q,
        "company_filter": company_filter,
        "status_filter": status_filter,
        "outcome_filter": outcome_filter,
        "source_filter": source_filter,
        "company_options": companies,
        "status_options": [("active", "Active")] + [("all", "All Statuses")] + MarketProofCaseStudy.STATUS_CHOICES,
        "outcome_options": [("all", "All Outcomes")] + MarketProofCaseStudy.OUTCOME_CHOICES,
        "source_options": [("all", "All Sources")] + MarketProofCaseStudy.SOURCE_CHOICES,
        "export_query": market_case_study_filter_query(params),
    }


def market_case_study_filter_query(params):
    values = {
        "q": (params.get("q") or "").strip(),
        "company": (params.get("company") or "all").strip(),
        "status": (params.get("status") or "active").strip(),
        "outcome": (params.get("outcome") or "all").strip(),
        "source": (params.get("source") or "all").strip(),
    }
    defaults = {"q": "", "company": "all", "status": "active", "outcome": "all", "source": "all"}
    return urlencode({key: value for key, value in values.items() if value != defaults[key]})


def case_study_missing_items(case_study):
    missing = []
    if not case_study.testimonial_quote.strip():
        missing.append("testimonial quote")
    if not case_study.publish_consent:
        missing.append("publish consent")
    if not case_study.consent_reference.strip():
        missing.append("consent reference")
    if not case_study.evidence_reference.strip():
        missing.append("evidence reference")
    if not case_study.has_metric_proof:
        missing.append("measurable result")
    if not case_study.is_approved:
        missing.append("approval")
    if case_study.migration_source == MarketProofCaseStudy.SOURCE_TALLY and not case_study.tally_parallel_run_days:
        missing.append("Tally parallel-run days")
    return missing


def build_case_study_signals(company):
    items = MarketProofCaseStudy.objects.filter(company=company)
    case_studies = list(items)
    publishable = [item for item in case_studies if item.is_publishable]
    approved = [item for item in case_studies if item.is_approved]
    consented = [item for item in case_studies if item.publish_consent]
    with_metrics = [item for item in case_studies if item.has_metric_proof]
    converted = [
        item
        for item in case_studies
        if item.outcome in {
            MarketProofCaseStudy.OUTCOME_CONVERTED,
            MarketProofCaseStudy.OUTCOME_PAID,
            MarketProofCaseStudy.OUTCOME_EXPANDED,
        }
    ]
    latest = sorted(case_studies, key=lambda item: item.updated_at, reverse=True)[0] if case_studies else None
    return {
        "case_study_count": len(case_studies),
        "publishable_count": len(publishable),
        "approved_count": len(approved),
        "consented_count": len(consented),
        "with_metrics_count": len(with_metrics),
        "converted_count": len(converted),
        "tally_replacement_count": sum(1 for item in case_studies if item.migration_source == MarketProofCaseStudy.SOURCE_TALLY),
        "latest_title": latest.title if latest else "",
        "missing_item_count": sum(len(case_study_missing_items(item)) for item in case_studies),
    }


def create_case_study_follow_up(case_study, user):
    missing = case_study_missing_items(case_study)
    if not missing:
        return None, False
    task = (
        PracticeTask.objects.filter(company=case_study.company, reference=f"{CASE_STUDY_TASK_PREFIX}{case_study.pk}")
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
        .first()
    )
    if task:
        return task, False
    task = PracticeTask.objects.create(
        company=case_study.company,
        title=f"Market case study proof: {case_study.title[:120]}",
        task_type=PracticeTask.TYPE_OTHER,
        priority=PracticeTask.PRIORITY_HIGH,
        status=PracticeTask.STATUS_OPEN,
        due_date=timezone.localdate() + timezone.timedelta(days=3),
        assigned_to=case_study.owner or user,
        created_by=user,
        reference=f"{CASE_STUDY_TASK_PREFIX}{case_study.pk}",
        description=(
            f"Complete missing case-study proof for {case_study.company.name}.\n"
            f"Missing: {', '.join(missing)}\n"
            f"Outcome: {case_study.get_outcome_display()}\n"
            f"Source: {case_study.get_migration_source_display()}"
        ),
    )
    return task, True


def approve_case_study(case_study, user):
    case_study.status = MarketProofCaseStudy.STATUS_APPROVED
    case_study.approved_by = user
    case_study.approved_at = timezone.now()
    case_study.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    _close_case_study_task_if_publishable(case_study, user)
    return case_study


def publish_case_study(case_study, user):
    case_study.status = MarketProofCaseStudy.STATUS_PUBLISHED
    case_study.approved_by = case_study.approved_by or user
    case_study.approved_at = case_study.approved_at or timezone.now()
    case_study.published_at = timezone.now()
    case_study.save(update_fields=["status", "approved_by", "approved_at", "published_at", "updated_at"])
    _close_case_study_task_if_publishable(case_study, user)
    return case_study


def market_case_study_csv_response(rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="market-case-studies.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "GSTIN",
        "Title",
        "Status",
        "Outcome",
        "Replaced Tool",
        "Publishable",
        "Publish Consent",
        "Consent Reference",
        "Evidence Reference",
        "Before Hours",
        "After Hours",
        "Hours Saved",
        "Monthly Documents",
        "Monthly Invoices",
        "GST Periods",
        "Tally Parallel Run Days",
        "Commercial Value",
        "Missing Items",
    ])
    for row in rows:
        item = row["case_study"]
        writer.writerow([
            item.company.name,
            item.company.gstin or "",
            item.title,
            item.get_status_display(),
            item.get_outcome_display(),
            item.get_migration_source_display(),
            "Yes" if item.is_publishable else "No",
            "Yes" if item.publish_consent else "No",
            item.consent_reference,
            item.evidence_reference,
            item.before_process_hours if item.before_process_hours is not None else "",
            item.after_process_hours if item.after_process_hours is not None else "",
            item.hours_saved if item.hours_saved is not None else "",
            item.monthly_documents,
            item.monthly_invoices,
            item.gst_periods_completed,
            item.tally_parallel_run_days,
            item.commercial_value,
            "; ".join(row["missing_items"]),
        ])
    return response


def _close_case_study_task_if_publishable(case_study, user):
    if not case_study.is_publishable:
        return 0
    tasks = (
        PracticeTask.objects.filter(company=case_study.company, reference=f"{CASE_STUDY_TASK_PREFIX}{case_study.pk}")
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
    )
    closed = 0
    for task in tasks:
        task.status = PracticeTask.STATUS_DONE
        task.completed_by = user
        task.completed_at = timezone.now()
        task.save(update_fields=["status", "completed_by", "completed_at", "updated_at"])
        closed += 1
    return closed


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
