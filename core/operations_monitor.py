from django.urls import reverse
from django.utils import timezone

from .evidence_vault import verify_vault_chain
from .models import AuditLog, Company, PracticeTask
from .production_trust import build_production_trust_context
from .security_control import build_security_control


OPERATIONS_TASK_PREFIX = "OPSMON:"


def build_operations_monitor(companies, *, current_company=None, include_deploy=False, as_of=None):
    as_of = as_of or timezone.now()
    company_list = list(companies)
    current_company = current_company or (company_list[0] if company_list else None)
    issues = []

    if current_company:
        _add_production_trust_issues(issues, current_company, include_deploy=include_deploy)
        _add_evidence_vault_issues(issues, current_company)
        _add_security_issues(issues, current_company)

    _add_provider_retry_issues(issues, company_list, as_of)
    _add_client_request_issues(issues, company_list, as_of)
    _add_task_sla_issues(issues, company_list, as_of)

    issues.sort(key=lambda issue: (issue["severity_rank"], issue["due_rank"], issue["company"].name.lower(), issue["title"]))
    totals = _totals(issues, company_list)
    score = max(0, 100 - (totals["critical"] * 12) - (totals["warning"] * 6) - min(15, totals["open_task_count"]))
    if score >= 90:
        status = "Stable"
        badge_class = "bg-success"
    elif score >= 70:
        status = "Watch"
        badge_class = "bg-warning text-dark"
    else:
        status = "Incident"
        badge_class = "bg-danger"

    return {
        "as_of": as_of,
        "current_company": current_company,
        "companies": company_list,
        "issues": issues,
        "taskable_issues": [issue for issue in issues if issue["taskable"]],
        "has_taskable_issues": any(issue["taskable"] for issue in issues),
        "score": score,
        "status": status,
        "badge_class": badge_class,
        "totals": totals,
        "summary": _summary(status, totals),
        "include_deploy": include_deploy,
    }


def create_operations_monitor_tasks(user, monitor):
    active_issues = monitor.get("taskable_issues", [])
    active_refs = {issue["reference"] for issue in active_issues}
    company_ids = {issue["company"].pk for issue in active_issues}
    created = 0
    updated = 0
    closed = 0

    for issue in active_issues:
        description = _task_description(issue)
        priority = PracticeTask.PRIORITY_CRITICAL if issue["severity"] == "critical" else PracticeTask.PRIORITY_HIGH
        task, was_created = PracticeTask.objects.get_or_create(
            company=issue["company"],
            reference=issue["reference"],
            defaults={
                "title": f"Operations Monitor: {issue['title']}",
                "task_type": issue["task_type"],
                "priority": priority,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": timezone.localdate() if issue["severity"] == "critical" else timezone.localdate() + timezone.timedelta(days=2),
                "assigned_to": user if getattr(user, "is_authenticated", False) else None,
                "created_by": user if getattr(user, "is_authenticated", False) else None,
                "description": description,
            },
        )
        if was_created:
            created += 1
            _audit_task(issue, user, task, "create")
            continue

        changed = False
        if task.status in {PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED}:
            task.status = PracticeTask.STATUS_OPEN
            task.completed_at = None
            task.completed_by = None
            changed = True
        if task.priority != priority:
            task.priority = priority
            changed = True
        if task.description != description:
            task.description = description
            changed = True
        if changed:
            task.save(update_fields=["status", "completed_at", "completed_by", "priority", "description", "updated_at"])
            updated += 1
            _audit_task(issue, user, task, "update")

    if company_ids:
        stale_tasks = (
            PracticeTask.objects.filter(company_id__in=company_ids, reference__startswith=OPERATIONS_TASK_PREFIX)
            .exclude(reference__in=active_refs)
            .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
        )
        for task in stale_tasks:
            old_status = task.status
            task.status = PracticeTask.STATUS_DONE
            task.completed_at = timezone.now()
            task.completed_by = user if getattr(user, "is_authenticated", False) else None
            task.description = f"{task.description}\n\nClosed because the Operations Monitor issue is no longer active.".strip()
            task.save(update_fields=["status", "completed_at", "completed_by", "description", "updated_at"])
            closed += 1
            AuditLog.objects.create(
                company=task.company,
                user=user if getattr(user, "is_authenticated", False) else None,
                action=AuditLog.ACTION_UPDATE,
                model_name="PracticeTask",
                record_id=task.pk,
                object_repr=task.title[:200],
                old_data={"status": old_status},
                new_data={"source": "operations_monitor", "status": task.status},
            )

    return {"created": created, "updated": updated, "closed": closed}


