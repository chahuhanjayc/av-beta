"""TRACES post-filing and certificate issuance helpers."""

from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from .models import TDSCertificateIssue, TDSFilingPack, TDSPostFilingTracker, TDSReturnWorkpaper
from .workbench import financial_year_label, parse_workbench_filters, quarter_dates, return_due_date


ZERO = Decimal("0.00")


def build_tds_post_filing_center(company, fy_start, quarter, form_type):
    period_start, period_end = quarter_dates(fy_start, quarter)
    pack = TDSFilingPack.objects.filter(
        company=company,
        form_type=form_type,
        financial_year_start=fy_start,
        quarter=quarter,
    ).select_related("workpaper", "generated_by", "filed_by").first()
    tracker = None
    certificates = []
    if pack:
        tracker = getattr(pack, "post_filing_tracker", None)
        certificates = list(pack.certificates.select_related("deductee_ledger", "issued_by").order_by("entry_serial"))

    summary = _summary(pack, tracker, certificates, fy_start, quarter, form_type, period_start, period_end)
    validations = _validations(pack, tracker, certificates, summary)
    return {
        "company": company,
        "filters": {
            "fy_start": fy_start,
            "fy_label": financial_year_label(fy_start),
            "quarter": quarter,
            "form_type": form_type,
            "period_start": period_start,
            "period_end": period_end,
            "due_date": return_due_date(fy_start, quarter),
        },
        "pack": pack,
        "tracker": tracker,
        "certificates": certificates,
        "summary": summary,
        "validations": validations,
    }


def build_tds_post_filing_center_from_params(company, params):
    filters = parse_workbench_filters(params)
    return build_tds_post_filing_center(
        company=company,
        fy_start=filters["fy_start"],
        quarter=filters["quarter"],
        form_type=filters["form_type"],
    )


def save_post_filing_tracker(pack, user, data):
    tracker, _ = TDSPostFilingTracker.objects.get_or_create(pack=pack)
    old_statement_status = tracker.statement_status
    tracker.statement_status = data.get("statement_status") or TDSPostFilingTracker.STATEMENT_NOT_CHECKED
    tracker.traces_request_number = (data.get("traces_request_number") or "").strip()
    tracker.justification_report_status = data.get("justification_report_status") or TDSPostFilingTracker.REPORT_NOT_REQUIRED
    tracker.justification_request_number = (data.get("justification_request_number") or "").strip()
    tracker.conso_file_status = data.get("conso_file_status") or TDSPostFilingTracker.REPORT_NOT_REQUIRED
    tracker.conso_request_number = (data.get("conso_request_number") or "").strip()
    tracker.correction_required = bool(data.get("correction_required"))
    tracker.correction_reason = (data.get("correction_reason") or "").strip()
    tracker.correction_status = data.get("correction_status") or TDSPostFilingTracker.CORRECTION_NOT_REQUIRED
    tracker.notes = (data.get("notes") or "").strip()
    tracker.updated_by = user
    now = timezone.now()
    if tracker.statement_status != old_statement_status or not tracker.status_checked_at:
        tracker.status_checked_at = now
    if tracker.justification_report_status in {
        TDSPostFilingTracker.REPORT_DOWNLOADED,
        TDSPostFilingTracker.REPORT_REVIEWED,
    } and not tracker.justification_downloaded_at:
        tracker.justification_downloaded_at = now
    if tracker.conso_file_status in {
        TDSPostFilingTracker.REPORT_DOWNLOADED,
        TDSPostFilingTracker.REPORT_REVIEWED,
    } and not tracker.conso_downloaded_at:
        tracker.conso_downloaded_at = now
    if tracker.statement_status == TDSPostFilingTracker.STATEMENT_PROCESSED_DEFAULT:
        tracker.correction_required = True
        if tracker.justification_report_status == TDSPostFilingTracker.REPORT_NOT_REQUIRED:
            tracker.justification_report_status = TDSPostFilingTracker.REPORT_NOT_REQUESTED
        if tracker.conso_file_status == TDSPostFilingTracker.REPORT_NOT_REQUIRED:
            tracker.conso_file_status = TDSPostFilingTracker.REPORT_NOT_REQUESTED
        if tracker.correction_status == TDSPostFilingTracker.CORRECTION_NOT_REQUIRED:
            tracker.correction_status = TDSPostFilingTracker.CORRECTION_OPEN
    if not tracker.correction_required and tracker.statement_status != TDSPostFilingTracker.STATEMENT_PROCESSED_DEFAULT:
        tracker.correction_status = TDSPostFilingTracker.CORRECTION_NOT_REQUIRED
    tracker.save()
    return tracker


