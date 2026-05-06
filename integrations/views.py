import logging
import csv
import hashlib
import json
from datetime import datetime, time
from urllib.parse import urlencode
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse, HttpResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.utils.crypto import constant_time_compare
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.core.files.base import ContentFile
from django.shortcuts import get_object_or_404, redirect, render
from ocr.models import OCRSubmission
from core.models import AuditLog, BankStatement, BankStatementRow, Company, PracticeTask, UserCompanyAccess
from core.decorators import write_required
from core.evidence_vault import (
    create_evidence_vault_tasks,
    list_vault_entries,
    seal_evidence_vault,
    verify_vault_chain,
)
from core.phone import normalize_phone_number
from core.upload_validation import DOCUMENT_EXTENSIONS, validate_uploaded_file
from ledger.models import Ledger
from tds.models import TDSReturnWorkpaper
from vouchers.models import Voucher

from .gst import (
    GSTConfigurationError,
    GSTIntegrationError,
    build_e_invoice_payload,
    build_e_way_bill_payload,
    dump_gst_payload_json,
    generate_e_invoice_for_voucher,
    generate_e_way_bill_for_voucher,
    validate_gstin,
)
from .models import IntegrationConnector, IntegrationRequestLog, IntegrationRetryJob, StatutoryExportLog
from .provider_readiness import (
    build_provider_go_live_readiness,
    create_provider_readiness_tasks,
    queue_failed_provider_requests,
    resolve_retry_job,
)
from .readiness import (
    PRODUCTION_EVIDENCE_FIELDS,
    build_connector_control_plane,
    build_gst_certification_readiness,
    build_statutory_integration_control_room,
    connector_production_evidence,
    create_statutory_integration_tasks,
    statutory_control_focus_choices,
)
from .result_import import import_gst_result_file
from .retry_dispatcher import process_due_retry_jobs
from .traces_import import import_traces_result_file

logger = logging.getLogger(__name__)

BANK_FEED_CSV_EXTENSIONS = {".csv"}


def _configured(*values):
    return all(bool(value) for value in values)


def _mask(value):
    if not value:
        return ""
    if len(value) <= 8:
        return "configured"
    return f"{value[:4]}...{value[-4:]}"


def _connector_snapshot(connector):
    if not connector:
        return {}
    return {
        "connector_type": connector.connector_type,
        "display_name": connector.display_name,
        "provider_name": connector.provider_name,
        "mode": connector.mode,
        "status": connector.status,
        "gstin": connector.gstin,
        "tan": connector.tan,
        "username": connector.masked_username,
        "base_url": connector.base_url,
        "credential_reference": connector.credential_reference,
        "credential_last_rotated_at": connector.credential_last_rotated_at.isoformat() if connector.credential_last_rotated_at else "",
        "production_evidence": connector_production_evidence(connector),
        "notes": connector.notes,
    }


def _choice_value(value, choices, default):
    allowed = {key for key, _label in choices}
    return value if value in allowed else default


def _safe_next_redirect(request, fallback, *args, **kwargs):
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect(fallback, *args, **kwargs)


def _request_value(request, *keys):
    for key in keys:
        value = request.POST.get(key)
        if value:
            return value

    if request.content_type == "application/json":
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        for key in keys:
            value = payload.get(key)
            if value:
                return value
    return ""


def _request_data(request):
    if request.content_type == "application/json":
        try:
            parsed = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return request.POST


