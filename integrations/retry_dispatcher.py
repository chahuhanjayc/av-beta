from django.db import transaction
from django.utils import timezone

from core.models import AuditLog

from .gst import (
    GSTConfigurationError,
    GSTIntegrationError,
    generate_e_invoice_for_voucher,
    generate_e_way_bill_for_voucher,
)
from .models import IntegrationConnector, IntegrationRequestLog, IntegrationRetryJob


DISPATCHABLE_SERVICES = {
    IntegrationRequestLog.SERVICE_E_INVOICE: {
        "connector_type": IntegrationConnector.TYPE_IRP,
        "handler": generate_e_invoice_for_voucher,
    },
    IntegrationRequestLog.SERVICE_E_WAY_BILL: {
        "connector_type": IntegrationConnector.TYPE_EWAY,
        "handler": generate_e_way_bill_for_voucher,
    },
}
RETRY_OPEN_STATUSES = (
    IntegrationRetryJob.STATUS_PENDING,
    IntegrationRetryJob.STATUS_FAILED,
)


def process_due_retry_jobs(*, company=None, service="", limit=20, user=None, dry_run=False, as_of=None):
    as_of = as_of or timezone.now()
    limit = max(1, min(int(limit or 20), 100))
    jobs = (
        IntegrationRetryJob.objects.filter(status__in=RETRY_OPEN_STATUSES, next_attempt_at__lte=as_of)
        .select_related("company", "connector", "request_log", "voucher")
        .order_by("next_attempt_at", "-priority", "id")
    )
    if company:
        jobs = jobs.filter(company=company)
    if service:
        jobs = jobs.filter(service=service)

    selected_jobs = list(jobs[:limit])
    summary = {
        "processed": 0,
        "resolved": 0,
        "failed": 0,
        "skipped": 0,
        "unsupported": 0,
        "dry_run": bool(dry_run),
        "jobs": [],
    }
    for job in selected_jobs:
        result = _preview_job(job) if dry_run else _process_retry_job(job, user=user, as_of=as_of)
        summary["jobs"].append(result)
        if result["status"] == "resolved":
            summary["processed"] += 1
            summary["resolved"] += 1
        elif result["status"] == "failed":
            summary["processed"] += 1
            summary["failed"] += 1
        elif result["status"] == "unsupported":
            summary["unsupported"] += 1
        else:
            summary["skipped"] += 1
    return summary


def _preview_job(job):
    if job.service not in DISPATCHABLE_SERVICES:
        return _job_result(job, "unsupported", "Manual/portal retry required for this service.")
    if not job.voucher_id:
        return _job_result(job, "failed", "Retry job has no voucher reference.")
    if job.attempts >= job.max_attempts:
        return _job_result(job, "skipped", "Max attempts already reached.")
    return _job_result(job, "ready", "Due for retry.")


@transaction.atomic
def _process_retry_job(job, *, user=None, as_of=None):
    as_of = as_of or timezone.now()
    job = (
        IntegrationRetryJob.objects.select_for_update()
        .select_related("company", "connector", "request_log", "voucher")
        .get(pk=job.pk)
    )
    if job.status not in RETRY_OPEN_STATUSES or job.next_attempt_at > as_of:
        return _job_result(job, "skipped", "Retry job is not due.")
    if job.service not in DISPATCHABLE_SERVICES:
        return _job_result(job, "unsupported", "Manual/portal retry required for this service.")
    if not job.voucher_id:
        return _fail_job(job, user, "Retry job has no voucher reference.", terminal=True)
    if job.attempts >= job.max_attempts:
        return _fail_job(job, user, "Max attempts reached.", terminal=True)

    old_data = _retry_job_snapshot(job)
    job.status = IntegrationRetryJob.STATUS_IN_PROGRESS
    job.attempts += 1
    job.save(update_fields=["status", "attempts", "updated_at"])
    _audit_retry_job(job, user, old_data, "integration_retry_dispatcher_start")

    dispatch = DISPATCHABLE_SERVICES[job.service]
    handler = dispatch["handler"]
    try:
        result = handler(job.voucher, user=user)
    except (GSTConfigurationError, GSTIntegrationError) as exc:
        return _fail_job(job, user, str(exc), terminal=job.attempts >= job.max_attempts)
    except Exception as exc:
        return _fail_job(job, user, f"Unexpected retry failure: {exc}", terminal=job.attempts >= job.max_attempts)

    old_data = _retry_job_snapshot(job)
    job.status = IntegrationRetryJob.STATUS_RESOLVED
    job.resolved_by = user if getattr(user, "is_authenticated", False) else None
    job.resolved_at = timezone.now()
    job.last_error = ""
    job.response_payload = _safe_payload(result)
    latest_log = _latest_success_log(job)
    if latest_log:
        job.request_log = job.request_log or latest_log
    job.save(update_fields=["status", "resolved_by", "resolved_at", "last_error", "response_payload", "request_log", "updated_at"])
    _record_connector_success(job, dispatch["connector_type"])
    _audit_retry_job(job, user, old_data, "integration_retry_dispatcher_resolved")
    return _job_result(job, "resolved", "Retry completed successfully.")