def sync_certificates_from_pack(pack):
    rows = list((pack.export_snapshot or {}).get("deductee_rows") or [])
    created = 0
    updated = 0
    certificate_type = (
        TDSCertificateIssue.CERT_FORM16
        if pack.form_type == TDSReturnWorkpaper.FORM_24Q
        else TDSCertificateIssue.CERT_FORM16A
    )
    for fallback_idx, row in enumerate(rows, start=1):
        serial = _int_value(row.get("Deductee Serial") or row.get("deductee_serial") or fallback_idx)
        defaults = _certificate_defaults(pack, row, certificate_type)
        cert, was_created = TDSCertificateIssue.objects.get_or_create(
            pack=pack,
            entry_serial=serial,
            defaults=defaults,
        )
        if was_created:
            created += 1
            continue
        changed = False
        for field, value in defaults.items():
            if field in {"request_number", "status", "issue_channel", "evidence_reference", "notes"}:
                continue
            if getattr(cert, field) != value:
                setattr(cert, field, value)
                changed = True
        if changed:
            cert.save()
            updated += 1
    return {"created": created, "updated": updated, "total": TDSCertificateIssue.objects.filter(pack=pack).count()}


def update_certificate_issue(cert, user, data):
    old_status = cert.status
    cert.status = data.get("status") or cert.STATUS_PENDING
    cert.request_number = (data.get("request_number") or "").strip()
    cert.issue_channel = data.get("issue_channel") or ""
    cert.evidence_reference = (data.get("evidence_reference") or "").strip()
    cert.notes = (data.get("notes") or "").strip()
    now = timezone.now()
    if cert.status == cert.STATUS_DOWNLOADED and old_status != cert.STATUS_DOWNLOADED:
        cert.downloaded_at = cert.downloaded_at or now
    elif cert.status == cert.STATUS_PDF_GENERATED and old_status != cert.STATUS_PDF_GENERATED:
        cert.downloaded_at = cert.downloaded_at or now
        cert.pdf_generated_at = cert.pdf_generated_at or now
    elif cert.status == cert.STATUS_SIGNED and old_status != cert.STATUS_SIGNED:
        cert.downloaded_at = cert.downloaded_at or now
        cert.pdf_generated_at = cert.pdf_generated_at or now
        cert.signed_at = cert.signed_at or now
    elif cert.status == cert.STATUS_ISSUED and old_status != cert.STATUS_ISSUED:
        cert.downloaded_at = cert.downloaded_at or now
        cert.pdf_generated_at = cert.pdf_generated_at or now
        cert.signed_at = cert.signed_at or now
        cert.issued_at = cert.issued_at or now
        cert.issued_by = user
        if not cert.issue_channel:
            cert.issue_channel = cert.CHANNEL_MANUAL
    cert.save()
    return cert


def _certificate_defaults(pack, row, certificate_type):
    return {
        "certificate_type": certificate_type,
        "deductee_name": (row.get("Deductee Name") or row.get("deductee_name") or "").strip()[:200],
        "deductee_pan": (row.get("Deductee PAN") or row.get("deductee_pan") or "").strip().upper()[:10],
        "section_code": (row.get("Section") or row.get("section") or "").strip()[:20],
        "amount_paid": _decimal_value(row.get("Amount Paid") or row.get("amount_paid")),
        "tds_amount": _decimal_value(row.get("TDS Amount") or row.get("tds_amount")),
    }