def _data_value(data, *keys):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _companies_for_user(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return (
        Company.objects.filter(user_access__user=user)
        .distinct()
        .order_by("name")
    )


def _manageable_company_ids_for_user(user):
    if user.is_superuser:
        return set(Company.objects.values_list("pk", flat=True))
    return set(
        UserCompanyAccess.objects.filter(user=user, role__in=["Admin", "Accountant"])
        .values_list("company_id", flat=True)
    )


def _connector_rotation_timestamp(request):
    if request.POST.get("credential_rotated_now"):
        return timezone.now()
    value = _data_value(request.POST, "credential_last_rotated_at")
    if not value:
        return None
    parsed_date = parse_date(value)
    if parsed_date and "T" not in value and " " not in value:
        return timezone.make_aware(datetime.combine(parsed_date, time(hour=12)))
    return _parse_optional_datetime(value, "Credential last rotated")


def _wants_json(request):
    return request.content_type == "application/json" or request.headers.get("x-requested-with") == "XMLHttpRequest"


def _payload_response(payload, filename):
    response = HttpResponse(dump_gst_payload_json(payload), content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _voucher_filename(voucher, suffix):
    number = "".join(ch for ch in (voucher.number or str(voucher.pk)) if ch.isalnum() or ch in ("-", "_"))
    return f"{number}_{suffix}.json"


def _parse_optional_datetime(value, field_label):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        parsed_date = parse_date(value)
        if parsed_date:
            parsed = datetime.combine(parsed_date, time.min)
    if parsed is None:
        raise ValidationError(f"{field_label} must be a valid date/time.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _optional_json_object(value, field_label):
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    if isinstance(parsed, dict):
        return parsed
    raise ValidationError(f"{field_label} must be a JSON object.")


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


def _record_connector_result(company, connector_type, success, error_message="", user=None):
    connector = IntegrationConnector.objects.filter(
        company=company,
        connector_type=connector_type,
    ).first()
    if not connector:
        return 0

    now = timezone.now()
    update_fields = ["updated_at"]
    if success:
        connector.last_success_at = now
        connector.last_error = ""
        if connector.status == IntegrationConnector.STATUS_READY:
            connector.status = IntegrationConnector.STATUS_LIVE
            update_fields.append("status")
        update_fields.extend(["last_success_at", "last_error"])
    else:
        connector.last_failure_at = now
        connector.last_error = str(error_message)[:1000]
        update_fields.extend(["last_failure_at", "last_error"])
    connector.save(update_fields=update_fields)
    if success:
        return _close_resolved_integration_task(connector, user)
    return 0


def _generate_e_invoice_with_audit(request, voucher):
    old_data = {
        "e_invoice_irn": voucher.e_invoice_irn,
        "e_invoice_ack_no": voucher.e_invoice_ack_no,
        "e_invoice_ack_date": voucher.e_invoice_ack_date.isoformat() if voucher.e_invoice_ack_date else None,
        "e_invoice_status": voucher.e_invoice_status,
    }
    result = generate_e_invoice_for_voucher(voucher, request.user)
    voucher.refresh_from_db()
    _audit_voucher_update(
        request.current_company,
        request.user,
        voucher,
        old_data,
        {
            "e_invoice_irn": voucher.e_invoice_irn,
            "e_invoice_ack_no": voucher.e_invoice_ack_no,
            "e_invoice_ack_date": voucher.e_invoice_ack_date.isoformat() if voucher.e_invoice_ack_date else None,
            "e_invoice_status": voucher.e_invoice_status,
            "source": "gst_provider",
        },
    )
    _record_connector_result(request.current_company, IntegrationConnector.TYPE_IRP, True, user=request.user)
    return result


def _generate_e_way_bill_with_audit(request, voucher):
    old_data = {
        "e_way_bill_no": voucher.e_way_bill_no,
        "e_way_bill_date": voucher.e_way_bill_date.isoformat() if voucher.e_way_bill_date else None,
        "e_way_bill_valid_until": voucher.e_way_bill_valid_until.isoformat() if voucher.e_way_bill_valid_until else None,
        "e_way_bill_status": voucher.e_way_bill_status,
    }
    result = generate_e_way_bill_for_voucher(voucher, request.user)
    voucher.refresh_from_db()
    _audit_voucher_update(
        request.current_company,
        request.user,
        voucher,
        old_data,
        {
            "e_way_bill_no": voucher.e_way_bill_no,
            "e_way_bill_date": voucher.e_way_bill_date.isoformat() if voucher.e_way_bill_date else None,
            "e_way_bill_valid_until": voucher.e_way_bill_valid_until.isoformat() if voucher.e_way_bill_valid_until else None,
            "e_way_bill_status": voucher.e_way_bill_status,
            "source": "gst_provider",
        },
    )
    _record_connector_result(request.current_company, IntegrationConnector.TYPE_EWAY, True, user=request.user)
    return result


def _status_response(request, voucher, message, payload):
    if _wants_json(request):
        return JsonResponse({"success": True, "voucher_id": voucher.pk, **payload})
    messages.success(request, message)
    return _safe_next_redirect(request, "vouchers:detail", pk=voucher.pk)


def _error_response(request, voucher, error, status=400):
    if _wants_json(request):
        return JsonResponse({"success": False, "error": str(error)}, status=status)
    messages.error(request, str(error))
    return _safe_next_redirect(request, "vouchers:detail", pk=voucher.pk)


def _company_for_whatsapp_intake(token, intake_number):
    if token:
        try:
            return Company.objects.get(portal_token=token), None
        except Company.DoesNotExist:
            return None, "Invalid company token"

    if not intake_number:
        return None, "Missing company token or WhatsApp intake number"

    try:
        normalized_number = normalize_phone_number(intake_number)
    except ValueError:
        return None, "Invalid WhatsApp intake number"

    try:
        return Company.objects.get(whatsapp_intake_number=normalized_number), None
    except Company.DoesNotExist:
        return None, "No company is configured for this WhatsApp intake number"


@login_required
def integration_dashboard(request):
    current_company = getattr(request, "current_company", None)
    if not current_company:
        current_company = _companies_for_user(request.user).first()
    whatsapp_intake_number = getattr(current_company, "whatsapp_intake_number", "") if current_company else ""
    recent_logs = IntegrationRequestLog.objects.filter(
        company=current_company,
    ).select_related("voucher", "requested_by")[:10] if current_company else []
    gst_readiness = build_gst_certification_readiness(current_company)
    connector_plane = build_connector_control_plane(current_company)
    provider_readiness = build_provider_go_live_readiness(current_company)
    integrations = [
        {
            "name": "GST API",
            "purpose": "GSTR, GSTIN lookup, e-invoice and e-way bill connectivity",
            "enabled": bool(settings.GST_API_PROVIDER),
            "configured": _configured(
                settings.GST_API_PROVIDER,
                settings.GST_API_BASE_URL if settings.GST_API_PROVIDER != "mock" else "mock",
                settings.GST_API_KEY if settings.GST_API_PROVIDER != "mock" else "mock",
                settings.GST_API_SECRET if settings.GST_API_PROVIDER != "mock" else "mock",
            ),
            "details": {
                "provider": settings.GST_API_PROVIDER or "Not selected",
                "base_url": settings.GST_API_BASE_URL or "Not configured",
                "api_key": _mask(settings.GST_API_KEY),
                "sandbox": "Yes" if settings.GST_API_SANDBOX_MODE else "No",
                "taxpayer_gstin": settings.GST_API_TAXPAYER_GSTIN or "Not configured",
                "e_invoice": "Enabled" if settings.E_INVOICE_ENABLED else "Disabled",
                "e_way_bill": "Enabled" if settings.E_WAY_BILL_ENABLED else "Disabled",
            },
        },
        {
            "name": "Connected Banking",
            "purpose": "Bank feed import and statement reconciliation",
            "enabled": bool(settings.BANK_FEED_PROVIDER),
            "configured": _configured(settings.BANK_FEED_PROVIDER, settings.BANK_FEED_BASE_URL, settings.BANK_FEED_API_KEY),
            "details": {
                "provider": settings.BANK_FEED_PROVIDER or "Not selected",
                "base_url": settings.BANK_FEED_BASE_URL or "Not configured",
                "api_key": _mask(settings.BANK_FEED_API_KEY),
                "api_secret": _mask(settings.BANK_FEED_API_SECRET),
            },
        },
        {
            "name": "Payments",
            "purpose": "Subscription payments and invoice payment links",
            "enabled": bool(settings.PAYMENT_PROVIDER),
            "configured": _configured(
                settings.PAYMENT_PROVIDER,
                settings.PAYMENT_API_KEY,
                settings.PAYMENT_WEBHOOK_SECRET,
            ),
            "details": {
                "provider": settings.PAYMENT_PROVIDER or "Not selected",
                "api_key": _mask(settings.PAYMENT_API_KEY),
                "webhook_secret": _mask(settings.PAYMENT_WEBHOOK_SECRET),
            },
        },
        {
            "name": "WhatsApp Document Intake",
            "purpose": "Client document collection into OCR inbox",
            "enabled": bool(settings.WHATSAPP_WEBHOOK_TOKEN),
            "configured": bool(settings.WHATSAPP_WEBHOOK_TOKEN) and _configured(settings.WHATSAPP_API_URL, settings.WHATSAPP_API_TOKEN),
            "details": {
                "intake_number": whatsapp_intake_number or "Not configured",
                "webhook_token": _mask(settings.WHATSAPP_WEBHOOK_TOKEN),
                "api_url": settings.WHATSAPP_API_URL or "Not configured",
                "api_token": _mask(settings.WHATSAPP_API_TOKEN),
                "accepted_files": ", ".join(sorted(DOCUMENT_EXTENSIONS)),
            },
        },
    ]
    return render(request, "integrations/dashboard.html", {
        "integrations": integrations,
        "recent_logs": recent_logs,
        "gst_readiness": gst_readiness,
        "connector_plane": connector_plane,
        "provider_readiness": provider_readiness,
    })


@login_required
def provider_go_live_readiness(request):
    current_company = getattr(request, "current_company", None)
    if not current_company:
        current_company = _companies_for_user(request.user).first()
    if not current_company:
        messages.error(request, "Select a company before opening Provider Go-Live Readiness.")
        return redirect("core:select_company")

    assessment = build_provider_go_live_readiness(current_company)

    if request.method == "POST":
        if current_company.pk not in _manageable_company_ids_for_user(request.user):
            messages.error(request, "You need Admin or Accountant access to update provider readiness.")
            return redirect("integrations:provider_readiness")
        action = request.POST.get("action", "")
        if action == "sync_provider_tasks":
            result = create_provider_readiness_tasks(current_company, request.user, assessment)
            if result["created"]:
                messages.success(request, f"Provider readiness tasks ready: {result['created']} created, {result['existing']} already existed.")
            elif result["existing"]:
                messages.info(request, f"Provider readiness tasks already existed for {result['existing']} item(s).")
            elif result["closed"]:
                messages.success(request, f"Closed {result['closed']} resolved provider readiness task(s).")
            else:
                messages.info(request, "No provider readiness gates currently need tasks.")
            if result["closed"] and result["created"]:
                messages.success(request, f"Closed {result['closed']} resolved provider readiness task(s).")
        elif action == "queue_failed_requests":
            result = queue_failed_provider_requests(current_company, request.user)
            if result["created"]:
                messages.success(request, f"Retry queue updated: {result['created']} failed provider request(s) queued.")
            elif result["existing"]:
                messages.info(request, f"Retry queue already had {result['existing']} open job(s).")
            else:
                messages.info(request, "No recent failed provider requests need retry jobs.")
            if result["skipped"]:
                messages.info(request, f"Skipped {result['skipped']} resolved or cancelled retry item(s).")
        elif action == "run_due_retries":
            result = process_due_retry_jobs(company=current_company, user=request.user, limit=5)
            if result["processed"]:
                messages.success(
                    request,
                    f"Retry run complete: {result['resolved']} resolved, {result['failed']} failed.",
                )
            elif result["unsupported"]:
                messages.info(request, f"{result['unsupported']} due retry job(s) require manual portal handling.")
            else:
                messages.info(request, "No due dispatchable retry jobs found.")
        else:
            messages.error(request, "Unknown provider readiness action.")
        return redirect("integrations:provider_readiness")

    return render(request, "integrations/provider_readiness.html", {
        "assessment": assessment,
        "title": "Provider Go-Live Readiness",
    })


@login_required
@write_required
@require_POST
def provider_retry_job_update(request, job_id):
    current_company = getattr(request, "current_company", None)
    if not current_company:
        messages.error(request, "Select a company before updating retry jobs.")
        return redirect("integrations:provider_readiness")

    job = get_object_or_404(IntegrationRetryJob, pk=job_id, company=current_company)
    action = request.POST.get("action", "")
    note = _data_value(request.POST, "note")
    if action == "resolve_retry_job":
        resolve_retry_job(job, request.user, status=IntegrationRetryJob.STATUS_RESOLVED, note=note)
        messages.success(request, "Retry job marked resolved.")
    elif action == "cancel_retry_job":
        resolve_retry_job(job, request.user, status=IntegrationRetryJob.STATUS_CANCELLED, note=note)
        messages.success(request, "Retry job cancelled.")
    else:
        messages.error(request, "Unknown retry job action.")
    return redirect("integrations:provider_readiness")


def _bank_feed_provider_name(request, connector=None):
    return (
        _data_value(request.POST, "provider_name")
        or (connector.provider_name if connector else "")
        or settings.BANK_FEED_PROVIDER
        or "Manual Bank Feed"
    )


def _bank_feed_context(company, *, form_values=None):
    form_values = form_values or {}
    connector = IntegrationConnector.objects.filter(
        company=company,
        connector_type=IntegrationConnector.TYPE_BANK,
    ).first()
    return {
        "connector": connector,
        "bank_ledgers": Ledger.objects.filter(company=company, is_active=True).order_by("name"),
        "recent_statements": (
            BankStatement.objects.filter(company=company)
            .select_related("account_ledger")
            .order_by("-uploaded_at")[:8]
        ),
        "recent_logs": (
            IntegrationRequestLog.objects.filter(
                company=company,
                service=IntegrationRequestLog.SERVICE_BANK_FEED,
            )
            .select_related("requested_by")
            .order_by("-created_at")[:8]
        ),
        "provider_name": form_values.get("provider_name") or _bank_feed_provider_name_from_connector(connector),
        "selected_ledger_id": str(form_values.get("account_ledger") or ""),
        "statement_date": form_values.get("statement_date") or timezone.localdate().isoformat(),
        "title": "Connected Banking Feed Import",
    }


def _bank_feed_provider_name_from_connector(connector):
    return (
        (connector.provider_name if connector else "")
        or settings.BANK_FEED_PROVIDER
        or "Manual Bank Feed"
    )


def _ensure_bank_feed_connector(company, provider_name, user):
    connector, created = IntegrationConnector.objects.get_or_create(
        company=company,
        connector_type=IntegrationConnector.TYPE_BANK,
        defaults={
            "display_name": "Connected Banking",
            "provider_name": provider_name,
            "mode": IntegrationConnector.MODE_MANUAL,
            "status": IntegrationConnector.STATUS_READY,
            "credential_reference": "MANUAL_CSV_UPLOAD",
        },
    )
    old_data = _connector_snapshot(None if created else connector)
    changed_fields = []

    if not connector.display_name:
        connector.display_name = "Connected Banking"
        changed_fields.append("display_name")
    if provider_name and connector.provider_name != provider_name:
        connector.provider_name = provider_name
        changed_fields.append("provider_name")
    if not connector.credential_reference:
        connector.credential_reference = "MANUAL_CSV_UPLOAD"
        changed_fields.append("credential_reference")
    if connector.mode not in {IntegrationConnector.MODE_MANUAL, IntegrationConnector.MODE_SANDBOX, IntegrationConnector.MODE_PRODUCTION}:
        connector.mode = IntegrationConnector.MODE_MANUAL
        changed_fields.append("mode")
    if connector.status in {
        IntegrationConnector.STATUS_DISABLED,
        IntegrationConnector.STATUS_NEEDS_SETUP,
        IntegrationConnector.STATUS_BLOCKED,
    }:
        connector.status = IntegrationConnector.STATUS_READY
        changed_fields.append("status")

    if changed_fields:
        connector.save(update_fields=sorted(set(changed_fields + ["updated_at"])))

    if created or changed_fields:
        AuditLog.objects.create(
            company=company,
            user=user,
            action=AuditLog.ACTION_CREATE if created else AuditLog.ACTION_UPDATE,
            model_name="IntegrationConnector",
            record_id=connector.pk,
            object_repr=str(connector)[:200],
            old_data=old_data,
            new_data=_connector_snapshot(connector),
        )
    return connector


def _bank_feed_failure(company, request, provider_name, digest, error_message, *, status):
    _record_connector_result(
        company,
        IntegrationConnector.TYPE_BANK,
        False,
        error_message,
        user=request.user,
    )
    IntegrationRequestLog.objects.create(
        company=company,
        requested_by=request.user,
        provider=provider_name,
        service=IntegrationRequestLog.SERVICE_BANK_FEED,
        status=status,
        request_digest=digest or "",
        response_payload={"file_name": getattr(request.FILES.get("feed_file"), "name", "")},
        error_message=error_message,
    )


@login_required
@write_required
def bank_feed_import(request):
    current_company = getattr(request, "current_company", None)
    if not current_company:
        messages.error(request, "Select a company before importing bank feeds.")
        return redirect("core:select_company")

    connector = IntegrationConnector.objects.filter(
        company=current_company,
        connector_type=IntegrationConnector.TYPE_BANK,
    ).first()

    if request.method == "POST":
        provider_name = _bank_feed_provider_name(request, connector)
        statement_date = parse_date(_data_value(request.POST, "statement_date"))
        account_ledger_id = _data_value(request.POST, "account_ledger")
        upload = request.FILES.get("feed_file")
        errors = []
        digest = ""

        account_ledger = None
        if account_ledger_id:
            account_ledger = Ledger.objects.filter(
                company=current_company,
                is_active=True,
                pk=account_ledger_id,
            ).first()
        if not account_ledger:
            errors.append("Select a valid bank ledger.")
        if not statement_date:
            errors.append("Select a valid statement date.")
        if not upload:
            errors.append("Upload a bank feed CSV file.")
        else:
            try:
                validate_uploaded_file(
                    upload,
                    allowed_extensions=BANK_FEED_CSV_EXTENSIONS,
                    max_mb=20,
                    require_signature=False,
                )
            except ValidationError as exc:
                errors.extend(exc.messages)

        if errors:
            error_message = "; ".join(errors)
            _bank_feed_failure(
                current_company,
                request,
                provider_name,
                digest,
                error_message,
                status=IntegrationRequestLog.STATUS_CONFIG_ERROR,
            )
            messages.error(request, error_message)
            return render(request, "integrations/bank_feed_import.html", _bank_feed_context(current_company, form_values=request.POST))

        file_bytes = upload.read()
        digest = hashlib.sha256(file_bytes).hexdigest()
        duplicate_log = (
            IntegrationRequestLog.objects.filter(
                company=current_company,
                service=IntegrationRequestLog.SERVICE_BANK_FEED,
                status=IntegrationRequestLog.STATUS_SUCCESS,
                request_digest=digest,
            )
            .order_by("created_at", "id")
            .first()
        )
        if duplicate_log:
            payload = duplicate_log.response_payload or {}
            statement_id = payload.get("statement_id") or payload.get("original_statement_id")
            IntegrationRequestLog.objects.create(
                company=current_company,
                requested_by=request.user,
                provider=provider_name,
                service=IntegrationRequestLog.SERVICE_BANK_FEED,
                status=IntegrationRequestLog.STATUS_SUCCESS,
                request_digest=digest,
                response_payload={
                    "duplicate": True,
                    "original_request_id": str(duplicate_log.request_id),
                    "statement_id": statement_id,
                },
                response_code="duplicate_skipped",
            )
            messages.info(request, "This bank feed was already imported. No duplicate statement was created.")
            if statement_id:
                return redirect("core:bank_statement_detail", pk=statement_id)
            return redirect("integrations:bank_feed_import")

        try:
            from core.views import _auto_match, _parse_csv

            with transaction.atomic():
                rows_data = _parse_csv(file_bytes)
                if not rows_data:
                    raise ValidationError("No bank transactions were found in the CSV file.")

                statement = BankStatement.objects.create(
                    company=current_company,
                    account_ledger=account_ledger,
                    statement_date=statement_date,
                    notes=f"Connected bank feed import feed:{digest[:12]} via {provider_name}",
                )
                BankStatementRow.objects.bulk_create([
                    BankStatementRow(
                        statement=statement,
                        date=row["date"],
                        description=row["description"],
                        debit=row["debit"],
                        credit=row["credit"],
                        balance=row.get("balance"),
                        row_number=row["row_number"],
                    )
                    for row in rows_data
                ])
                auto_matched = _auto_match(statement)
                _ensure_bank_feed_connector(current_company, provider_name, request.user)
                closed_tasks = _record_connector_result(
                    current_company,
                    IntegrationConnector.TYPE_BANK,
                    True,
                    user=request.user,
                )
                IntegrationRequestLog.objects.create(
                    company=current_company,
                    requested_by=request.user,
                    provider=provider_name,
                    service=IntegrationRequestLog.SERVICE_BANK_FEED,
                    status=IntegrationRequestLog.STATUS_SUCCESS,
                    request_digest=digest,
                    response_code="imported",
                    response_payload={
                        "statement_id": statement.pk,
                        "ledger_id": account_ledger.pk,
                        "ledger_name": account_ledger.name,
                        "row_count": len(rows_data),
                        "auto_matched": auto_matched,
                        "closed_control_tasks": closed_tasks,
                        "file_name": upload.name,
                    },
                )
                AuditLog.objects.create(
                    company=current_company,
                    user=request.user,
                    action=AuditLog.ACTION_CREATE,
                    model_name="BankStatement",
                    record_id=statement.pk,
                    object_repr=str(statement)[:200],
                    old_data={},
                    new_data={
                        "source": "connected_bank_feed_import",
                        "request_digest": digest,
                        "provider": provider_name,
                        "row_count": len(rows_data),
                        "auto_matched": auto_matched,
                    },
                )
        except ValidationError as exc:
            error_message = "; ".join(exc.messages)
            _bank_feed_failure(
                current_company,
                request,
                provider_name,
                digest,
                error_message,
                status=IntegrationRequestLog.STATUS_FAILED,
            )
            messages.error(request, error_message)
            return render(request, "integrations/bank_feed_import.html", _bank_feed_context(current_company, form_values=request.POST))
        except Exception as exc:
            logger.exception("Connected bank feed import failed for company %s", current_company.pk)
            error_message = str(exc)
            _bank_feed_failure(
                current_company,
                request,
                provider_name,
                digest,
                error_message,
                status=IntegrationRequestLog.STATUS_FAILED,
            )
            messages.error(request, "Bank feed import failed. Check the file and try again.")
            return render(request, "integrations/bank_feed_import.html", _bank_feed_context(current_company, form_values=request.POST))

        messages.success(request, f"Imported {len(rows_data)} bank feed row(s). {auto_matched} auto-matched.")
        return redirect("core:bank_statement_detail", pk=statement.pk)

    return render(request, "integrations/bank_feed_import.html", _bank_feed_context(current_company))


def _statutory_control_csv_response(center):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="statutory_integration_control_room.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "Connector",
        "Severity",
        "Issue",
        "Next Action",
        "Provider",
        "Mode",
        "Status",
        "Credential Reference",
        "Credential Age Days",
        "Last Success",
        "Last Failure",
        "Latest Request Status",
        "Latest Request Time",
        "Last Error",
        "Task Exists",
    ])
    for row in center["connector_rows"]:
        writer.writerow([
            row["company"].name,
            row["name"],
            row["severity_label"],
            row["issue"],
            row["next_action"],
            row["provider_name"],
            row["mode_label"],
            row["status_label"],
            row["credential_reference"],
            row["credential_age_days"] if row["credential_age_days"] is not None else "",
            row["last_success_at"].isoformat() if row["last_success_at"] else "",
            row["last_failure_at"].isoformat() if row["last_failure_at"] else "",
            row["latest_log_status"],
            row["latest_log_at"].isoformat() if row["latest_log_at"] else "",
            row["last_error"],
            "Yes" if row["task_exists"] else "No",
        ])
    return response


def _control_row_for_connector(connector):
    center = build_statutory_integration_control_room(
        Company.objects.filter(pk=connector.company_id),
        focus="all",
    )
    return next(
        (row for row in center["connector_rows"] if row["type"] == connector.connector_type),
        None,
    )


def _close_resolved_integration_task(connector, user):
    row = _control_row_for_connector(connector)
    if not row or row["severity"] in {"critical", "warning"}:
        return 0

    now = timezone.now()
    tasks = PracticeTask.objects.filter(
        company=connector.company,
        reference=f"INTCTL:{connector.company_id}:{connector.connector_type}",
    ).exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])

    closed = 0
    for task in tasks:
        old_status = task.status
        task.status = PracticeTask.STATUS_DONE
        task.completed_by = user
        task.completed_at = now
        task.description = (
            f"{task.description}\n\nIntegration connector resolved through settings."
        ).strip()
        task.save(update_fields=["status", "completed_by", "completed_at", "description", "updated_at"])
        closed += 1
        AuditLog.objects.create(
            company=connector.company,
            user=user,
            action=AuditLog.ACTION_UPDATE,
            model_name="PracticeTask",
            record_id=task.pk,
            object_repr=task.title[:200],
            old_data={"status": old_status},
            new_data={
                "status": task.status,
                "source": "statutory_integration_control_room_auto_close",
                "connector_type": connector.connector_type,
            },
        )
    return closed


