from django.db import transaction
from django.utils import timezone

from core.models import AuditLog, PracticeTask

from .models import IntegrationConnector, IntegrationRequestLog, IntegrationRetryJob
from .readiness import CONNECTOR_CATALOG, CONNECTOR_SERVICE_MAP, PRODUCTION_EVIDENCE_FIELDS, connector_production_evidence


PROVIDER_READY_TASK_PREFIX = "PROVIDERREADY:"
STATUTORY_CONNECTOR_TYPES = (
    IntegrationConnector.TYPE_GST,
    IntegrationConnector.TYPE_IRP,
    IntegrationConnector.TYPE_EWAY,
    IntegrationConnector.TYPE_TRACES,
)
RETRYABLE_SERVICES = (
    IntegrationRequestLog.SERVICE_GST_RETURN,
    IntegrationRequestLog.SERVICE_E_INVOICE,
    IntegrationRequestLog.SERVICE_E_WAY_BILL,
    IntegrationRequestLog.SERVICE_TRACES,
)
OPEN_RETRY_STATUSES = (
    IntegrationRetryJob.STATUS_PENDING,
    IntegrationRetryJob.STATUS_IN_PROGRESS,
    IntegrationRetryJob.STATUS_FAILED,
)


def build_provider_go_live_readiness(company, *, as_of=None):
    as_of = as_of or timezone.now()
    if not company:
        return _empty_readiness(as_of)

    catalog = [item for item in CONNECTOR_CATALOG if item["type"] in STATUTORY_CONNECTOR_TYPES]
    services = [CONNECTOR_SERVICE_MAP[item["type"]] for item in catalog]
    connectors = {
        connector.connector_type: connector
        for connector in IntegrationConnector.objects.filter(company=company, connector_type__in=STATUTORY_CONNECTOR_TYPES)
    }
    logs = list(
        IntegrationRequestLog.objects.filter(company=company, service__in=services)
        .select_related("voucher", "requested_by")
        .order_by("-created_at", "-id")[:200]
    )
    retry_jobs = list(
        IntegrationRetryJob.objects.filter(company=company)
        .select_related("connector", "request_log", "voucher", "created_by", "resolved_by")
        .order_by("status", "next_attempt_at", "-created_at")[:50]
    )

    logs_by_service = {}
    for log in logs:
        logs_by_service.setdefault(log.service, []).append(log)

    open_retry_counts = {service: 0 for service in services}
    failed_retry_counts = {service: 0 for service in services}
    retry_by_log_id = {}
    for job in retry_jobs:
        if job.request_log_id:
            retry_by_log_id[job.request_log_id] = job
        if job.status in OPEN_RETRY_STATUSES:
            open_retry_counts[job.service] = open_retry_counts.get(job.service, 0) + 1
        if job.status == IntegrationRetryJob.STATUS_FAILED:
            failed_retry_counts[job.service] = failed_retry_counts.get(job.service, 0) + 1

    connector_rows = []
    taskable_issues = []
    for item in catalog:
        service = CONNECTOR_SERVICE_MAP[item["type"]]
        row = _provider_connector_row(
            company,
            item,
            connectors.get(item["type"]),
            logs_by_service.get(service, []),
            open_retry_counts.get(service, 0),
            failed_retry_counts.get(service, 0),
            as_of,
        )
        connector_rows.append(row)
        taskable_issues.extend(row["taskable_issues"])

    recent_failures = _recent_failure_logs(logs, retry_by_log_id, as_of)
    open_jobs = [job for job in retry_jobs if job.status in OPEN_RETRY_STATUSES]
    due_jobs = [job for job in open_jobs if job.next_attempt_at <= as_of]
    failed_jobs = [job for job in open_jobs if job.status == IntegrationRetryJob.STATUS_FAILED]
    score = _overall_score(connector_rows, open_jobs, failed_jobs, recent_failures)

    return {
        "company": company,
        "as_of": as_of,
        "score": score,
        "status": _overall_status(score, connector_rows, failed_jobs),
        "status_label": _overall_status_label(score, connector_rows, failed_jobs),
        "connector_rows": connector_rows,
        "taskable_issues": taskable_issues,
        "has_taskable_issues": bool(taskable_issues),
        "recent_failures": recent_failures,
        "retry_jobs": retry_jobs,
        "open_retry_jobs": open_jobs,
        "retry_summary": {
            "open": len(open_jobs),
            "due": len(due_jobs),
            "failed": len(failed_jobs),
            "queued": sum(1 for job in retry_jobs if job.status == IntegrationRetryJob.STATUS_PENDING),
            "resolved": sum(1 for job in retry_jobs if job.status == IntegrationRetryJob.STATUS_RESOLVED),
        },
        "totals": {
            "connectors": len(connector_rows),
            "ready": sum(1 for row in connector_rows if row["state"] == "ready"),
            "warning": sum(1 for row in connector_rows if row["state"] == "warning"),
            "critical": sum(1 for row in connector_rows if row["state"] == "critical"),
            "checks": sum(len(row["checks"]) for row in connector_rows),
            "critical_checks": sum(row["critical_count"] for row in connector_rows),
            "warning_checks": sum(row["warning_count"] for row in connector_rows),
        },
        "certification": {
            "required": sum(1 for row in connector_rows if row["certification"]["required"]),
            "ready": sum(1 for row in connector_rows if row["certification"]["required"] and row["certification"]["missing_count"] == 0),
            "missing": sum(row["certification"]["missing_count"] for row in connector_rows),
        },
        "summary": _readiness_summary(score, connector_rows, open_jobs, recent_failures),
    }


