from django.utils import timezone

from .models import ComplianceFiling, ComplianceNotice, PracticeTask


def _task_status_for_filing(filing):
    if filing.status == ComplianceFiling.STATUS_FILED:
        return PracticeTask.STATUS_DONE
    if filing.status == ComplianceFiling.STATUS_CANCELLED:
        return PracticeTask.STATUS_CANCELLED
    if filing.status in {ComplianceFiling.STATUS_BLOCKED, ComplianceFiling.STATUS_CLIENT_PENDING}:
        return PracticeTask.STATUS_BLOCKED
    if filing.status in {ComplianceFiling.STATUS_IN_PROGRESS, ComplianceFiling.STATUS_READY_FOR_REVIEW}:
        return PracticeTask.STATUS_IN_PROGRESS
    return PracticeTask.STATUS_OPEN


def sync_task_for_filing(filing, user=None):
    status = _task_status_for_filing(filing)
    task = filing.related_task
    reference = filing.arn_ack_number or filing.source_reference or ""

    if task is None:
        task = PracticeTask(
            company=filing.company,
            created_by=user or filing.created_by,
        )

    task.title = filing.title
    task.task_type = filing.task_type
    task.priority = filing.priority
    task.status = status
    task.due_date = filing.due_date
    task.period_start = filing.period_start
    task.period_end = filing.period_end
    task.assigned_to = filing.assigned_to
    task.reference = reference
    task.description = filing.notes

    if status == PracticeTask.STATUS_DONE:
        if not task.completed_at:
            task.completed_at = filing.filed_at or timezone.now()
        task.completed_by = filing.filed_by or user
    else:
        task.completed_at = None
        task.completed_by = None

    task.save()

    if filing.related_task_id != task.pk:
        filing.related_task = task
        filing.save(update_fields=["related_task", "updated_at"])

    return task


def set_filing_status(filing, status, user=None):
    filing.status = status
    if status == ComplianceFiling.STATUS_FILED:
        filing.filed_at = filing.filed_at or timezone.now()
        filing.filed_by = filing.filed_by or user
    elif filing.filed_at or filing.filed_by_id:
        filing.filed_at = None
        filing.filed_by = None
    filing.save()
    sync_task_for_filing(filing, user=user)
    return filing


def _task_type_for_notice(notice):
    if notice.notice_type == ComplianceNotice.TYPE_GST:
        return PracticeTask.TYPE_GST
    if notice.notice_type == ComplianceNotice.TYPE_TDS:
        return PracticeTask.TYPE_TDS
    if notice.notice_type == ComplianceNotice.TYPE_MCA:
        return PracticeTask.TYPE_MCA
    if notice.notice_type in {ComplianceNotice.TYPE_INCOME_TAX, ComplianceNotice.TYPE_AUDIT}:
        return PracticeTask.TYPE_ITR
    return PracticeTask.TYPE_NOTICE


def _task_status_for_notice(notice):
    if notice.status == ComplianceNotice.STATUS_CLOSED:
        return PracticeTask.STATUS_DONE
    if notice.status in {ComplianceNotice.STATUS_ESCALATED, ComplianceNotice.STATUS_DATA_PENDING}:
        return PracticeTask.STATUS_BLOCKED
    if notice.status in {
        ComplianceNotice.STATUS_IN_REVIEW,
        ComplianceNotice.STATUS_RESPONSE_READY,
        ComplianceNotice.STATUS_RESPONDED,
    }:
        return PracticeTask.STATUS_IN_PROGRESS
    return PracticeTask.STATUS_OPEN


def sync_task_for_notice(notice, user=None):
    status = _task_status_for_notice(notice)
    task = notice.related_task

    if task is None:
        task = PracticeTask(
            company=notice.company,
            created_by=user or notice.created_by,
        )

    task.title = notice.title
    task.task_type = _task_type_for_notice(notice)
    task.priority = notice.priority
    task.status = status
    task.due_date = notice.response_due_date
    task.period_start = notice.issue_date
    task.period_end = notice.response_due_date
    task.assigned_to = notice.assigned_to
    task.reference = notice.reference_number
    task.description = notice.description or notice.response_summary

    if status == PracticeTask.STATUS_DONE:
        if not task.completed_at:
            task.completed_at = notice.closed_at or timezone.now()
        task.completed_by = notice.closed_by or user
    else:
        task.completed_at = None
        task.completed_by = None

    task.save()

    if notice.related_task_id != task.pk:
        notice.related_task = task
        notice.save(update_fields=["related_task", "updated_at"])

    return task


def set_notice_status(notice, status, user=None):
    notice.status = status
    if status == ComplianceNotice.STATUS_CLOSED:
        notice.closed_at = notice.closed_at or timezone.now()
        notice.closed_by = notice.closed_by or user
    elif notice.closed_at or notice.closed_by_id:
        notice.closed_at = None
        notice.closed_by = None
    notice.save()
    sync_task_for_notice(notice, user=user)
    return notice