def _fail_job(job, user, message, *, terminal=False):
    old_data = _retry_job_snapshot(job)
    job.status = IntegrationRetryJob.STATUS_FAILED
    job.last_error = message[:2000]
    if terminal:
        job.next_attempt_at = timezone.now()
    else:
        job.next_attempt_at = timezone.now() + timezone.timedelta(minutes=_backoff_minutes(job.attempts))
    latest_log = _latest_failure_log(job)
    if latest_log:
        job.request_log = job.request_log or latest_log
        job.response_payload = latest_log.response_payload or job.response_payload
    job.save(update_fields=["status", "last_error", "next_attempt_at", "request_log", "response_payload", "updated_at"])
    dispatch = DISPATCHABLE_SERVICES.get(job.service)
    if dispatch:
        _record_connector_failure(job, dispatch["connector_type"], message)
    _audit_retry_job(job, user, old_data, "integration_retry_dispatcher_failed")
    return _job_result(job, "failed", message)


def _record_connector_success(job, connector_type):
    connector = job.connector or IntegrationConnector.objects.filter(
        company=job.company,
        connector_type=connector_type,
    ).first()
    if not connector:
        return
    connector.last_success_at = timezone.now()
    connector.last_error = ""
    update_fields = ["last_success_at", "last_error", "updated_at"]
    if connector.status == IntegrationConnector.STATUS_READY:
        connector.status = IntegrationConnector.STATUS_LIVE
        update_fields.append("status")
    connector.save(update_fields=update_fields)


def _record_connector_failure(job, connector_type, message):
    connector = job.connector or IntegrationConnector.objects.filter(
        company=job.company,
        connector_type=connector_type,
    ).first()
    if not connector:
        return
    connector.last_failure_at = timezone.now()
    connector.last_error = str(message)[:1000]
    connector.save(update_fields=["last_failure_at", "last_error", "updated_at"])


def _latest_success_log(job):
    return (
        IntegrationRequestLog.objects.filter(
            company=job.company,
            voucher=job.voucher,
            service=job.service,
            status=IntegrationRequestLog.STATUS_SUCCESS,
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _latest_failure_log(job):
    return (
        IntegrationRequestLog.objects.filter(
            company=job.company,
            voucher=job.voucher,
            service=job.service,
            status__in=[IntegrationRequestLog.STATUS_FAILED, IntegrationRequestLog.STATUS_CONFIG_ERROR],
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _backoff_minutes(attempts):
    return min(720, 15 * (2 ** max(0, attempts - 1)))


def _safe_payload(value):
    return value if isinstance(value, dict) else {"raw": str(value)}


def _audit_retry_job(job, user, old_data, source):
    AuditLog.objects.create(
        company=job.company,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_UPDATE,
        model_name="IntegrationRetryJob",
        record_id=job.pk,
        object_repr=str(job)[:200],
        old_data=old_data,
        new_data=_retry_job_snapshot(job) | {"source": source},
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


def _job_result(job, status, message):
    return {
        "id": job.pk,
        "company_id": job.company_id,
        "service": job.service,
        "provider": job.provider,
        "status": status,
        "message": message,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
    }