@transaction.atomic
def queue_failed_provider_requests(company, user, *, days=7):
    since = timezone.now() - timezone.timedelta(days=days)
    logs = (
        IntegrationRequestLog.objects.filter(
            company=company,
            service__in=RETRYABLE_SERVICES,
            status__in=[IntegrationRequestLog.STATUS_FAILED, IntegrationRequestLog.STATUS_CONFIG_ERROR],
            created_at__gte=since,
        )
        .select_related("voucher")
        .order_by("-created_at", "-id")
    )
    connectors = {
        connector.connector_type: connector
        for connector in IntegrationConnector.objects.filter(company=company, connector_type__in=STATUTORY_CONNECTOR_TYPES)
    }

    created = 0
    existing = 0
    skipped = 0
    now = timezone.now()
    for log in logs:
        connector = connectors.get(_connector_type_for_service(log.service))
        defaults = {
            "company": company,
            "connector": connector,
            "voucher": log.voucher,
            "service": log.service,
            "provider": log.provider,
            "status": IntegrationRetryJob.STATUS_PENDING,
            "priority": _retry_priority(log),
            "attempts": 0,
            "max_attempts": 3,
            "next_attempt_at": now,
            "last_error": log.error_message,
            "response_payload": log.response_payload or {},
            "created_by": user,
        }
        job, was_created = IntegrationRetryJob.objects.get_or_create(request_log=log, defaults=defaults)
        if was_created:
            created += 1
            AuditLog.objects.create(
                company=company,
                user=user,
                action=AuditLog.ACTION_CREATE,
                model_name="IntegrationRetryJob",
                record_id=job.pk,
                object_repr=str(job)[:200],
                old_data={},
                new_data={
                    "source": "provider_go_live_readiness",
                    "service": job.service,
                    "provider": job.provider,
                    "request_log_id": log.pk,
                    "status": job.status,
                },
            )
        elif job.status in {IntegrationRetryJob.STATUS_RESOLVED, IntegrationRetryJob.STATUS_CANCELLED}:
            skipped += 1
        else:
            existing += 1
    return {"created": created, "existing": existing, "skipped": skipped}


