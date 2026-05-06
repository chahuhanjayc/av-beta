import hashlib
import json
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.models import AuditLog, PracticeTask
from core.upload_validation import JSON_EXTENSIONS, validate_uploaded_file
from vouchers.models import Voucher

from .models import IntegrationRequestLog


GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


@dataclass
class ImportRow:
    service: str
    document_number: str
    status: str
    message: str
    voucher: Voucher | None = None
    task: PracticeTask | None = None


def import_gst_result_file(company, user, uploaded_file, *, service_filter="auto"):
    validate_uploaded_file(
        uploaded_file,
        allowed_extensions=JSON_EXTENSIONS,
        max_mb=10,
        require_signature=False,
    )
    try:
        payload = json.loads(uploaded_file.read().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Upload a valid JSON response file: {exc}") from exc

    records = _extract_records(payload)
    if not records:
        raise ValidationError("No GST result records were found in the uploaded JSON.")

    rows = []
    for record in records:
        row = _process_record(company, user, record, service_filter)
        rows.append(row)

    return {
        "filename": uploaded_file.name,
        "total": len(rows),
        "updated": sum(1 for row in rows if row.status == "updated"),
        "failed": sum(1 for row in rows if row.status in {"failed", "unmatched", "blocked"}),
        "tasked": sum(1 for row in rows if row.task is not None),
        "rows": rows,
    }


def _process_record(company, user, record, service_filter):
    service = _detect_service(record, service_filter)
    doc_no = _document_number(record)
    if service not in {"e_invoice", "e_way_bill"}:
        message = "Could not identify whether this result is for e-invoice or e-way bill."
        task = _create_result_task(company, user, None, service or "unknown", doc_no, message, record)
        _log_result(company, None, user, service or IntegrationRequestLog.SERVICE_E_INVOICE, record, False, message)
        return ImportRow(service or "unknown", doc_no, "blocked", message, task=task)

    voucher, match_error = _match_voucher(company, service, record)
    error_message = _error_message(record)
    has_success = _success_identifier(service, record)

    if not voucher:
        message = match_error or "No matching voucher was found for this GST result."
        task = _create_result_task(company, user, None, service, doc_no, message, record)
        _log_result(company, None, user, service, record, False, message)
        return ImportRow(service, doc_no, "unmatched", message, task=task)

    if error_message and not has_success:
        task = _create_result_task(company, user, voucher, service, doc_no, error_message, record)
        _log_result(company, voucher, user, service, record, False, error_message)
        return ImportRow(service, doc_no, "failed", error_message, voucher=voucher, task=task)

    try:
        if service == "e_invoice":
            _apply_e_invoice_result(company, user, voucher, record)
            message = "IRN imported."
        else:
            _apply_e_way_bill_result(company, user, voucher, record)
            message = "E-way bill imported."
    except ValidationError as exc:
        message = "; ".join(exc.messages)
        task = _create_result_task(company, user, voucher, service, doc_no, message, record)
        _log_result(company, voucher, user, service, record, False, message)
        return ImportRow(service, doc_no, "failed", message, voucher=voucher, task=task)

    _log_result(company, voucher, user, service, record, True, "")
    return ImportRow(service, doc_no, "updated", message, voucher=voucher)


def _extract_records(payload):
    records = []
    _collect_records(payload, records)
    if not records and isinstance(payload, dict):
        records.append(payload)
    return records


def _collect_records(node, records):
    if isinstance(node, list):
        for item in node:
            _collect_records(item, records)
        return
    if not isinstance(node, dict):
        return
    if _looks_like_result_record(node):
        records.append(node)
        return
    for key in ("data", "Data", "result", "Result", "results", "Results", "response", "Response", "items", "ItemList"):
        if key in node:
            _collect_records(node[key], records)


def _looks_like_result_record(data):
    signals = {
        "irn",
        "ackno",
        "ackdt",
        "signedqrcode",
        "ewbno",
        "ewaybillno",
        "docno",
        "documentno",
        "invoiceno",
        "error",
        "errormessage",
        "errordetails",
    }
    normalized = {_normalize_key(key) for key in data.keys()}
    if normalized & signals:
        return True
    return isinstance(data.get("DocDtls") or data.get("docDtls"), dict)


def _detect_service(record, service_filter):
    if service_filter in {"e_invoice", "e_way_bill"}:
        return service_filter
    service_value = str(_deep_find(record, "service", "type", "module") or "").lower()
    if "way" in service_value or "ewb" in service_value:
        return "e_way_bill"
    if "invoice" in service_value or "irp" in service_value or "irn" in service_value:
        return "e_invoice"
    if _deep_find(record, "EwbNo", "EWBNo", "ewayBillNo", "e_way_bill_no", "ewb_no"):
        return "e_way_bill"
    if _deep_find(record, "Irn", "IRN", "irn", "AckNo", "SignedQRCode", "signed_qr_code"):
        return "e_invoice"
    return ""


def _match_voucher(company, service, record):
    doc_no = _document_number(record)
    existing_irn = str(_deep_find(record, "Irn", "IRN", "irn") or "").strip()
    qs = Voucher.objects.filter(company=company).prefetch_related("items__ledger__account_group")
    if service == "e_invoice":
        qs = qs.filter(voucher_type__in=["Sales", "Sales Return"])

    candidates = []
    if doc_no:
        candidates = list(qs.filter(number__iexact=doc_no))
    elif existing_irn:
        candidates = list(qs.filter(e_invoice_irn__iexact=existing_irn))

    if not candidates:
        return None, f"No voucher matched document number {doc_no or '-'}."
    if len(candidates) > 1:
        return None, f"Multiple vouchers matched document number {doc_no}."

    voucher = candidates[0]
    mismatch = _match_mismatch(voucher, record)
    if mismatch:
        return None, mismatch
    return voucher, ""


def _match_mismatch(voucher, record):
    doc_date = _document_date(record)
    if doc_date and voucher.date != doc_date:
        return f"Matched voucher {voucher.number}, but response date {doc_date.isoformat()} differs from voucher date {voucher.date.isoformat()}."

    value = _invoice_value(record)
    if value is not None and abs(voucher.total_amount() - value) > Decimal("1.00"):
        return f"Matched voucher {voucher.number}, but response value {value:.2f} differs from voucher value {voucher.total_amount():.2f}."

    response_gstins = _gstins(record)
    if response_gstins:
        expected = {
            (voucher.company.gstin or "").strip().upper(),
            *[
                (item.ledger.gstin or "").strip().upper()
                for item in voucher.items.all()
                if item.ledger.gstin
            ],
        }
        expected.discard("")
        if expected and not response_gstins.intersection(expected):
            return f"Matched voucher {voucher.number}, but GSTIN in response does not match company or party GSTIN."
    return ""


def _apply_e_invoice_result(company, user, voucher, record):
    irn = str(_deep_find(record, "Irn", "IRN", "irn") or "").strip()
    if not irn:
        raise ValidationError("Successful e-invoice result has no IRN.")
    old_data = {
        "e_invoice_irn": voucher.e_invoice_irn,
        "e_invoice_ack_no": voucher.e_invoice_ack_no,
        "e_invoice_ack_date": voucher.e_invoice_ack_date.isoformat() if voucher.e_invoice_ack_date else None,
        "e_invoice_status": voucher.e_invoice_status,
    }
    voucher.e_invoice_irn = irn
    voucher.e_invoice_ack_no = str(_deep_find(record, "AckNo", "ack_no", "ackNo") or voucher.e_invoice_ack_no or "")
    ack_date = _parse_datetime_value(_deep_find(record, "AckDt", "ack_date", "ackDt"))
    if ack_date:
        voucher.e_invoice_ack_date = ack_date
    voucher.e_invoice_status = str(_deep_find(record, "Status", "status", "status_cd") or voucher.e_invoice_status or "ACT")
    signed_invoice = _deep_find(record, "SignedInvoice", "signed_invoice", "signedInvoice")
    if signed_invoice:
        voucher.e_invoice_signed_invoice = signed_invoice if isinstance(signed_invoice, dict) else {"raw": str(signed_invoice)}
    signed_qr = _deep_find(record, "SignedQRCode", "SignedQrCode", "signed_qr_code", "signedQRCode")
    if signed_qr:
        voucher.e_invoice_signed_qr_code = str(signed_qr)
    voucher.save(update_fields=[
        "e_invoice_irn",
        "e_invoice_ack_no",
        "e_invoice_ack_date",
        "e_invoice_status",
        "e_invoice_signed_invoice",
        "e_invoice_signed_qr_code",
        "updated_at",
    ])
    _audit_voucher_update(company, user, voucher, old_data, {
        "e_invoice_irn": voucher.e_invoice_irn,
        "e_invoice_ack_no": voucher.e_invoice_ack_no,
        "e_invoice_ack_date": voucher.e_invoice_ack_date.isoformat() if voucher.e_invoice_ack_date else None,
        "e_invoice_status": voucher.e_invoice_status,
        "source": "gst_result_import",
    })


def _apply_e_way_bill_result(company, user, voucher, record):
    ewb_no = str(_deep_find(record, "EwbNo", "EWBNo", "ewayBillNo", "eway_bill_no", "ewbNo", "ewb_no") or "").strip()
    if not ewb_no:
        raise ValidationError("Successful e-way bill result has no e-way bill number.")
    old_data = {
        "e_way_bill_no": voucher.e_way_bill_no,
        "e_way_bill_date": voucher.e_way_bill_date.isoformat() if voucher.e_way_bill_date else None,
        "e_way_bill_valid_until": voucher.e_way_bill_valid_until.isoformat() if voucher.e_way_bill_valid_until else None,
        "e_way_bill_status": voucher.e_way_bill_status,
    }
    voucher.e_way_bill_no = ewb_no
    ewb_date = _parse_datetime_value(_deep_find(record, "EwbDt", "eway_bill_date", "ewbDt", "ewayBillDate"))
    valid_until = _parse_datetime_value(_deep_find(record, "EwbValidTill", "ValidUpto", "valid_upto", "validUntil", "validTill"))
    if ewb_date:
        voucher.e_way_bill_date = ewb_date
    if valid_until:
        voucher.e_way_bill_valid_until = valid_until
    voucher.e_way_bill_status = str(_deep_find(record, "Status", "status") or voucher.e_way_bill_status or "ACT")
    voucher.save(update_fields=[
        "e_way_bill_no",
        "e_way_bill_date",
        "e_way_bill_status",
        "e_way_bill_valid_until",
        "updated_at",
    ])
    _audit_voucher_update(company, user, voucher, old_data, {
        "e_way_bill_no": voucher.e_way_bill_no,
        "e_way_bill_date": voucher.e_way_bill_date.isoformat() if voucher.e_way_bill_date else None,
        "e_way_bill_valid_until": voucher.e_way_bill_valid_until.isoformat() if voucher.e_way_bill_valid_until else None,
        "e_way_bill_status": voucher.e_way_bill_status,
        "source": "gst_result_import",
    })


def _audit_voucher_update(company, user, voucher, old_data, new_data):
    AuditLog.objects.create(
        company=company,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_UPDATE,
        model_name="Voucher",
        record_id=voucher.pk,
        object_repr=str(voucher),
        old_data=old_data,
        new_data=new_data,
    )


def _create_result_task(company, user, voucher, service, doc_no, message, record):
    digest = hashlib.sha1(json.dumps(record, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]
    voucher_key = voucher.pk if voucher else "unmatched"
    reference = f"GSTRESULT:{service}:{voucher_key}:{digest}"
    service_label = "E-invoice" if service == "e_invoice" else "E-way bill" if service == "e_way_bill" else "GST"
    title_ref = voucher.number if voucher else doc_no or digest
    task, _ = PracticeTask.objects.get_or_create(
        company=company,
        reference=reference,
        defaults={
            "title": f"Resolve {service_label} result: {title_ref}",
            "task_type": PracticeTask.TYPE_GST,
            "priority": PracticeTask.PRIORITY_CRITICAL if service == "e_invoice" else PracticeTask.PRIORITY_HIGH,
            "status": PracticeTask.STATUS_OPEN,
            "due_date": timezone.localdate() + timedelta(days=1),
            "period_start": _period_start(voucher.date) if voucher else None,
            "period_end": _period_end(voucher.date) if voucher else None,
            "created_by": user if getattr(user, "is_authenticated", False) else None,
            "description": (
                f"{service_label} portal/GSP result could not be posted automatically.\n"
                f"Document: {doc_no or '-'}\n"
                f"Voucher: {voucher.number if voucher else '-'}\n"
                f"Exact message: {message}\n"
                "Review the voucher master data, portal response, and client evidence before retrying."
            ),
        },
    )
    return task


def _log_result(company, voucher, user, service, record, success, error_message):
    service_name = IntegrationRequestLog.SERVICE_E_WAY_BILL if service == "e_way_bill" else IntegrationRequestLog.SERVICE_E_INVOICE
    serialized = json.dumps(record, sort_keys=True, default=str)
    IntegrationRequestLog.objects.create(
        company=company,
        voucher=voucher,
        requested_by=user if getattr(user, "is_authenticated", False) else None,
        provider="portal_upload",
        service=service_name,
        status=IntegrationRequestLog.STATUS_SUCCESS if success else IntegrationRequestLog.STATUS_FAILED,
        request_digest=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        response_payload=_safe_payload(record),
        error_message=error_message[:2000],
    )


def _safe_payload(record):
    serialized = json.dumps(record, default=str)
    if len(serialized) > 5000:
        return {"truncated": True, "digest": hashlib.sha256(serialized.encode("utf-8")).hexdigest()}
    return json.loads(serialized)


def _document_number(record):
    doc = _nested_dict(record, "DocDtls", "docDtls", "document")
    if doc:
        value = _direct_find(doc, "No", "DocNo", "docNo", "documentNo", "invoiceNo")
        if value:
            return str(value).strip()
    value = _deep_find(record, "docNo", "DocNo", "documentNo", "document_number", "invoiceNo", "invoice_number", "InvNo", "inv_no")
    return str(value or "").strip()


def _document_date(record):
    doc = _nested_dict(record, "DocDtls", "docDtls", "document")
    value = _direct_find(doc, "Dt", "Date", "docDate") if doc else None
    value = value or _deep_find(record, "docDate", "DocDate", "documentDate", "invoiceDate", "invoice_date")
    return _parse_date_value(value)


def _invoice_value(record):
    value = _deep_find(record, "TotInvVal", "totInvValue", "totalInvoiceValue", "invoiceValue", "total_value", "totInvVal")
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _success_identifier(service, record):
    if service == "e_way_bill":
        return _deep_find(record, "EwbNo", "EWBNo", "ewayBillNo", "eway_bill_no", "ewbNo", "ewb_no")
    return _deep_find(record, "Irn", "IRN", "irn")


def _error_message(record):
    details = _deep_find(record, "ErrorDetails", "errorDetails", "errors", "Errors")
    if isinstance(details, list):
        parts = []
        for item in details:
            if isinstance(item, dict):
                parts.append(str(_deep_find(item, "ErrorMessage", "message", "errorMessage", "Desc", "description") or item))
            else:
                parts.append(str(item))
        return "; ".join(part for part in parts if part)
    if isinstance(details, dict):
        return str(_deep_find(details, "ErrorMessage", "message", "errorMessage", "Desc", "description") or details)
    value = _deep_find(record, "ErrorMessage", "errorMessage", "error", "message", "Msg", "status_desc", "statusDesc")
    if value:
        return str(value)
    success = str(_deep_find(record, "success", "Success", "status", "Status") or "").lower()
    if success in {"false", "failed", "failure", "err", "error", "n"}:
        return "Portal/GSP returned a failed status without a detailed error message."
    return ""


def _gstins(record):
    found = set()
    for value in _deep_values(record):
        text = str(value).strip().upper()
        if GSTIN_RE.match(text):
            found.add(text)
    return found


def _deep_values(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _deep_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _deep_values(value)
    else:
        yield node


def _deep_find(data, *keys):
    wanted = {_normalize_key(key) for key in keys}
    if isinstance(data, dict):
        direct = _direct_find(data, *keys)
        if direct is not None:
            return direct
        for value in data.values():
            found = _deep_find(value, *wanted)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _deep_find(value, *wanted)
            if found is not None:
                return found
    return None


def _direct_find(data, *keys):
    if not isinstance(data, dict):
        return None
    wanted = {_normalize_key(key) for key in keys}
    for key, value in data.items():
        if _normalize_key(key) in wanted and value not in (None, ""):
            return value
    return None


def _nested_dict(data, *keys):
    if not isinstance(data, dict):
        return None
    wanted = {_normalize_key(key) for key in keys}
    for key, value in data.items():
        if _normalize_key(key) in wanted and isinstance(value, dict):
            return value
    return None


def _normalize_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _parse_date_value(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    parsed = parse_date(text)
    if parsed:
        return parsed
    parsed_dt = parse_datetime(text)
    if parsed_dt:
        return parsed_dt.date()
    for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return timezone.datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


def _parse_datetime_value(value):
    if not value:
        return None
    if hasattr(value, "tzinfo"):
        parsed = value
    else:
        text = str(value).strip()
        parsed = parse_datetime(text)
        if parsed is None:
            parsed_date = _parse_date_value(text)
            if parsed_date:
                parsed = timezone.datetime.combine(parsed_date, timezone.datetime.min.time())
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _period_start(value):
    return date(value.year, value.month, 1)


def _period_end(value):
    return date(value.year, value.month, monthrange(value.year, value.month)[1])