@login_required
def statutory_integration_control_room(request):
    params = request.POST if request.method == "POST" else request.GET
    companies = _companies_for_user(request.user)
    selected_company_id = (params.get("company") or "").strip()
    if selected_company_id and companies.filter(pk=selected_company_id).exists():
        scoped_companies = companies.filter(pk=selected_company_id)
    else:
        scoped_companies = companies
        selected_company_id = ""

    focus = (params.get("focus") or "attention").strip()
    valid_focuses = {value for value, _label in statutory_control_focus_choices()}
    if focus not in valid_focuses:
        focus = "attention"

    center = build_statutory_integration_control_room(scoped_companies, focus=focus)
    connector_setup_path = reverse("integrations:dashboard")
    for row in center["connector_rows"]:
        row["action_url"] = (
            f"{reverse('core:switch_company', args=[row['company'].pk])}?"
            f"{urlencode({'next': connector_setup_path})}"
        )
    for row in center["company_rows"]:
        row["action_url"] = (
            f"{reverse('core:switch_company', args=[row['company'].pk])}?"
            f"{urlencode({'next': connector_setup_path})}"
        )

    query_args = {"focus": focus}
    if selected_company_id:
        query_args["company"] = selected_company_id
    base_query = urlencode(query_args)

    if request.method == "GET" and request.GET.get("export") == "csv":
        return _statutory_control_csv_response(center)

    if request.method == "POST":
        action = request.POST.get("action", "")
        selected_keys = request.POST.getlist("connector_ids")
        manageable_ids = _manageable_company_ids_for_user(request.user)
        if action == "create_tasks":
            result = create_statutory_integration_tasks(
                center["connector_rows"],
                request.user,
                manageable_ids,
                selected_keys,
            )
            if result["created"]:
                messages.success(request, f"Integration tasks ready: {result['created']} created, {result['existing']} already existed.")
            elif result["existing"]:
                messages.info(request, f"Integration tasks already existed for {result['existing']} item(s).")
            else:
                messages.info(request, "No eligible integration blockers were selected.")
            if result["skipped"]:
                messages.warning(request, f"Skipped {result['skipped']} connector(s) without write access or without an open issue.")
        else:
            messages.error(request, "Invalid integration control action.")
        return redirect(f"{reverse('integrations:statutory_control')}?{base_query}")

    return render(request, "integrations/statutory_control_room.html", {
        "company_rows": center["company_rows"],
        "connector_rows": center["connector_rows"],
        "totals": center["totals"],
        "companies": companies,
        "selected_company_id": selected_company_id,
        "focus": focus,
        "focus_choices": statutory_control_focus_choices(),
        "base_query": base_query,
        "export_query": f"{base_query}&export=csv",
        "title": "Statutory Integration Control Room",
    })