def _add_production_trust_issues(issues, company, *, include_deploy=False):
    trust = build_production_trust_context(include_deploy=include_deploy)
    for check in trust["checks"]:
        if check["level"] not in {"error", "warning"}:
            continue
        severity = "critical" if check["level"] == "error" else "warning"
        _add_issue(
            issues,
            company=company,
            area="Production Trust",
            code=f"preflight_{check['name']}",
            title=f"Preflight {check['name']} is {check['level']}",
            detail=check["message"],
            next_action=check.get("hint") or "Fix the deployment preflight issue and rerun checks.",
            severity=severity,
            task_type=PracticeTask.TYPE_AUDIT,
            action_url=reverse("core:production_trust_center"),
        )

    for source, watchdog in (("backup", trust["backup_policy"]), ("scheduled", trust["scheduled_backup"])):
        for issue in watchdog.get("issues", []):
            if issue.get("severity") not in {"critical", "warning"}:
                continue
            _add_issue(
                issues,
                company=company,
                area="Production Trust",
                code=f"{source}_{issue['code']}",
                title=issue["title"],
                detail=issue["detail"],
                next_action=issue["recommendation"],
                severity=issue["severity"],
                task_type=PracticeTask.TYPE_AUDIT,
                action_url=reverse("core:production_trust_center"),
            )


def _add_evidence_vault_issues(issues, company):
    verification = verify_vault_chain(company)
    if verification["status"] == "Empty":
        _add_issue(
            issues,
            company=company,
            area="Evidence Vault",
            code="evidence_vault_empty",
            title="Evidence Vault has not been sealed",
            detail="No immutable evidence ledger entries exist for the selected company.",
            next_action="Seal GST evidence, statutory exports, integration logs, and backup evidence into the Evidence Vault.",
            severity="warning",
            task_type=PracticeTask.TYPE_AUDIT,
            action_url=reverse("integrations:evidence_center"),
        )
        return

    for issue in verification.get("issues", [])[:12]:
        _add_issue(
            issues,
            company=company,
            area="Evidence Vault",
            code=f"evidence_vault_{issue['code']}_{issue['sequence']}",
            title="Evidence Vault verification issue",
            detail=issue["message"],
            next_action="Open Evidence Center, reseal current evidence, and investigate the affected ledger sequence.",
            severity=issue["severity"],
            task_type=PracticeTask.TYPE_AUDIT,
            action_url=reverse("integrations:evidence_center"),
        )


def _add_security_issues(issues, company):
    assessment = build_security_control(company)
    for issue in assessment.get("issues", []):
        if not issue.get("taskable") or issue.get("severity") not in {"critical", "warning"}:
            continue
        _add_issue(
            issues,
            company=company,
            area="Security",
            code=f"security_{issue['code']}",
            title=issue["title"],
            detail=issue["detail"],
            next_action=issue["recommendation"],
            severity=issue["severity"],
            task_type=PracticeTask.TYPE_AUDIT,
            action_url=reverse("core:security_control"),
        )