@transaction.atomic
def resolve_retry_job(job, user, *, status=IntegrationRetryJob.STATUS_RESOLVED, note=""):
    if status not in {IntegrationRetryJob.STATUS_RESOLVED, IntegrationRetryJob.STATUS_CANCELLED}:
        raise ValueError("Retry job can only be resolved or cancelled from this action.")

    old_data = _retry_job_snapshot(job)
    job.status = status
    job.resolved_by = user
    job.resolved_at = timezone.now()
    if note:
        prefix = "Resolution note" if status == IntegrationRetryJob.STATUS_RESOLVED else "Cancellation note"
        job.last_error = f"{job.last_error}\n{prefix}: {note}".strip()
    job.save(update_fields=["status", "resolved_by", "resolved_at", "last_error", "updated_at"])

    AuditLog.objects.create(
        company=job.company,
        user=user,
        action=AuditLog.ACTION_UPDATE,
        model_name="IntegrationRetryJob",
        record_id=job.pk,
        object_repr=str(job)[:200],
        old_data=old_data,
        new_data=_retry_job_snapshot(job) | {"source": "provider_go_live_readiness"},
    )
    return job


@transaction.atomic
def create_provider_readiness_tasks(company, user, assessment):
    created = 0
    existing = 0
    closed = 0
    active_refs = set()
    today = timezone.localdate()

    for issue in assessment.get("taskable_issues", []):
        active_refs.add(issue["task_reference"])
        task, was_created = PracticeTask.objects.get_or_create(
            company=company,
            reference=issue["task_reference"],
            defaults={
                "title": f"Provider readiness: {issue['connector_name']} {issue['name']}",
                "task_type": _task_type_for_connector(issue["connector_type"]),
                "priority": PracticeTask.PRIORITY_CRITICAL if issue["level"] == "critical" else PracticeTask.PRIORITY_HIGH,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": today,
                "created_by": user,
                "description": _provider_task_description(company, issue),
            },
        )
        if was_created:
            created += 1
            AuditLog.objects.create(
                company=company,
                user=user,
                action=AuditLog.ACTION_CREATE,
                model_name="PracticeTask",
                record_id=task.pk,
                object_repr=task.title[:200],
                old_data={},
                new_data={
                    "source": "provider_go_live_readiness",
                    "connector_type": issue["connector_type"],
                    "gate": issue["code"],
                    "reference": task.reference,
                },
            )
        else:
            existing += 1

    stale_tasks = PracticeTask.objects.filter(
        company=company,
        reference__startswith=f"{PROVIDER_READY_TASK_PREFIX}{company.pk}:",
    ).exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
    if active_refs:
        stale_tasks = stale_tasks.exclude(reference__in=active_refs)

    now = timezone.now()
    for task in stale_tasks:
        old_status = task.status
        task.status = PracticeTask.STATUS_DONE
        task.completed_by = user
        task.completed_at = now
        task.description = f"{task.description}\n\nClosed because the provider readiness gate is now clear.".strip()
        task.save(update_fields=["status", "completed_by", "completed_at", "description", "updated_at"])
        closed += 1
        AuditLog.objects.create(
            company=company,
            user=user,
            action=AuditLog.ACTION_UPDATE,
            model_name="PracticeTask",
            record_id=task.pk,
            object_repr=task.title[:200],
            old_data={"status": old_status},
            new_data={
                "status": task.status,
                "source": "provider_go_live_readiness_auto_close",
                "reference": task.reference,
            },
        )

    return {"created": created, "existing": existing, "closed": closed}


def _empty_readiness(as_of):
    return {
        "company": None,
        "as_of": as_of,
        "score": 0,
        "status": "blocked",
        "status_label": "Select Company",
        "connector_rows": [],
        "taskable_issues": [],
        "has_taskable_issues": False,
        "recent_failures": [],
        "retry_jobs": [],
        "open_retry_jobs": [],
        "retry_summary": {"open": 0, "due": 0, "failed": 0, "queued": 0, "resolved": 0},
        "totals": {"connectors": 0, "ready": 0, "warning": 0, "critical": 0, "checks": 0, "critical_checks": 0, "warning_checks": 0},
        "certification": {"required": 0, "ready": 0, "missing": 0},
        "summary": "Select a company to review provider go-live readiness.",
    }