@login_required
@write_required
@require_POST
def connector_update(request, connector_type):
    current_company = getattr(request, "current_company", None)
    if not current_company:
        messages.error(request, "Select a company before configuring integrations.")
        return redirect("integrations:dashboard")

    connector_types = {key for key, _label in IntegrationConnector.CONNECTOR_CHOICES}
    if connector_type not in connector_types:
        messages.error(request, "Unknown connector type.")
        return redirect("integrations:dashboard")

    connector, created = IntegrationConnector.objects.get_or_create(
        company=current_company,
        connector_type=connector_type,
        defaults={
            "display_name": dict(IntegrationConnector.CONNECTOR_CHOICES).get(connector_type, ""),
        },
    )
    old_data = _connector_snapshot(None if created else connector)
    connector.display_name = _data_value(request.POST, "display_name") or connector.display_name
    connector.provider_name = _data_value(request.POST, "provider_name")
    connector.mode = _choice_value(
        _data_value(request.POST, "mode"),
        IntegrationConnector.MODE_CHOICES,
        IntegrationConnector.MODE_MANUAL,
    )
    connector.status = _choice_value(
        _data_value(request.POST, "status"),
        IntegrationConnector.STATUS_CHOICES,
        IntegrationConnector.STATUS_NEEDS_SETUP,
    )
    connector.gstin = _data_value(request.POST, "gstin").upper()
    connector.tan = _data_value(request.POST, "tan").upper()
    connector.username = _data_value(request.POST, "username")
    connector.base_url = _data_value(request.POST, "base_url")
    connector.credential_reference = _data_value(request.POST, "credential_reference")
    try:
        connector.credential_last_rotated_at = _connector_rotation_timestamp(request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("integrations:dashboard")
    metadata = dict(connector.metadata or {})
    for field in PRODUCTION_EVIDENCE_FIELDS:
        metadata[field["key"]] = _data_value(request.POST, f"evidence_{field['key']}")
    connector.metadata = metadata
    connector.notes = _data_value(request.POST, "notes")

    try:
        connector.full_clean()
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("integrations:dashboard")

    connector.save()
    AuditLog.objects.create(
        company=current_company,
        user=request.user,
        action=AuditLog.ACTION_CREATE if created else AuditLog.ACTION_UPDATE,
        model_name="IntegrationConnector",
        record_id=connector.pk,
        object_repr=str(connector)[:200],
        old_data=old_data,
        new_data=_connector_snapshot(connector),
    )
    messages.success(request, f"{connector.label} settings saved.")
    closed_tasks = _close_resolved_integration_task(connector, request.user)
    if closed_tasks:
        messages.success(request, f"Completed {closed_tasks} resolved integration task(s).")
    return redirect("integrations:dashboard")


def _evidence_date_range(request):
    today = timezone.localdate()
    default_start = today - timezone.timedelta(days=90)
    start_date = parse_date(request.GET.get("start_date", "")) or default_start
    end_date = parse_date(request.GET.get("end_date", "")) or today
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def _apply_export_filters(queryset, request, start_date, end_date):
    queryset = queryset.filter(created_at__date__range=(start_date, end_date))
    export_type = request.GET.get("export_type", "")
    export_status = request.GET.get("export_status", "")
    q = request.GET.get("q", "").strip()
    if export_type:
        queryset = queryset.filter(export_type=export_type)
    if export_status:
        queryset = queryset.filter(status=export_status)
    if q:
        queryset = queryset.filter(
            Q(file_name__icontains=q)
            | Q(file_sha256__icontains=q)
            | Q(portal_reference__icontains=q)
            | Q(generated_by__email__icontains=q)
        )
    return queryset


def _apply_request_filters(queryset, request, start_date, end_date):
    queryset = queryset.filter(created_at__date__range=(start_date, end_date))
    service = request.GET.get("service", "")
    request_status = request.GET.get("request_status", "")
    q = request.GET.get("q", "").strip()
    if service:
        queryset = queryset.filter(service=service)
    if request_status:
        queryset = queryset.filter(status=request_status)
    if q:
        queryset = queryset.filter(
            Q(request_id__icontains=q)
            | Q(provider__icontains=q)
            | Q(request_digest__icontains=q)
            | Q(error_message__icontains=q)
            | Q(voucher__number__icontains=q)
            | Q(requested_by__email__icontains=q)
        )
    return queryset


def _evidence_csv_response(company, export_logs, request_logs, start_date, end_date, vault_entries=None, vault_verification=None):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    filename = f"Statutory_Evidence_{company.name}_{start_date:%Y%m%d}_{end_date:%Y%m%d}.csv".replace(" ", "_")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow([
        "Evidence Type",
        "Date",
        "Company",
        "Category",
        "Status",
        "Reference",
        "User",
        "File Or Provider",
        "Hash Or Digest",
        "Period",
        "Rows",
        "Amount",
        "Error",
    ])
    for log in export_logs:
        writer.writerow([
            "Statutory Export",
            log.created_at.strftime("%Y-%m-%d %H:%M"),
            company.name,
            log.get_export_type_display(),
            log.get_status_display(),
            log.portal_reference,
            log.generated_by.email if log.generated_by else "",
            log.file_name,
            log.file_sha256,
            _period_label(log.period_start, log.period_end),
            log.row_count,
            f"{log.amount_total:.2f}",
            "",
        ])
    for log in request_logs:
        writer.writerow([
            "Integration Request",
            log.created_at.strftime("%Y-%m-%d %H:%M"),
            company.name,
            log.get_service_display(),
            log.get_status_display(),
            log.voucher.number if log.voucher else str(log.request_id),
            log.requested_by.email if log.requested_by else "",
            log.provider,
            log.request_digest,
            "",
            "",
            "",
            log.error_message,
        ])
    if vault_entries is not None and vault_verification is not None:
        _vault_csv_rows(writer, company, vault_entries, vault_verification)
    return response


def _vault_csv_rows(writer, company, vault_entries, vault_verification):
    writer.writerow([])
    writer.writerow([
        "Evidence Type",
        "Date",
        "Company",
        "Category",
        "Status",
        "Reference",
        "User",
        "File Or Provider",
        "Hash Or Digest",
        "Period",
        "Rows",
        "Amount",
        "Error",
    ])
    for entry in vault_entries:
        artifact = entry.get("artifact") or {}
        writer.writerow([
            "Immutable Vault",
            entry.get("created_at", ""),
            company.name,
            entry.get("category", ""),
            "Sealed",
            entry.get("reference", ""),
            entry.get("sealed_by", ""),
            artifact.get("name", ""),
            artifact.get("sha256", ""),
            "",
            "",
            "",
            "",
        ])
    for issue in vault_verification.get("issues", []):
        writer.writerow([
            "Vault Verification",
            "",
            company.name,
            issue["code"],
            issue["severity"],
            f"Sequence {issue['sequence']}",
            "",
            "",
            vault_verification.get("head_hash", ""),
            "",
            "",
            "",
            issue["message"],
        ])


def _period_label(start_date, end_date):
    if start_date and end_date:
        return f"{start_date:%Y-%m-%d} to {end_date:%Y-%m-%d}"
    if start_date:
        return start_date.strftime("%Y-%m-%d")
    if end_date:
        return end_date.strftime("%Y-%m-%d")
    return ""


@login_required
def evidence_center(request):
    current_company = getattr(request, "current_company", None)
    if not current_company:
        messages.error(request, "Select a company before opening the evidence center.")
        return redirect("core:select_company")

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "seal_vault":
            result = seal_evidence_vault(current_company, user=request.user)
            messages.success(
                request,
                (
                    "Evidence Vault sealed: "
                    f"{result['created']} new, {result['skipped']} existing; "
                    f"status {result['verification']['status']}."
                ),
            )
        elif action == "sync_vault_tasks":
            verification = verify_vault_chain(current_company)
            result = create_evidence_vault_tasks(current_company, request.user, verification)
            messages.success(
                request,
                (
                    "Evidence Vault tasks synced: "
                    f"{result['created']} created, {result['updated']} updated, {result['closed']} closed."
                ),
            )
        else:
            messages.error(request, "Unknown evidence vault action.")
        return redirect("integrations:evidence_center")

    start_date, end_date = _evidence_date_range(request)
    evidence_type = request.GET.get("type", "all")

    export_logs = _apply_export_filters(
        StatutoryExportLog.objects.filter(company=current_company).select_related("generated_by", "connector"),
        request,
        start_date,
        end_date,
    )
    request_logs = _apply_request_filters(
        IntegrationRequestLog.objects.filter(company=current_company).select_related("voucher", "requested_by"),
        request,
        start_date,
        end_date,
    )

    if evidence_type == "exports":
        request_logs = request_logs.none()
    elif evidence_type == "requests":
        export_logs = export_logs.none()
    else:
        evidence_type = "all"

    export_logs = export_logs.order_by("-created_at")
    request_logs = request_logs.order_by("-created_at")
    summary = {
        "exports": export_logs.count(),
        "requests": request_logs.count(),
        "successes": request_logs.filter(status=IntegrationRequestLog.STATUS_SUCCESS).count(),
        "failures": request_logs.filter(status=IntegrationRequestLog.STATUS_FAILED).count(),
        "config_errors": request_logs.filter(status=IntegrationRequestLog.STATUS_CONFIG_ERROR).count(),
        "rejected_exports": export_logs.filter(status=StatutoryExportLog.STATUS_REJECTED).count(),
    }
    summary["attention"] = summary["failures"] + summary["config_errors"] + summary["rejected_exports"]
    vault_entries = list_vault_entries(current_company, limit=100)
    vault_verification = verify_vault_chain(current_company)
    summary["vault_entries"] = vault_verification["entries"]
    summary["vault_issues"] = vault_verification["critical_count"] + vault_verification["warning_count"]

    if request.GET.get("export") == "csv":
        return _evidence_csv_response(
            current_company,
            export_logs[:1000],
            request_logs[:1000],
            start_date,
            end_date,
            vault_entries=vault_entries,
            vault_verification=vault_verification,
        )

    return render(request, "integrations/evidence_center.html", {
        "export_logs": export_logs[:100],
        "request_logs": request_logs[:100],
        "summary": summary,
        "vault_entries": vault_entries,
        "vault_verification": vault_verification,
        "start_date": start_date,
        "end_date": end_date,
        "evidence_type": evidence_type,
        "export_type": request.GET.get("export_type", ""),
        "export_status": request.GET.get("export_status", ""),
        "service": request.GET.get("service", ""),
        "request_status": request.GET.get("request_status", ""),
        "q": request.GET.get("q", "").strip(),
        "export_type_choices": StatutoryExportLog.EXPORT_TYPE_CHOICES,
        "export_status_choices": StatutoryExportLog.STATUS_CHOICES,
        "service_choices": IntegrationRequestLog.SERVICE_CHOICES,
        "request_status_choices": IntegrationRequestLog.STATUS_CHOICES,
        "csv_query": request.GET.copy().urlencode() + ("&" if request.GET else "") + "export=csv",
    })


def _gst_cockpit_date_range(request):
    today = timezone.localdate()
    default_start = today - timezone.timedelta(days=90)
    start_date = parse_date(request.GET.get("start_date", "")) or default_start
    end_date = parse_date(request.GET.get("end_date", "")) or today
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def _latest_logs_by_voucher(company, voucher_ids, service):
    logs = IntegrationRequestLog.objects.filter(
        company=company,
        service=service,
        voucher_id__in=voucher_ids,
    ).select_related("requested_by")
    latest = {}
    for log in logs:
        latest.setdefault(log.voucher_id, log)
    return latest


def _e_invoice_row(voucher, latest_log=None):
    errors = []
    payload = None
    buyer_name = ""
    buyer_gstin = ""
    invoice_value = voucher.total_amount()
    taxable_value = None

    try:
        payload = build_e_invoice_payload(voucher)
    except GSTIntegrationError as exc:
        errors.append(str(exc))

    if payload:
        buyer = payload.get("BuyerDtls") or {}
        values = payload.get("ValDtls") or {}
        buyer_name = buyer.get("LglNm", "")
        buyer_gstin = buyer.get("Gstin", "")
        invoice_value = values.get("TotInvVal", invoice_value)
        taxable_value = values.get("AssVal")

    generated = bool(voucher.e_invoice_irn)
    failed = bool(
        latest_log
        and latest_log.status in {
            IntegrationRequestLog.STATUS_FAILED,
            IntegrationRequestLog.STATUS_CONFIG_ERROR,
        }
        and not generated
    )
    ready = not errors
    if generated:
        state = "generated"
        state_label = "IRN Saved"
        badge_class = "bg-success"
    elif failed:
        state = "failed"
        state_label = "Failed"
        badge_class = "bg-danger"
    elif ready:
        state = "ready"
        state_label = "Ready"
        badge_class = "bg-warning text-dark"
    else:
        state = "blocked"
        state_label = "Blocked"
        badge_class = "bg-danger-subtle text-danger"

    return {
        "voucher": voucher,
        "ready": ready,
        "generated": generated,
        "failed": failed,
        "state": state,
        "state_label": state_label,
        "badge_class": badge_class,
        "errors": errors,
        "buyer_name": buyer_name,
        "buyer_gstin": buyer_gstin,
        "invoice_value": invoice_value,
        "taxable_value": taxable_value,
        "latest_log": latest_log,
    }


def _e_way_bill_row(voucher, latest_log=None):
    errors = []
    payload = None
    buyer_name = ""
    buyer_gstin = ""
    invoice_value = voucher.total_amount()
    transport_label = ""
    vehicle_or_transporter = ""

    try:
        payload = build_e_way_bill_payload(voucher)
    except GSTIntegrationError as exc:
        errors.append(str(exc))

    if payload:
        buyer_name = payload.get("toTrdName", "")
        buyer_gstin = payload.get("toGstin", "")
        invoice_value = payload.get("totInvValue", invoice_value)
        transport_label = dict(Voucher.TRANSPORT_MODE_CHOICES).get(str(payload.get("transMode") or ""), payload.get("transMode") or "")
        vehicle_or_transporter = payload.get("vehicleNo") or payload.get("transporterId") or ""

    now = timezone.now()
    expiry_window = now + timezone.timedelta(hours=48)
    generated = bool(voucher.e_way_bill_no)
    expired = bool(voucher.e_way_bill_valid_until and voucher.e_way_bill_valid_until < now)
    expiring = bool(
        voucher.e_way_bill_valid_until
        and now <= voucher.e_way_bill_valid_until <= expiry_window
    )
    failed = bool(
        latest_log
        and latest_log.status in {
            IntegrationRequestLog.STATUS_FAILED,
            IntegrationRequestLog.STATUS_CONFIG_ERROR,
        }
        and not generated
    )
    ready = not errors

    if expired:
        state = "expired"
        state_label = "Expired"
        badge_class = "bg-danger"
    elif expiring:
        state = "expiring"
        state_label = "Expiring Soon"
        badge_class = "bg-warning text-dark"
    elif generated:
        state = "generated"
        state_label = "EWB Saved"
        badge_class = "bg-success"
    elif failed:
        state = "failed"
        state_label = "Failed"
        badge_class = "bg-danger"
    elif ready:
        state = "ready"
        state_label = "Ready"
        badge_class = "bg-warning text-dark"
    else:
        state = "blocked"
        state_label = "Blocked"
        badge_class = "bg-danger-subtle text-danger"

    return {
        "voucher": voucher,
        "ready": ready,
        "generated": generated,
        "failed": failed,
        "expired": expired,
        "expiring": expiring,
        "state": state,
        "state_label": state_label,
        "badge_class": badge_class,
        "errors": errors,
        "buyer_name": buyer_name,
        "buyer_gstin": buyer_gstin,
        "invoice_value": invoice_value,
        "transport_label": transport_label,
        "vehicle_or_transporter": vehicle_or_transporter,
        "latest_log": latest_log,
    }


@login_required
def e_invoice_cockpit(request):
    current_company = getattr(request, "current_company", None)
    if not current_company:
        messages.error(request, "Select a company before opening the e-invoice cockpit.")
        return redirect("core:select_company")

    start_date, end_date = _gst_cockpit_date_range(request)
    status_filter = request.GET.get("status", "pending")
    q = request.GET.get("q", "").strip()

    vouchers = (
        Voucher.objects.filter(
            company=current_company,
            voucher_type="Sales",
            date__range=(start_date, end_date),
        )
        .prefetch_related("items__ledger", "items__ledger__account_group", "items__stock_item__hsn_sac", "items__stock_item__tax_rate")
        .order_by("-date", "-id")
    )
    if q:
        vouchers = vouchers.filter(
            Q(number__icontains=q)
            | Q(source_reference__icontains=q)
            | Q(items__ledger__name__icontains=q)
            | Q(items__ledger__gstin__icontains=q)
        ).distinct()

    voucher_list = list(vouchers[:150])
    latest_logs = _latest_logs_by_voucher(
        current_company,
        [voucher.pk for voucher in voucher_list],
        IntegrationRequestLog.SERVICE_E_INVOICE,
    )
    all_rows = [_e_invoice_row(voucher, latest_logs.get(voucher.pk)) for voucher in voucher_list]

    summary = {
        "total": len(all_rows),
        "generated": sum(1 for row in all_rows if row["state"] == "generated"),
        "ready": sum(1 for row in all_rows if row["state"] == "ready"),
        "blocked": sum(1 for row in all_rows if row["state"] == "blocked"),
        "failed": sum(1 for row in all_rows if row["state"] == "failed"),
    }
    summary["pending"] = summary["ready"] + summary["blocked"] + summary["failed"]

    if status_filter == "pending":
        rows = [row for row in all_rows if row["state"] != "generated"]
    elif status_filter in {"generated", "ready", "blocked", "failed"}:
        rows = [row for row in all_rows if row["state"] == status_filter]
    else:
        status_filter = "all"
        rows = all_rows

    irp_connector = IntegrationConnector.objects.filter(
        company=current_company,
        connector_type=IntegrationConnector.TYPE_IRP,
    ).first()
    connector_ready = bool(
        irp_connector
        and irp_connector.is_ready
        and irp_connector.provider_name
        and irp_connector.gstin
        and irp_connector.credential_reference
    )

    return render(request, "integrations/e_invoice_cockpit.html", {
        "rows": rows,
        "summary": summary,
        "status_filter": status_filter,
        "status_choices": [
            ("pending", "Pending"),
            ("ready", "Ready"),
            ("blocked", "Blocked"),
            ("failed", "Failed"),
            ("generated", "IRN Saved"),
            ("all", "All"),
        ],
        "start_date": start_date,
        "end_date": end_date,
        "q": q,
        "current_path": request.get_full_path(),
        "irp_connector": irp_connector,
        "connector_ready": connector_ready,
        "provider_ready": bool(settings.GST_API_PROVIDER),
        "e_invoice_enabled": settings.E_INVOICE_ENABLED,
    })


@login_required
@write_required
@require_POST
def e_invoice_cockpit_generate(request, voucher_id):
    voucher = get_object_or_404(Voucher, pk=voucher_id, company=request.current_company, voucher_type="Sales")
    try:
        _generate_e_invoice_with_audit(request, voucher)
    except GSTConfigurationError as exc:
        _record_connector_result(request.current_company, IntegrationConnector.TYPE_IRP, False, str(exc))
        messages.error(request, f"{voucher.number}: {exc}")
    except GSTIntegrationError as exc:
        _record_connector_result(request.current_company, IntegrationConnector.TYPE_IRP, False, str(exc))
        messages.error(request, f"{voucher.number}: {exc}")
    else:
        voucher.refresh_from_db()
        messages.success(request, f"IRN generated for {voucher.number}: {voucher.e_invoice_irn}")
    return _safe_next_redirect(request, "integrations:e_invoice_cockpit")


@login_required
def e_way_bill_cockpit(request):
    current_company = getattr(request, "current_company", None)
    if not current_company:
        messages.error(request, "Select a company before opening the e-way bill cockpit.")
        return redirect("core:select_company")

    start_date, end_date = _gst_cockpit_date_range(request)
    status_filter = request.GET.get("status", "pending")
    q = request.GET.get("q", "").strip()

    vouchers = (
        Voucher.objects.filter(
            company=current_company,
            voucher_type="Sales",
            date__range=(start_date, end_date),
        )
        .prefetch_related("items__ledger", "items__ledger__account_group", "items__stock_item__hsn_sac", "items__stock_item__tax_rate")
        .order_by("-date", "-id")
    )
    if q:
        vouchers = vouchers.filter(
            Q(number__icontains=q)
            | Q(source_reference__icontains=q)
            | Q(vehicle_number__icontains=q)
            | Q(transporter_id__icontains=q)
            | Q(items__ledger__name__icontains=q)
            | Q(items__ledger__gstin__icontains=q)
        ).distinct()

    voucher_list = list(vouchers[:150])
    latest_logs = _latest_logs_by_voucher(
        current_company,
        [voucher.pk for voucher in voucher_list],
        IntegrationRequestLog.SERVICE_E_WAY_BILL,
    )
    all_rows = [_e_way_bill_row(voucher, latest_logs.get(voucher.pk)) for voucher in voucher_list]

    summary = {
        "total": len(all_rows),
        "generated": sum(1 for row in all_rows if row["state"] == "generated"),
        "ready": sum(1 for row in all_rows if row["state"] == "ready"),
        "blocked": sum(1 for row in all_rows if row["state"] == "blocked"),
        "failed": sum(1 for row in all_rows if row["state"] == "failed"),
        "expiring": sum(1 for row in all_rows if row["state"] == "expiring"),
        "expired": sum(1 for row in all_rows if row["state"] == "expired"),
    }
    summary["pending"] = summary["ready"] + summary["blocked"] + summary["failed"] + summary["expiring"] + summary["expired"]

    if status_filter == "pending":
        rows = [row for row in all_rows if row["state"] != "generated"]
    elif status_filter in {"generated", "ready", "blocked", "failed", "expiring", "expired"}:
        rows = [row for row in all_rows if row["state"] == status_filter]
    else:
        status_filter = "all"
        rows = all_rows

    eway_connector = IntegrationConnector.objects.filter(
        company=current_company,
        connector_type=IntegrationConnector.TYPE_EWAY,
    ).first()
    connector_ready = bool(
        eway_connector
        and eway_connector.is_ready
        and eway_connector.provider_name
        and eway_connector.gstin
        and eway_connector.credential_reference
    )

    return render(request, "integrations/e_way_bill_cockpit.html", {
        "rows": rows,
        "summary": summary,
        "status_filter": status_filter,
        "status_choices": [
            ("pending", "Pending"),
            ("ready", "Ready"),
            ("blocked", "Blocked"),
            ("failed", "Failed"),
            ("expiring", "Expiring Soon"),
            ("expired", "Expired"),
            ("generated", "EWB Saved"),
            ("all", "All"),
        ],
        "start_date": start_date,
        "end_date": end_date,
        "q": q,
        "current_path": request.get_full_path(),
        "eway_connector": eway_connector,
        "connector_ready": connector_ready,
        "provider_ready": bool(settings.GST_API_PROVIDER),
        "e_way_bill_enabled": settings.E_WAY_BILL_ENABLED,
    })


@login_required
@write_required
@require_POST
def e_way_bill_cockpit_generate(request, voucher_id):
    voucher = get_object_or_404(Voucher, pk=voucher_id, company=request.current_company, voucher_type="Sales")
    try:
        _generate_e_way_bill_with_audit(request, voucher)
    except GSTConfigurationError as exc:
        _record_connector_result(request.current_company, IntegrationConnector.TYPE_EWAY, False, str(exc))
        messages.error(request, f"{voucher.number}: {exc}")
    except GSTIntegrationError as exc:
        _record_connector_result(request.current_company, IntegrationConnector.TYPE_EWAY, False, str(exc))
        messages.error(request, f"{voucher.number}: {exc}")
    else:
        voucher.refresh_from_db()
        messages.success(request, f"E-way bill generated for {voucher.number}: {voucher.e_way_bill_no}")
    return _safe_next_redirect(request, "integrations:e_way_bill_cockpit")


@login_required
@write_required
def gst_result_import(request):
    summary = None
    selected_service = request.POST.get("service", "auto") if request.method == "POST" else request.GET.get("service", "auto")
    if selected_service not in {"auto", "e_invoice", "e_way_bill"}:
        selected_service = "auto"

    if request.method == "POST":
        uploaded_file = request.FILES.get("result_file")
        if not uploaded_file:
            messages.error(request, "Upload a GST portal/GSP JSON result file.")
        else:
            try:
                summary = import_gst_result_file(
                    request.current_company,
                    request.user,
                    uploaded_file,
                    service_filter=selected_service,
                )
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(
                    request,
                    (
                        f"Imported {summary['updated']} GST result(s), "
                        f"created/reused {summary['tasked']} task(s), "
                        f"{summary['failed']} need attention."
                    ),
                )

    return render(request, "integrations/gst_result_import.html", {
        "summary": summary,
        "selected_service": selected_service,
        "service_choices": [
            ("auto", "Auto-detect"),
            ("e_invoice", "E-Invoice / IRN"),
            ("e_way_bill", "E-Way Bill"),
        ],
    })


@login_required
@write_required
def traces_result_import(request):
    summary = None
    current_company = getattr(request, "current_company", None)
    if not current_company:
        messages.error(request, "Select a company before importing TRACES results.")
        return redirect("core:select_company")

    if request.method == "POST":
        uploaded_file = request.FILES.get("result_file")
        if not uploaded_file:
            messages.error(request, "Upload a TRACES JSON or CSV result file.")
        else:
            try:
                summary = import_traces_result_file(
                    current_company,
                    request.user,
                    uploaded_file,
                )
            except ValidationError as exc:
                error_message = "; ".join(exc.messages)
                _record_connector_result(
                    current_company,
                    IntegrationConnector.TYPE_TRACES,
                    False,
                    error_message,
                    user=request.user,
                )
                IntegrationRequestLog.objects.create(
                    company=current_company,
                    requested_by=request.user,
                    provider="traces_upload",
                    service=IntegrationRequestLog.SERVICE_TRACES,
                    status=IntegrationRequestLog.STATUS_CONFIG_ERROR,
                    error_message=error_message,
                )
                messages.error(request, error_message)
            else:
                if summary["failed"]:
                    _record_connector_result(
                        current_company,
                        IntegrationConnector.TYPE_TRACES,
                        False,
                        f"{summary['failed']} TRACES result row(s) need correction.",
                        user=request.user,
                    )
                else:
                    _record_connector_result(
                        current_company,
                        IntegrationConnector.TYPE_TRACES,
                        True,
                        user=request.user,
                    )
                messages.success(
                    request,
                    (
                        f"Imported {summary['updated']} TRACES result(s), "
                        f"created/reused {summary['tasked']} task(s), "
                        f"{summary['failed']} failed."
                    ),
                )

    recent_workpapers = (
        TDSReturnWorkpaper.objects.filter(company=current_company)
        .order_by("-updated_at")[:8]
    )
    recent_logs = (
        IntegrationRequestLog.objects.filter(
            company=current_company,
            service=IntegrationRequestLog.SERVICE_TRACES,
        )
        .select_related("requested_by")
        .order_by("-created_at")[:8]
    )
    traces_connector = IntegrationConnector.objects.filter(
        company=current_company,
        connector_type=IntegrationConnector.TYPE_TRACES,
    ).first()

    return render(request, "integrations/traces_result_import.html", {
        "summary": summary,
        "recent_workpapers": recent_workpapers,
        "recent_logs": recent_logs,
        "traces_connector": traces_connector,
        "title": "TRACES Result Import",
    })


@login_required
def integration_status_api(request):
    current_company = getattr(request, "current_company", None)
    provider_readiness = build_provider_go_live_readiness(current_company)
    return JsonResponse({
        "gst": {
            "provider": settings.GST_API_PROVIDER,
            "configured": _configured(
                settings.GST_API_PROVIDER,
                settings.GST_API_BASE_URL if settings.GST_API_PROVIDER != "mock" else "mock",
                settings.GST_API_KEY if settings.GST_API_PROVIDER != "mock" else "mock",
                settings.GST_API_SECRET if settings.GST_API_PROVIDER != "mock" else "mock",
            ),
            "e_invoice_enabled": settings.E_INVOICE_ENABLED,
            "e_way_bill_enabled": settings.E_WAY_BILL_ENABLED,
            "sandbox": settings.GST_API_SANDBOX_MODE,
            "certification_readiness": build_gst_certification_readiness(current_company),
            "provider_go_live": {
                "score": provider_readiness["score"],
                "status": provider_readiness["status"],
                "status_label": provider_readiness["status_label"],
                "critical_gates": provider_readiness["totals"]["critical_checks"],
                "warning_gates": provider_readiness["totals"]["warning_checks"],
                "open_retry_jobs": provider_readiness["retry_summary"]["open"],
            },
        },
        "banking": {
            "provider": settings.BANK_FEED_PROVIDER,
            "configured": _configured(settings.BANK_FEED_PROVIDER, settings.BANK_FEED_BASE_URL, settings.BANK_FEED_API_KEY),
        },
        "payments": {
            "provider": settings.PAYMENT_PROVIDER,
            "configured": _configured(
                settings.PAYMENT_PROVIDER,
                settings.PAYMENT_API_KEY,
                settings.PAYMENT_WEBHOOK_SECRET,
            ),
        },
        "whatsapp": {
            "webhook_configured": bool(settings.WHATSAPP_WEBHOOK_TOKEN),
            "outbound_configured": _configured(settings.WHATSAPP_API_URL, settings.WHATSAPP_API_TOKEN),
            "intake_number": getattr(current_company, "whatsapp_intake_number", "") if current_company else "",
        },
    })


@login_required
@write_required
@require_POST
def gstin_lookup_api(request):
    gstin = (request.POST.get("gstin") or "").strip().upper()
    if not gstin:
        return JsonResponse({"success": False, "error": "GSTIN is required."}, status=400)
    try:
        result = validate_gstin(request.current_company, gstin, request.user)
        return JsonResponse({"success": True, "result": result})
    except GSTConfigurationError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=503)
    except GSTIntegrationError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=502)