def _add_provider_retry_issues(issues, companies, as_of):
    from integrations.models import IntegrationRetryJob

    company_ids = [company.pk for company in companies]
    if not company_ids:
        return
    open_statuses = [
        IntegrationRetryJob.STATUS_PENDING,
        IntegrationRetryJob.STATUS_IN_PROGRESS,
        IntegrationRetryJob.STATUS_FAILED,
    ]
    jobs = (
        IntegrationRetryJob.objects.filter(company_id__in=company_ids, status__in=open_statuses)
        .select_related("company")
        .order_by("company__name", "status", "next_attempt_at")
    )
    by_company = {}
    for job in jobs:
        by_company.setdefault(job.company_id, []).append(job)

    for company in companies:
        rows = by_company.get(company.pk, [])
        if not rows:
            continue
        failed = [job for job in rows if job.status == IntegrationRetryJob.STATUS_FAILED]
        due = [job for job in rows if job.next_attempt_at <= as_of]
        severity = "critical" if failed or len(due) >= 3 else "warning"
        _add_issue(
            issues,
            company=company,
            area="Provider Retries",
            code="provider_retry_backlog",
            title="Provider retry queue needs attention",
            detail=f"{len(rows)} open retry job(s), {len(due)} due now, {len(failed)} failed.",
            next_action="Open Provider Go-Live Readiness, run due retries, and resolve or cancel stale provider jobs.",
            severity=severity,
            task_type=PracticeTask.TYPE_GST,
            action_url=reverse("integrations:provider_readiness"),
            count=len(rows),
            due_rank=0 if due else 1,
        )


def _add_client_request_issues(issues, companies, as_of):
    from portal.models import ClientDocumentRequest

    today = as_of.date()
    company_ids = [company.pk for company in companies]
    if not company_ids:
        return
    by_id = {company.pk: company for company in companies}
    overdue = (
        ClientDocumentRequest.objects.filter(
            company_id__in=company_ids,
            status=ClientDocumentRequest.STATUS_OPEN,
            due_date__lt=today,
        )
        .values("company_id")
        .order_by("company_id")
    )
    overdue_counts = {}
    for row in overdue:
        overdue_counts[row["company_id"]] = overdue_counts.get(row["company_id"], 0) + 1
    for company_id, count in overdue_counts.items():
        company = by_id[company_id]
        _add_issue(
            issues,
            company=company,
            area="Client Portal",
            code="client_requests_overdue",
            title="Client document requests are overdue",
            detail=f"{count} client upload request(s) are past due.",
            next_action="Open reminders, send email/WhatsApp follow-up, or escalate to the partner review cockpit.",
            severity="critical" if count >= 3 else "warning",
            task_type=PracticeTask.TYPE_DOCUMENT,
            action_url=f"{reverse('portal:client_request_reminders')}?company={company.pk}&kind=overdue",
            count=count,
        )

    stale_cutoff = as_of - timezone.timedelta(hours=48)
    stale_uploaded = (
        ClientDocumentRequest.objects.filter(
            company_id__in=company_ids,
            status=ClientDocumentRequest.STATUS_UPLOADED,
            uploaded_at__lt=stale_cutoff,
        )
        .values("company_id")
        .order_by("company_id")
    )
    stale_counts = {}
    for row in stale_uploaded:
        stale_counts[row["company_id"]] = stale_counts.get(row["company_id"], 0) + 1
    for company_id, count in stale_counts.items():
        company = by_id[company_id]
        _add_issue(
            issues,
            company=company,
            area="Client Portal",
            code="client_upload_review_stale",
            title="Client uploads are waiting for CA review",
            detail=f"{count} uploaded document request(s) have waited more than 48 hours.",
            next_action="Review uploaded evidence, close completed requests, and update linked work items.",
            severity="warning",
            task_type=PracticeTask.TYPE_DOCUMENT,
            action_url=f"{reverse('portal:client_requests')}?company={company.pk}&status=uploaded",
            count=count,
        )