def _provider_connector_row(company, item, connector, logs, open_retry_count, failed_retry_count, as_of):
    service = CONNECTOR_SERVICE_MAP[item["type"]]
    checks = []
    latest_log = logs[0] if logs else None
    latest_success = next((log for log in logs if log.status == IntegrationRequestLog.STATUS_SUCCESS), None)
    latest_failure = next(
        (
            log for log in logs
            if log.status in {IntegrationRequestLog.STATUS_FAILED, IntegrationRequestLog.STATUS_CONFIG_ERROR}
        ),
        None,
    )
    success_last_30 = bool(latest_success and latest_success.created_at >= as_of - timezone.timedelta(days=30))
    success_last_7 = bool(latest_success and latest_success.created_at >= as_of - timezone.timedelta(days=7))
    failed_after_success = bool(
        latest_failure and (not latest_success or latest_failure.created_at >= latest_success.created_at)
    )

    missing_fields = [
        field.replace("_", " ").title()
        for field in item["required"]
        if not connector or not getattr(connector, field)
    ]

    _add_gate(
        checks,
        "configured",
        "Connector configured",
        bool(connector),
        "critical",
        "Create the connector with provider, credentials, and operating mode.",
    )
    if connector:
        _add_gate(
            checks,
            "enabled",
            "Connector enabled",
            connector.status not in {IntegrationConnector.STATUS_DISABLED, IntegrationConnector.STATUS_BLOCKED},
            "critical",
            connector.last_error or "Move the connector out of Disabled/Blocked after fixing the provider issue.",
        )
        _add_gate(
            checks,
            "ready_status",
            "Ready or live status",
            connector.status in {IntegrationConnector.STATUS_READY, IntegrationConnector.STATUS_LIVE},
            "warning",
            "Mark the connector Ready after setup, or Live after production evidence is available.",
        )
        _add_gate(
            checks,
            "required_identity",
            "Client identity complete",
            not missing_fields,
            "critical",
            f"Complete missing field(s): {', '.join(missing_fields)}.",
        )
        if item["type"] in {IntegrationConnector.TYPE_GST, IntegrationConnector.TYPE_IRP, IntegrationConnector.TYPE_EWAY}:
            company_gstin = (company.gstin or "").strip().upper()
            connector_gstin = (connector.gstin or "").strip().upper()
            _add_gate(
                checks,
                "gstin_match",
                "GSTIN matches client",
                bool(company_gstin and connector_gstin and company_gstin == connector_gstin),
                "critical",
                "Use the same GSTIN as Company Settings before production provider calls.",
            )
            if connector.mode in {IntegrationConnector.MODE_SANDBOX, IntegrationConnector.MODE_PRODUCTION}:
                _add_gate(
                    checks,
                    "endpoint",
                    "Provider endpoint recorded",
                    bool(connector.base_url),
                    "warning",
                    "Record the provider base URL used for sandbox or production calls.",
                )
        _add_gate(
            checks,
            "production_mode",
            "Production mode selected",
            connector.mode == IntegrationConnector.MODE_PRODUCTION and connector.status == IntegrationConnector.STATUS_LIVE,
            "warning",
            "Move from sandbox/manual to Production + Live only after sandbox evidence and client approval.",
        )
        _credential_gates(checks, connector, as_of)
        certification = _certification_evidence_gates(checks, connector)
        _add_gate(
            checks,
            "sandbox_success",
            "Recent sandbox/provider success",
            success_last_30 or bool(connector.last_success_at and connector.last_success_at >= as_of - timezone.timedelta(days=30)),
            "warning",
            "Run a sandbox or portal/provider test and keep the success log.",
        )
        if connector.mode == IntegrationConnector.MODE_PRODUCTION and connector.status == IntegrationConnector.STATUS_LIVE:
            _add_gate(
                checks,
                "production_heartbeat",
                "Production heartbeat",
                success_last_7 or bool(connector.last_success_at and connector.last_success_at >= as_of - timezone.timedelta(days=7)),
                "warning",
                "Run a production status check or sync within the last 7 days.",
            )
        _add_gate(
            checks,
            "latest_failure_clear",
            "Latest failure reconciled",
            not failed_after_success,
            "critical",
            latest_failure.error_message if latest_failure and latest_failure.error_message else "Resolve the latest provider failure and rerun.",
        )
        _add_gate(
            checks,
            "retry_queue_clear",
            "Retry queue clear",
            failed_retry_count == 0 and open_retry_count == 0,
            "warning",
            "Resolve or cancel open retry jobs for this provider service.",
        )
    else:
        certification = _empty_certification_evidence()

    critical_count = sum(1 for check in checks if check["level"] == "critical")
    warning_count = sum(1 for check in checks if check["level"] == "warning")
    state = "critical" if critical_count else "warning" if warning_count else "ready"
    score = max(0, 100 - (critical_count * 20) - (warning_count * 7))
    taskable_issues = []
    for check in checks:
        if check["level"] not in {"critical", "warning"}:
            continue
        task_reference = f"{PROVIDER_READY_TASK_PREFIX}{company.pk}:{item['type']}:{check['code']}"
        taskable_issues.append({
            "connector_type": item["type"],
            "connector_name": item["name"],
            "code": check["code"],
            "name": check["name"],
            "level": check["level"],
            "message": check["message"],
            "fix": check["fix"],
            "task_reference": task_reference,
        })

    return {
        "type": item["type"],
        "name": item["name"],
        "purpose": item["purpose"],
        "service": service,
        "connector": connector,
        "checks": checks,
        "taskable_issues": taskable_issues,
        "state": state,
        "score": score,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "badge_class": {"critical": "bg-danger", "warning": "bg-warning text-dark", "ready": "bg-success"}[state],
        "state_label": {"critical": "Blocked", "warning": "Needs Evidence", "ready": "Go-Live Ready"}[state],
        "latest_log": latest_log,
        "latest_success": latest_success,
        "latest_failure": latest_failure,
        "open_retry_count": open_retry_count,
        "failed_retry_count": failed_retry_count,
        "certification": certification,
    }