@login_required
@write_required
@require_GET
def e_invoice_payload_download(request, voucher_id):
    voucher = get_object_or_404(Voucher, pk=voucher_id, company=request.current_company)
    try:
        payload = build_e_invoice_payload(voucher)
    except GSTIntegrationError as exc:
        return JsonResponse({"success": False, "errors": str(exc)}, status=422)
    return _payload_response(payload, _voucher_filename(voucher, "e_invoice_irp_payload"))


@login_required
@write_required
@require_GET
def e_way_bill_payload_download(request, voucher_id):
    voucher = get_object_or_404(Voucher, pk=voucher_id, company=request.current_company)
    try:
        payload = build_e_way_bill_payload(voucher)
    except GSTIntegrationError as exc:
        return JsonResponse({"success": False, "errors": str(exc)}, status=422)
    return _payload_response(payload, _voucher_filename(voucher, "e_way_bill_payload"))


@login_required
@write_required
@require_POST
def generate_e_invoice_api(request, voucher_id):
    voucher = get_object_or_404(Voucher, pk=voucher_id, company=request.current_company)
    try:
        result = _generate_e_invoice_with_audit(request, voucher)
        return JsonResponse({
            "success": True,
            "voucher_id": voucher.pk,
            "irn": voucher.e_invoice_irn,
            "ack_no": voucher.e_invoice_ack_no,
            "result": result,
        })
    except GSTConfigurationError as exc:
        _record_connector_result(request.current_company, IntegrationConnector.TYPE_IRP, False, str(exc))
        return JsonResponse({"success": False, "error": str(exc)}, status=503)
    except GSTIntegrationError as exc:
        _record_connector_result(request.current_company, IntegrationConnector.TYPE_IRP, False, str(exc))
        return JsonResponse({"success": False, "error": str(exc)}, status=422)