def _summary(pack, tracker, certificates, fy_start, quarter, form_type, period_start, period_end):
    expected_count = len((pack.export_snapshot or {}).get("deductee_rows") or []) if pack else 0
    issued_count = sum(1 for cert in certificates if cert.status == cert.STATUS_ISSUED)
    pending_count = max(len(certificates) - issued_count, 0)
    certificate_type = (
        TDSCertificateIssue.CERT_FORM16
        if form_type == TDSReturnWorkpaper.FORM_24Q
        else TDSCertificateIssue.CERT_FORM16A
    )
    return {
        "fy_start": fy_start,
        "fy_label": financial_year_label(fy_start),
        "quarter": quarter,
        "form_type": form_type,
        "period_start": period_start,
        "period_end": period_end,
        "pack_exists": bool(pack),
        "pack_status": pack.status if pack else "",
        "pack_is_filed": bool(pack and pack.status == TDSFilingPack.STATUS_FILED),
        "statement_status": tracker.statement_status if tracker else TDSPostFilingTracker.STATEMENT_NOT_CHECKED,
        "certificate_type": certificate_type,
        "expected_certificate_count": expected_count,
        "certificate_count": len(certificates),
        "issued_certificate_count": issued_count,
        "pending_certificate_count": pending_count,
        "correction_required": bool(tracker and tracker.correction_required),
        "can_issue_certificates": bool(
            pack
            and pack.status == TDSFilingPack.STATUS_FILED
            and tracker
            and tracker.statement_status in {
                TDSPostFilingTracker.STATEMENT_PROCESSED,
                TDSPostFilingTracker.STATEMENT_PROCESSED_DEFAULT,
            }
        ),
    }


def _validations(pack, tracker, certificates, summary):
    validations = []

    def add(severity, code, title, count, description, action_url=""):
        validations.append({
            "severity": severity,
            "code": code,
            "title": title,
            "count": count,
            "description": description,
            "action_url": action_url,
        })

    add(
        "ok" if pack else "critical",
        "filing_pack",
        "Filing Pack",
        0 if pack else 1,
        "TDS filing pack exists." if pack else "Generate the TDS filing pack before post-filing tracking.",
        reverse("tds:filing_pack"),
    )
    add(
        "ok" if summary["pack_is_filed"] else "critical",
        "filed_status",
        "Filed Status",
        0 if summary["pack_is_filed"] else 1,
        "Filing pack is marked filed." if summary["pack_is_filed"] else "Mark the filing pack filed with acknowledgement number.",
        reverse("tds:filing_pack"),
    )

    if not tracker:
        add("warning", "traces_tracker", "TRACES Tracker", 1, "TRACES post-filing tracker has not been saved yet.")
    elif tracker.statement_status == TDSPostFilingTracker.STATEMENT_PROCESSED:
        add("ok", "statement_status", "Statement Status", 0, "Statement is processed without default.")
    elif tracker.statement_status == TDSPostFilingTracker.STATEMENT_PROCESSED_DEFAULT:
        add("critical", "statement_status", "Statement Status", 1, "Statement is processed with default; review justification report and correction workflow.")
    elif tracker.statement_status == TDSPostFilingTracker.STATEMENT_REJECTED:
        add("critical", "statement_status", "Statement Status", 1, "Statement is rejected; correction or refiling action is required.")
    else:
        add("warning", "statement_status", "Statement Status", 1, "Statement processing status is not confirmed.")

    if tracker and tracker.correction_required:
        jr_clear = tracker.justification_report_status in {
            TDSPostFilingTracker.REPORT_DOWNLOADED,
            TDSPostFilingTracker.REPORT_REVIEWED,
        }
        add(
            "ok" if jr_clear else "critical",
            "justification_report",
            "Justification Report",
            0 if jr_clear else 1,
            "Justification report is downloaded/reviewed." if jr_clear else "Download and review justification report for defaults.",
        )
        conso_clear = tracker.conso_file_status in {
            TDSPostFilingTracker.REPORT_DOWNLOADED,
            TDSPostFilingTracker.REPORT_REVIEWED,
        }
        add(
            "ok" if conso_clear else "warning",
            "conso_file",
            "Conso File",
            0 if conso_clear else 1,
            "Conso file is downloaded/reviewed." if conso_clear else "Request/download conso file if correction return is needed.",
        )

    missing_certificates = max(summary["expected_certificate_count"] - summary["certificate_count"], 0)
    add(
        "ok" if missing_certificates == 0 else "warning",
        "certificate_sync",
        "Certificate Rows",
        missing_certificates,
        "Certificate rows are synced from the filing pack." if missing_certificates == 0 else "Sync certificate rows from the filing pack.",
    )
    add(
        "ok" if summary["pending_certificate_count"] == 0 and summary["certificate_count"] else "warning",
        "certificate_issue",
        "Certificate Issuance",
        summary["pending_certificate_count"],
        "All certificate rows are issued." if summary["pending_certificate_count"] == 0 and summary["certificate_count"] else "Some certificate rows are pending issue.",
    )
    return validations


def _int_value(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _decimal_value(value):
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except Exception:
        return ZERO
