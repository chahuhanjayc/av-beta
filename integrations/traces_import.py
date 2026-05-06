import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

from django.core.exceptions import ValidationError
from django.utils import timezone

from core.models import AuditLog, PracticeTask
from core.upload_validation import JSON_EXTENSIONS, validate_uploaded_file
from tds.models import TDSFilingPack, TDSPostFilingTracker, TDSReturnWorkpaper
from tds.workbench import quarter_dates, return_due_date

from .models import IntegrationRequestLog


TRACES_RESULT_EXTENSIONS = JSON_EXTENSIONS | {".csv"}


@dataclass
class TracesImportRow:
    form_type: str
    financial_year_start: int | None
    quarter: str
    status: str
    message: str
    workpaper: TDSReturnWorkpaper | None = None
    task: PracticeTask | None = None


def import_traces_result_file(company, user, uploaded_file):
    validate_uploaded_file(
        uploaded_file,
        allowed_extensions=TRACES_RESULT_EXTENSIONS,
        max_mb=10,
        require_signature=False,
    )
    content = uploaded_file.read()
    file_digest = hashlib.sha256(content).hexdigest()
    records = _parse_result_records(uploaded_file.name, content)
    if not records:
        raise ValidationError("No TRACES result records were found in the uploaded file.")

    rows = []
    for index, record in enumerate(records, start=1):
        rows.append(_process_record(company, user, record, uploaded_file.name, file_digest, index))

    return {
        "filename": uploaded_file.name,
        "digest": file_digest,
        "total": len(rows),
        "updated": sum(1 for row in rows if row.status in {"updated", "attention"}),
        "failed": sum(1 for row in rows if row.status in {"failed", "unmatched"}),
        "attention": sum(1 for row in rows if row.status == "attention"),
        "tasked": sum(1 for row in rows if row.task is not None),
        "rows": rows,
    }


def _parse_result_records(filename, content):
    ext = Path(filename or "").suffix.lower()
    if ext == ".csv":
        return _parse_csv_records(content)
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Upload a valid TRACES JSON or CSV result file: {exc}") from exc
    records = []
    _collect_records(payload, records)
    return records


def _parse_csv_records(content):
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    records = []
    for row in reader:
        cleaned = {
            (key or "").strip(): (value or "").strip()
            for key, value in row.items()
            if (key or "").strip()
        }
        if any(cleaned.values()):
            records.append(cleaned)
    return records


def _collect_records(node, records):
    if isinstance(node, list):
        for item in node:
            _collect_records(item, records)
        return
    if not isinstance(node, dict):
        return
    if _looks_like_traces_record(node):
        records.append(node)
        return
    for key in ("data", "Data", "result", "Result", "results", "Results", "records", "Records", "items"):
        if key in node:
            _collect_records(node[key], records)


def _looks_like_traces_record(data):
    normalized = {_normalize_key(key) for key in data.keys()}
    signals = {
        "formtype",
        "financialyear",
        "financialyearstart",
        "fy",
        "quarter",
        "tracesstatus",
        "statementstatus",
        "acknumber",
        "ackno",
        "token",
        "tracestoken",
        "challanstatus",
        "fvustatus",
    }
    return bool(normalized & signals)