@login_required
@write_required
@require_POST
def generate_e_way_bill_api(request, voucher_id):
    voucher = get_object_or_404(Voucher, pk=voucher_id, company=request.current_company)
    try:
        result = _generate_e_way_bill_with_audit(request, voucher)
        return JsonResponse({
            "success": True,
            "voucher_id": voucher.pk,
            "e_way_bill_no": voucher.e_way_bill_no,
            "result": result,
        })
    except GSTConfigurationError as exc:
        _record_connector_result(request.current_company, IntegrationConnector.TYPE_EWAY, False, str(exc))
        return JsonResponse({"success": False, "error": str(exc)}, status=503)
    except GSTIntegrationError as exc:
        _record_connector_result(request.current_company, IntegrationConnector.TYPE_EWAY, False, str(exc))
        return JsonResponse({"success": False, "error": str(exc)}, status=422)


@login_required
@write_required
@require_POST
def mark_e_invoice_status(request, voucher_id):
    voucher = get_object_or_404(Voucher, pk=voucher_id, company=request.current_company, voucher_type="Sales")
    data = _request_data(request)
    irn = _data_value(data, "irn", "e_invoice_irn")
    if not irn:
        return _error_response(request, voucher, "IRN is required to mark the e-invoice.", status=400)

    try:
        ack_date = _parse_optional_datetime(_data_value(data, "ack_date", "e_invoice_ack_date"), "Ack date")
        signed_invoice = _optional_json_object(_data_value(data, "signed_invoice", "e_invoice_signed_invoice"), "Signed invoice")
    except ValidationError as exc:
        return _error_response(request, voucher, "; ".join(exc.messages), status=400)

    old_data = {
        "e_invoice_irn": voucher.e_invoice_irn,
        "e_invoice_ack_no": voucher.e_invoice_ack_no,
        "e_invoice_ack_date": voucher.e_invoice_ack_date.isoformat() if voucher.e_invoice_ack_date else None,
        "e_invoice_status": voucher.e_invoice_status,
        "e_invoice_signed_qr_code": voucher.e_invoice_signed_qr_code,
    }
    voucher.e_invoice_irn = irn
    voucher.e_invoice_ack_no = _data_value(data, "ack_no", "e_invoice_ack_no") or voucher.e_invoice_ack_no
    if ack_date:
        voucher.e_invoice_ack_date = ack_date
    voucher.e_invoice_status = _data_value(data, "status", "e_invoice_status") or voucher.e_invoice_status or "ACT"
    signed_qr = _data_value(data, "signed_qr_code", "signed_qr", "e_invoice_signed_qr_code")
    if signed_qr:
        voucher.e_invoice_signed_qr_code = signed_qr
    update_fields = [
        "e_invoice_irn",
        "e_invoice_ack_no",
        "e_invoice_ack_date",
        "e_invoice_status",
        "e_invoice_signed_qr_code",
        "updated_at",
    ]
    if signed_invoice is not None:
        voucher.e_invoice_signed_invoice = signed_invoice
        update_fields.append("e_invoice_signed_invoice")
    voucher.save(update_fields=update_fields)

    new_data = {
        "e_invoice_irn": voucher.e_invoice_irn,
        "e_invoice_ack_no": voucher.e_invoice_ack_no,
        "e_invoice_ack_date": voucher.e_invoice_ack_date.isoformat() if voucher.e_invoice_ack_date else None,
        "e_invoice_status": voucher.e_invoice_status,
        "e_invoice_signed_qr_code": voucher.e_invoice_signed_qr_code,
        "source": "manual_capture",
    }
    _audit_voucher_update(request.current_company, request.user, voucher, old_data, new_data)
    _record_connector_result(request.current_company, IntegrationConnector.TYPE_IRP, True, user=request.user)
    return _status_response(
        request,
        voucher,
        f"IRN captured for invoice {voucher.number}.",
        {"irn": voucher.e_invoice_irn, "ack_no": voucher.e_invoice_ack_no},
    )