def _credential_gates(checks, connector, as_of):
    _add_gate(
        checks,
        "credential_reference",
        "Credential reference stored",
        bool(connector.credential_reference),
        "critical",
        "Store a vault/env reference only. Do not store raw provider passwords.",
    )
    if not connector.credential_last_rotated_at:
        _add_gate(
            checks,
            "credential_rotation",
            "Credential rotation date",
            False,
            "critical",
            "Record when the credential was last rotated.",
        )
        return

    age_days = (as_of - connector.credential_last_rotated_at).days
    _add_gate(
        checks,
        "credential_rotation",
        "Credential rotation under 180 days",
        age_days < 180,
        "critical",
        f"Credential is {age_days} days old. Rotate it before production use.",
    )
    _add_gate(
        checks,
        "credential_rotation_warning",
        "Credential not near expiry",
        age_days < 150,
        "warning",
        f"Credential is {age_days} days old. Plan rotation before 180 days.",
    )


def _certification_evidence_gates(checks, connector):
    evidence = connector_production_evidence(connector)
    required = connector.mode == IntegrationConnector.MODE_PRODUCTION and connector.status == IntegrationConnector.STATUS_LIVE
    fields = []
    for field in PRODUCTION_EVIDENCE_FIELDS:
        value = evidence.get(field["key"], "")
        passed = bool(value)
        item = {**field, "value": value, "passed": passed or not required}
        fields.append(item)
        if required:
            _add_gate(
                checks,
                f"cert_{field['key']}",
                field["label"],
                passed,
                field["level"],
                field["fix"],
            )
    return {
        "required": required,
        "fields": fields,
        "missing_count": sum(1 for field in fields if required and not field["value"]),
    }


def _empty_certification_evidence():
    return {
        "required": False,
        "fields": [{**field, "value": "", "passed": True} for field in PRODUCTION_EVIDENCE_FIELDS],
        "missing_count": 0,
    }


def _add_gate(checks, code, name, passed, failure_level, fix):
    checks.append({
        "code": code,
        "name": name,
        "level": "ok" if passed else failure_level,
        "message": "Clear" if passed else fix,
        "fix": fix,
    })