def _process_record(company, user, record, filename, file_digest, index):
    record_digest = hashlib.sha256(
        json.dumps(record, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    try:
        parsed = _normalise_record(record)
    except ValidationError as exc:
        message = "; ".join(exc.messages)
        task = _create_traces_task(
            company,
            user,
            None,
            "unmatched",
            message,
            record,
            record_digest,
        )
        _log_traces_result(company, user, record, record_digest, False, message)
        return TracesImportRow("", None, "", "unmatched", message, task=task)

    workpaper = _apply_traces_result(company, user, parsed, record, filename, file_digest)
    attention_reasons = _attention_reasons(parsed)
    if parsed["traces_statement_status"] == TDSReturnWorkpaper.TRACES_REJECTED:
        status = "failed"
        message = parsed["message"] or "TRACES statement was rejected."
        task = _create_traces_task(company, user, workpaper, "rejected", message, record, record_digest)
        _log_traces_result(company, user, record, record_digest, False, message)
    elif attention_reasons:
        status = "attention"
        message = parsed["message"] or "; ".join(attention_reasons)
        task = _create_traces_task(company, user, workpaper, "attention", message, record, record_digest)
        _log_traces_result(company, user, record, record_digest, True, "")
    else:
        status = "updated"
        message = parsed["message"] or "TRACES result imported."
        task = None
        _log_traces_result(company, user, record, record_digest, True, "")

    return TracesImportRow(
        parsed["form_type"],
        parsed["financial_year_start"],
        parsed["quarter"],
        status,
        message,
        workpaper=workpaper,
        task=task,
    )


def _normalise_record(record):
    form_type = _normalise_form_type(_deep_find(record, "form_type", "formType", "form", "return_type", "returnType"))
    fy_start = _normalise_financial_year(record)
    quarter = _normalise_quarter(_deep_find(record, "quarter", "qtr", "period"))
    if not form_type:
        raise ValidationError("Form type is required. Expected 24Q, 26Q, or 27Q.")
    if fy_start is None:
        raise ValidationError("Financial year is required. Expected values like 2026 or 2026-27.")
    if not quarter:
        raise ValidationError("Quarter is required. Expected Q1, Q2, Q3, or Q4.")

    message = _result_message(record)
    return {
        "form_type": form_type,
        "financial_year_start": fy_start,
        "quarter": quarter,
        "traces_statement_status": _normalise_traces_status(record, message),
        "challan_status": _normalise_challan_status(record),
        "fvu_status": _normalise_fvu_status(record),
        "form16_status": _normalise_form16_status(record, form_type),
        "traces_token": str(_deep_find(record, "traces_token", "tracesToken", "token", "request_number", "requestNo", "prn") or "").strip(),
        "ack_number": str(_deep_find(record, "ack_number", "ackNumber", "ack_no", "ackNo", "provisional_receipt_number", "prn") or "").strip(),
        "message": message,
    }


def _normalise_form_type(value):
    text = str(value or "").upper().replace("FORM", "").replace(" ", "")
    match = re.search(r"(24|26|27)Q", text)
    if match:
        candidate = f"{match.group(1)}Q"
        if candidate in {choice[0] for choice in TDSReturnWorkpaper.FORM_TYPE_CHOICES}:
            return candidate
    return ""


def _normalise_financial_year(record):
    value = _deep_find(record, "financial_year_start", "fy_start", "fyStart")
    if value:
        parsed = _first_four_digit_year(value)
        if parsed:
            return parsed

    value = _deep_find(record, "financial_year", "financialYear", "fy", "year")
    parsed = _first_four_digit_year(value)
    if parsed:
        return parsed

    ay_value = _deep_find(record, "assessment_year", "assessmentYear", "ay")
    parsed = _first_four_digit_year(ay_value)
    if parsed:
        return parsed - 1
    return None


def _first_four_digit_year(value):
    match = re.search(r"(20[0-9]{2})", str(value or ""))
    if not match:
        return None
    return int(match.group(1))


def _normalise_quarter(value):
    text = str(value or "").upper().strip()
    match = re.search(r"Q(?:TR|UARTER)?\s*([1-4])", text)
    if not match:
        match = re.fullmatch(r"([1-4])", text)
    if not match:
        return ""
    return f"Q{match.group(1)}"


def _normalise_traces_status(record, message):
    value = str(
        _deep_find(record, "traces_statement_status", "tracesStatus", "statement_status", "statementStatus", "status", "result_status")
        or ""
    ).lower()
    combined = f"{value} {message.lower()}"
    if "reject" in combined:
        return TDSReturnWorkpaper.TRACES_REJECTED
    if "without default" in combined or "without any default" in combined or "no default" in combined:
        return TDSReturnWorkpaper.TRACES_ACCEPTED
    if "default" in combined:
        return TDSReturnWorkpaper.TRACES_PROCESSED_DEFAULT
    if "processed" in combined or "accept" in combined or "success" in combined or "filed" in combined:
        return TDSReturnWorkpaper.TRACES_ACCEPTED
    return TDSReturnWorkpaper.TRACES_NOT_CHECKED


def _normalise_challan_status(record):
    value = str(_deep_find(record, "challan_status", "challanStatus", "csi_status", "csiStatus") or "").lower()
    if "over" in value:
        return TDSReturnWorkpaper.CHALLAN_OVERBOOKED
    if "unmatch" in value or "mismatch" in value:
        return TDSReturnWorkpaper.CHALLAN_UNMATCHED
    if "match" in value or "booked" in value:
        return TDSReturnWorkpaper.CHALLAN_MATCHED
    return TDSReturnWorkpaper.CHALLAN_NOT_CHECKED


def _normalise_fvu_status(record):
    value = str(_deep_find(record, "fvu_status", "fvuStatus", "validation_status", "validationStatus") or "").lower()
    if "fail" in value or "reject" in value:
        return TDSReturnWorkpaper.FVU_FAILED
    if "warn" in value:
        return TDSReturnWorkpaper.FVU_WARNINGS
    if "valid" in value or "pass" in value:
        return TDSReturnWorkpaper.FVU_VALIDATED
    return TDSReturnWorkpaper.FVU_NOT_RUN


def _normalise_form16_status(record, form_type):
    value = str(_deep_find(record, "form16_status", "form16Status", "certificate_status", "certificateStatus") or "").lower()
    if "issued" in value:
        return TDSReturnWorkpaper.FORM16_ISSUED
    if "download" in value:
        return TDSReturnWorkpaper.FORM16_DOWNLOADED
    if "request" in value:
        return TDSReturnWorkpaper.FORM16_REQUESTED
    if form_type == TDSReturnWorkpaper.FORM_24Q:
        return TDSReturnWorkpaper.FORM16_NOT_REQUESTED
    return TDSReturnWorkpaper.FORM16_NOT_APPLICABLE


def _result_message(record):
    value = _deep_find(
        record,
        "message",
        "remarks",
        "remark",
        "error",
        "error_message",
        "errorMessage",
        "reason",
        "description",
    )
    if isinstance(value, (list, dict)):
        return json.dumps(value, default=str)[:1000]
    return str(value or "").strip()[:1000]


def _apply_traces_result(company, user, parsed, record, filename, file_digest):
    period_start, period_end = quarter_dates(parsed["financial_year_start"], parsed["quarter"])
    workpaper, created = TDSReturnWorkpaper.objects.get_or_create(
        company=company,
        form_type=parsed["form_type"],
        financial_year_start=parsed["financial_year_start"],
        quarter=parsed["quarter"],
        defaults={
            "period_start": period_start,
            "period_end": period_end,
            "due_date": return_due_date(parsed["financial_year_start"], parsed["quarter"]),
            "form16_status": parsed["form16_status"],
            "prepared_by": user if getattr(user, "is_authenticated", False) else None,
        },
    )
    old_data = _workpaper_snapshot(None if created else workpaper)

    workpaper.period_start = period_start
    workpaper.period_end = period_end
    workpaper.due_date = return_due_date(parsed["financial_year_start"], parsed["quarter"])
    workpaper.traces_statement_status = parsed["traces_statement_status"]
    if parsed["challan_status"] != TDSReturnWorkpaper.CHALLAN_NOT_CHECKED:
        workpaper.challan_status = parsed["challan_status"]
    if parsed["fvu_status"] != TDSReturnWorkpaper.FVU_NOT_RUN:
        workpaper.fvu_status = parsed["fvu_status"]
    if parsed["form16_status"] != TDSReturnWorkpaper.FORM16_NOT_APPLICABLE or workpaper.form_type == TDSReturnWorkpaper.FORM_24Q:
        workpaper.form16_status = parsed["form16_status"]
    if parsed["traces_token"]:
        workpaper.traces_token = parsed["traces_token"]
    if parsed["ack_number"]:
        workpaper.ack_number = parsed["ack_number"]
    if parsed["ack_number"] and parsed["traces_statement_status"] in {
        TDSReturnWorkpaper.TRACES_ACCEPTED,
        TDSReturnWorkpaper.TRACES_PROCESSED_DEFAULT,
    }:
        workpaper.status = TDSReturnWorkpaper.STATUS_FILED
        if not workpaper.filed_by_id and getattr(user, "is_authenticated", False):
            workpaper.filed_by = user
        if not workpaper.filed_at:
            workpaper.filed_at = timezone.now()
    elif parsed["traces_statement_status"] == TDSReturnWorkpaper.TRACES_REJECTED and workpaper.status == TDSReturnWorkpaper.STATUS_FILED:
        workpaper.status = TDSReturnWorkpaper.STATUS_REOPENED

    workpaper.summary_snapshot = {
        **(workpaper.summary_snapshot or {}),
        "traces_result_import": {
            "filename": filename,
            "file_digest": file_digest,
            "imported_at": timezone.now().isoformat(),
            "ack_number": parsed["ack_number"],
            "traces_token": parsed["traces_token"],
        },
    }
    workpaper.validation_snapshot = {
        **(workpaper.validation_snapshot or {}),
        "traces_result_import": {
            "traces_statement_status": parsed["traces_statement_status"],
            "challan_status": parsed["challan_status"],
            "fvu_status": parsed["fvu_status"],
            "message": parsed["message"],
        },
    }
    if parsed["message"] and parsed["message"] not in workpaper.notes:
        workpaper.notes = (f"{workpaper.notes}\n\nTRACES import: {parsed['message']}").strip()

    workpaper.save()
    AuditLog.objects.create(
        company=company,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_CREATE if created else AuditLog.ACTION_UPDATE,
        model_name="TDSReturnWorkpaper",
        record_id=workpaper.pk,
        object_repr=str(workpaper)[:200],
        old_data=old_data,
        new_data=_workpaper_snapshot(workpaper) | {"source": "traces_result_import"},
    )
    _sync_post_filing_tracker(company, user, workpaper, parsed)
    return workpaper


def _sync_post_filing_tracker(company, user, workpaper, parsed):
    pack = TDSFilingPack.objects.filter(
        company=company,
        form_type=workpaper.form_type,
        financial_year_start=workpaper.financial_year_start,
        quarter=workpaper.quarter,
    ).first()
    if not pack:
        return None

    tracker, created = TDSPostFilingTracker.objects.get_or_create(pack=pack)
    old_data = _tracker_snapshot(None if created else tracker)
    status_map = {
        TDSReturnWorkpaper.TRACES_ACCEPTED: TDSPostFilingTracker.STATEMENT_PROCESSED,
        TDSReturnWorkpaper.TRACES_PROCESSED_DEFAULT: TDSPostFilingTracker.STATEMENT_PROCESSED_DEFAULT,
        TDSReturnWorkpaper.TRACES_REJECTED: TDSPostFilingTracker.STATEMENT_REJECTED,
    }
    tracker.statement_status = status_map.get(
        parsed["traces_statement_status"],
        tracker.statement_status,
    )
    tracker.status_checked_at = timezone.now()
    if parsed["traces_token"]:
        tracker.traces_request_number = parsed["traces_token"]
    if parsed["traces_statement_status"] in {
        TDSReturnWorkpaper.TRACES_PROCESSED_DEFAULT,
        TDSReturnWorkpaper.TRACES_REJECTED,
    }:
        tracker.correction_required = True
        tracker.correction_status = TDSPostFilingTracker.CORRECTION_OPEN
        tracker.correction_reason = (parsed["message"] or "TRACES result needs correction review.")[:240]
        tracker.justification_report_status = TDSPostFilingTracker.REPORT_NOT_REQUESTED
    if parsed["message"] and parsed["message"] not in tracker.notes:
        tracker.notes = (f"{tracker.notes}\n\nTRACES import: {parsed['message']}").strip()
    tracker.updated_by = user if getattr(user, "is_authenticated", False) else None
    tracker.save()
    AuditLog.objects.create(
        company=company,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_CREATE if created else AuditLog.ACTION_UPDATE,
        model_name="TDSPostFilingTracker",
        record_id=tracker.pk,
        object_repr=str(tracker)[:200],
        old_data=old_data,
        new_data=_tracker_snapshot(tracker) | {"source": "traces_result_import"},
    )
    return tracker


def _attention_reasons(parsed):
    reasons = []
    if parsed["traces_statement_status"] == TDSReturnWorkpaper.TRACES_PROCESSED_DEFAULT:
        reasons.append("TRACES processed with default.")
    if parsed["challan_status"] in {
        TDSReturnWorkpaper.CHALLAN_UNMATCHED,
        TDSReturnWorkpaper.CHALLAN_OVERBOOKED,
    }:
        reasons.append("Challan status needs review.")
    if parsed["fvu_status"] == TDSReturnWorkpaper.FVU_FAILED:
        reasons.append("FVU validation failed.")
    return reasons


def _create_traces_task(company, user, workpaper, issue_type, message, record, record_digest):
    if workpaper:
        reference = (
            f"TRACESRESULT:{workpaper.form_type}:{workpaper.financial_year_start}:"
            f"{workpaper.quarter}:{issue_type}:{record_digest[:10]}"
        )
        title_ref = f"{workpaper.form_type} {workpaper.quarter} FY {workpaper.financial_year_label}"
        period_start = workpaper.period_start
        period_end = workpaper.period_end
    else:
        reference = f"TRACESRESULT:unmatched:{issue_type}:{record_digest[:10]}"
        title_ref = record_digest[:10]
        period_start = None
        period_end = None

    task, created = PracticeTask.objects.get_or_create(
        company=company,
        reference=reference,
        defaults={
            "title": f"Resolve TRACES result: {title_ref}",
            "task_type": PracticeTask.TYPE_TDS,
            "priority": PracticeTask.PRIORITY_CRITICAL if issue_type == "rejected" else PracticeTask.PRIORITY_HIGH,
            "status": PracticeTask.STATUS_OPEN,
            "due_date": timezone.localdate() + timezone.timedelta(days=1),
            "period_start": period_start,
            "period_end": period_end,
            "created_by": user if getattr(user, "is_authenticated", False) else None,
            "description": (
                f"TRACES result needs review.\n"
                f"Issue: {issue_type}\n"
                f"Message: {message or '-'}\n"
                f"Result digest: {record_digest[:16]}"
            ),
        },
    )
    if created:
        AuditLog.objects.create(
            company=company,
            user=user if getattr(user, "is_authenticated", False) else None,
            action=AuditLog.ACTION_CREATE,
            model_name="PracticeTask",
            record_id=task.pk,
            object_repr=task.title[:200],
            old_data={},
            new_data={
                "source": "traces_result_import",
                "reference": reference,
                "issue_type": issue_type,
                "message": message,
            },
        )
    return task


def _log_traces_result(company, user, record, record_digest, success, error_message):
    IntegrationRequestLog.objects.create(
        company=company,
        requested_by=user if getattr(user, "is_authenticated", False) else None,
        provider="traces_upload",
        service=IntegrationRequestLog.SERVICE_TRACES,
        status=IntegrationRequestLog.STATUS_SUCCESS if success else IntegrationRequestLog.STATUS_FAILED,
        request_digest=record_digest,
        response_payload=_safe_payload(record),
        error_message=error_message[:2000],
    )


def _safe_payload(record):
    serialized = json.dumps(record, default=str)
    if len(serialized) > 5000:
        return {"truncated": True, "digest": hashlib.sha256(serialized.encode("utf-8")).hexdigest()}
    return json.loads(serialized)


def _workpaper_snapshot(workpaper):
    if not workpaper:
        return {}
    return {
        "status": workpaper.status,
        "form_type": workpaper.form_type,
        "financial_year_start": workpaper.financial_year_start,
        "quarter": workpaper.quarter,
        "traces_statement_status": workpaper.traces_statement_status,
        "challan_status": workpaper.challan_status,
        "fvu_status": workpaper.fvu_status,
        "form16_status": workpaper.form16_status,
        "traces_token": workpaper.traces_token,
        "ack_number": workpaper.ack_number,
    }


def _tracker_snapshot(tracker):
    if not tracker:
        return {}
    return {
        "statement_status": tracker.statement_status,
        "traces_request_number": tracker.traces_request_number,
        "justification_report_status": tracker.justification_report_status,
        "correction_required": tracker.correction_required,
        "correction_reason": tracker.correction_reason,
        "correction_status": tracker.correction_status,
    }


def _deep_find(node, *keys):
    wanted = {_normalize_key(key) for key in keys}
    if isinstance(node, dict):
        for key, value in node.items():
            if _normalize_key(key) in wanted and value not in (None, ""):
                return value
        for value in node.values():
            found = _deep_find(value, *keys)
            if found not in (None, ""):
                return found
    elif isinstance(node, list):
        for item in node:
            found = _deep_find(item, *keys)
            if found not in (None, ""):
                return found
    return None


def _normalize_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())