@login_required
@write_required
@require_POST
def mark_e_way_bill_status(request, voucher_id):
    voucher = get_object_or_404(Voucher, pk=voucher_id, company=request.current_company, voucher_type="Sales")
    data = _request_data(request)
    e_way_bill_no = _data_value(data, "e_way_bill_no", "ewb_no", "ewbNo")
    if not e_way_bill_no:
        return _error_response(request, voucher, "E-way bill number is required.", status=400)

    try:
        e_way_bill_date = _parse_optional_datetime(_data_value(data, "e_way_bill_date", "ewb_date", "EwbDt"), "E-way bill date")
        valid_until = _parse_optional_datetime(_data_value(data, "valid_until", "e_way_bill_valid_until", "EwbValidTill"), "Valid until")
    except ValidationError as exc:
        return _error_response(request, voucher, "; ".join(exc.messages), status=400)

    old_data = {
        "e_way_bill_no": voucher.e_way_bill_no,
        "e_way_bill_date": voucher.e_way_bill_date.isoformat() if voucher.e_way_bill_date else None,
        "e_way_bill_valid_until": voucher.e_way_bill_valid_until.isoformat() if voucher.e_way_bill_valid_until else None,
        "e_way_bill_status": voucher.e_way_bill_status,
    }
    voucher.e_way_bill_no = e_way_bill_no
    if e_way_bill_date:
        voucher.e_way_bill_date = e_way_bill_date
    if valid_until:
        voucher.e_way_bill_valid_until = valid_until
    voucher.e_way_bill_status = _data_value(data, "status", "e_way_bill_status") or voucher.e_way_bill_status or "ACT"
    voucher.save(update_fields=[
        "e_way_bill_no",
        "e_way_bill_date",
        "e_way_bill_status",
        "e_way_bill_valid_until",
        "updated_at",
    ])

    new_data = {
        "e_way_bill_no": voucher.e_way_bill_no,
        "e_way_bill_date": voucher.e_way_bill_date.isoformat() if voucher.e_way_bill_date else None,
        "e_way_bill_valid_until": voucher.e_way_bill_valid_until.isoformat() if voucher.e_way_bill_valid_until else None,
        "e_way_bill_status": voucher.e_way_bill_status,
        "source": "manual_capture",
    }
    _audit_voucher_update(request.current_company, request.user, voucher, old_data, new_data)
    _record_connector_result(request.current_company, IntegrationConnector.TYPE_EWAY, True, user=request.user)
    return _status_response(
        request,
        voucher,
        f"E-way bill captured for invoice {voucher.number}.",
        {"e_way_bill_no": voucher.e_way_bill_no},
    )