def _recent_failure_logs(logs, retry_by_log_id, as_of):
    failures = []
    since = as_of - timezone.timedelta(days=7)
    for log in logs:
        if log.created_at < since:
            continue
        if log.status not in {IntegrationRequestLog.STATUS_FAILED, IntegrationRequestLog.STATUS_CONFIG_ERROR}:
            continue
        job = retry_by_log_id.get(log.pk)
        log.retry_job_status = job.get_status_display() if job else ""
        log.retry_job_id = job.pk if job else None
        log.retry_job_open = bool(job and job.status in OPEN_RETRY_STATUSES)
        failures.append(log)
    return failures[:25]


def _overall_score(connector_rows, open_jobs, failed_jobs, recent_failures):
    if not connector_rows:
        return 0
    avg_connector_score = round(sum(row["score"] for row in connector_rows) / len(connector_rows))
    penalty = min(15, len(open_jobs) * 3) + min(15, len(failed_jobs) * 5) + min(10, len(recent_failures) * 2)
    return max(0, avg_connector_score - penalty)


def _overall_status(score, connector_rows, failed_jobs):
    if failed_jobs or any(row["state"] == "critical" for row in connector_rows):
        return "blocked"
    if score >= 90 and all(row["state"] == "ready" for row in connector_rows):
        return "production_ready"
    if score >= 70:
        return "sandbox_ready"
    return "needs_evidence"


def _overall_status_label(score, connector_rows, failed_jobs):
    status = _overall_status(score, connector_rows, failed_jobs)
    return {
        "production_ready": "Production Ready",
        "sandbox_ready": "Sandbox Ready",
        "needs_evidence": "Needs Evidence",
        "blocked": "Blocked",
    }[status]


def _readiness_summary(score, connector_rows, open_jobs, recent_failures):
    critical = sum(row["critical_count"] for row in connector_rows)
    warnings = sum(row["warning_count"] for row in connector_rows)
    if critical:
        return f"Blocked by {critical} critical provider gate(s)."
    if open_jobs:
        return f"Provider gates are mostly clear, but {len(open_jobs)} retry job(s) remain open."
    if recent_failures:
        return f"Provider gates need reconciliation for {len(recent_failures)} recent failed request(s)."
    if warnings:
        return f"Score {score}% with {warnings} evidence or production-hardening warning(s)."
    return "GST, IRP, e-way bill, and TRACES provider go-live gates are clear."


def _connector_type_for_service(service):
    for connector_type, mapped_service in CONNECTOR_SERVICE_MAP.items():
        if mapped_service == service:
            return connector_type
    return ""


def _retry_priority(log):
    if log.status == IntegrationRequestLog.STATUS_CONFIG_ERROR:
        return IntegrationRetryJob.PRIORITY_CRITICAL
    if log.service in {IntegrationRequestLog.SERVICE_E_INVOICE, IntegrationRequestLog.SERVICE_E_WAY_BILL, IntegrationRequestLog.SERVICE_TRACES}:
        return IntegrationRetryJob.PRIORITY_CRITICAL
    return IntegrationRetryJob.PRIORITY_HIGH


def _task_type_for_connector(connector_type):
    if connector_type in {IntegrationConnector.TYPE_GST, IntegrationConnector.TYPE_IRP, IntegrationConnector.TYPE_EWAY}:
        return PracticeTask.TYPE_GST
    if connector_type == IntegrationConnector.TYPE_TRACES:
        return PracticeTask.TYPE_TDS
    return PracticeTask.TYPE_OTHER


def _provider_task_description(company, issue):
    return (
        f"Company: {company.name}\n"
        f"Connector: {issue['connector_name']}\n"
        f"Gate: {issue['name']}\n"
        f"Severity: {issue['level'].title()}\n"
        f"Issue: {issue['message']}\n"
        f"Next action: {issue['fix']}"
    )


def _retry_job_snapshot(job):
    return {
        "company_id": job.company_id,
        "connector_id": job.connector_id,
        "request_log_id": job.request_log_id,
        "voucher_id": job.voucher_id,
        "service": job.service,
        "provider": job.provider,
        "status": job.status,
        "priority": job.priority,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "next_attempt_at": job.next_attempt_at.isoformat() if job.next_attempt_at else "",
        "last_error": job.last_error,
        "resolved_by_id": job.resolved_by_id,
        "resolved_at": job.resolved_at.isoformat() if job.resolved_at else "",
    }