def _add_task_sla_issues(issues, companies, as_of):
    today = as_of.date()
    company_ids = [company.pk for company in companies]
    if not company_ids:
        return
    by_id = {company.pk: company for company in companies}
    rows = (
        PracticeTask.objects.filter(company_id__in=company_ids, due_date__lt=today)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
        .exclude(reference__startswith=OPERATIONS_TASK_PREFIX)
        .values("company_id", "priority")
    )
    counts = {}
    critical_counts = {}
    for row in rows:
        counts[row["company_id"]] = counts.get(row["company_id"], 0) + 1
        if row["priority"] == PracticeTask.PRIORITY_CRITICAL:
            critical_counts[row["company_id"]] = critical_counts.get(row["company_id"], 0) + 1
    for company_id, count in counts.items():
        company = by_id[company_id]
        critical = critical_counts.get(company_id, 0)
        _add_issue(
            issues,
            company=company,
            area="Work Queue",
            code="task_sla_breach",
            title="Practice tasks are overdue",
            detail=f"{count} open task(s) are past due; {critical} are critical priority.",
            next_action="Open the work queue, reassign owners, and clear critical overdue tasks first.",
            severity="critical" if critical else "warning",
            task_type=PracticeTask.TYPE_OTHER,
            action_url=f"{reverse('core:practice_tasks')}?company={company.pk}&status=open",
            count=count,
        )


def _add_issue(
    issues,
    *,
    company,
    area,
    code,
    title,
    detail,
    next_action,
    severity,
    task_type,
    action_url,
    count=1,
    due_rank=1,
    taskable=True,
):
    clean_code = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in str(code).lower())[:70]
    issues.append({
        "company": company,
        "area": area,
        "code": clean_code,
        "title": title,
        "detail": detail,
        "next_action": next_action,
        "severity": severity,
        "severity_label": "Critical" if severity == "critical" else "Warning",
        "severity_rank": {"critical": 0, "warning": 1, "info": 2}.get(severity, 3),
        "badge_class": "bg-danger" if severity == "critical" else "bg-warning text-dark",
        "task_type": task_type,
        "action_url": action_url,
        "count": count,
        "due_rank": due_rank,
        "taskable": taskable,
        "reference": f"{OPERATIONS_TASK_PREFIX}{company.pk}:{clean_code}",
    })


def _totals(issues, companies):
    open_task_count = (
        PracticeTask.objects.filter(company__in=companies)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
        .count()
        if companies else 0
    )
    return {
        "company_count": len(companies),
        "issue_count": len(issues),
        "critical": sum(1 for issue in issues if issue["severity"] == "critical"),
        "warning": sum(1 for issue in issues if issue["severity"] == "warning"),
        "taskable": sum(1 for issue in issues if issue["taskable"]),
        "open_task_count": open_task_count,
        "impacted_companies": len({issue["company"].pk for issue in issues}),
    }


def _summary(status, totals):
    if status == "Stable":
        return "Operations are stable across production trust, provider retries, client requests, and task SLA."
    if totals["critical"]:
        return f"{totals['critical']} critical operations issue(s) need same-day action."
    return f"{totals['warning']} operations warning(s) need review before they become client-visible."


def _task_description(issue):
    return (
        f"Area: {issue['area']}\n"
        f"Company: {issue['company'].name}\n"
        f"Severity: {issue['severity_label']}\n"
        f"Detail: {issue['detail']}\n\n"
        f"Next action: {issue['next_action']}\n"
        f"Action URL: {issue['action_url']}"
    )


def _audit_task(issue, user, task, action):
    AuditLog.objects.create(
        company=issue["company"],
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_CREATE if action == "create" else AuditLog.ACTION_UPDATE,
        model_name="PracticeTask",
        record_id=task.pk,
        object_repr=task.title[:200],
        old_data={},
        new_data={
            "source": "operations_monitor",
            "area": issue["area"],
            "code": issue["code"],
            "severity": issue["severity"],
            "reference": task.reference,
        },
    )