@csrf_exempt
def whatsapp_webhook(request):
    """
    Webhook to receive documents from WhatsApp.
    Simulated POST request expected with:
    - token: Company portal_token
    - file: The document (PDF/Image)
    - sender: Sender's phone number
    """
    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)

    token = _request_value(request, "token", "company_token", "portal_token")
    intake_number = _request_value(
        request,
        "to",
        "receiver",
        "recipient",
        "business_phone",
        "business_phone_number",
        "whatsapp_intake_number",
    )
    sender = _request_value(request, "sender", "from", "wa_from") or "Unknown"
    webhook_token = request.headers.get("X-Webhook-Token", "") or request.POST.get("webhook_token", "")
    expected_webhook_token = getattr(settings, "WHATSAPP_WEBHOOK_TOKEN", "")
    if expected_webhook_token and not constant_time_compare(webhook_token, expected_webhook_token):
        return JsonResponse({"success": False, "error": "Unauthorized webhook request"}, status=403)
    
    if "file" not in request.FILES:
        return JsonResponse({"success": False, "error": "Missing file"}, status=400)

    company, company_error = _company_for_whatsapp_intake(token, intake_number)
    if company_error:
        return JsonResponse({"success": False, "error": company_error}, status=403)

    uploaded_file = request.FILES["file"]
    try:
        validate_uploaded_file(
            uploaded_file,
            allowed_extensions=DOCUMENT_EXTENSIONS,
            max_mb=20,
        )
    except Exception as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    file_data = uploaded_file.read()
    
    # Calculate SHA-256 hash to prevent duplicates
    hasher = hashlib.sha256()
    hasher.update(file_data)
    file_hash = hasher.hexdigest()

    if OCRSubmission.objects.filter(company=company, file_hash=file_hash).exists():
        return JsonResponse({"success": True, "message": "Duplicate document ignored"}, status=200)

    # Store for OCR queue (no processing here)
    submission = OCRSubmission.objects.create(
        company=company,
        file_hash=file_hash,
        status=OCRSubmission.STATUS_PENDING,
        source=OCRSubmission.SOURCE_WHATSAPP,
        ocr_error=f"Received from WhatsApp ({sender})"
    )
    submission.file.save(uploaded_file.name, ContentFile(file_data), save=True)

    logger.info(f"WhatsApp document received for {company.name} from {sender}")
    
    return JsonResponse({
        "success": True, 
        "submission_id": submission.pk,
        "message": "Document queued for OCR"
    })
