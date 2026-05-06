from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.upload_validation import DOCUMENT_EXTENSIONS, validate_uploaded_file
from reports.utils import get_gst_report

from .compliance_workflow import sync_task_for_filing, sync_task_for_notice
from .models import (
    ComplianceFiling,
    ComplianceNotice,
    GSTEvidenceDocument,
    GSTFilingPack,
    GSTPostFilingTracker,
    PracticeTask,
)


FILED_RETURN_STATUSES = {
    GSTPostFilingTracker.STATUS_FILED,
    GSTPostFilingTracker.STATUS_ACCEPTED,
    GSTPostFilingTracker.STATUS_UNDER_NOTICE,
}


def build_gst_post_filing_center(company, period_start, period_end):
    pack = (
        GSTFilingPack.objects.filter(
            company=company,
            period_start=period_start,
            period_end=period_end,
        )
        .select_related("generated_by", "filed_by", "review")
        .first()
    )
    tracker = (
        GSTPostFilingTracker.objects.filter(
            company=company,
            period_start=period_start,
            period_end=period_end,
        )
        .select_related("pack", "updated_by")
        .first()
    )
    filings = _gst_filings(company, period_start, period_end)
    notices = _gst_notices(company, period_start, period_end, filings)
    evidence = _evidence_documents(company, period_start, period_end)
    report = get_gst_report(company, period_start, period_end)
    gstr2b = _gstr2b_summary(company, period_start, period_end)
    tracker_values = _tracker_values(tracker, pack, report, gstr2b)

    summary = _summary(pack, tracker, filings, notices, evidence, report, gstr2b, tracker_values)
    return {
        "company": company,
        "period_start": period_start,
        "period_end": period_end,
        "period_value": period_start.strftime("%Y-%m"),
        "pack": pack,
        "tracker": tracker,
        "tracker_values": tracker_values,
        "filings": filings,
        "filing_rows": _filing_rows(filings),
        "notices": notices,
        "evidence_documents": evidence,
        "report": report,
        "gstr2b": gstr2b,
        "summary": summary,
        "validations": _validations(pack, tracker, filings, notices, evidence, report, gstr2b, tracker_values),
    }


def build_gst_post_filing_dashboard(companies, period_start, period_end):
    rows = []
    for company in companies:
        center = build_gst_post_filing_center(company, period_start, period_end)
        rows.append({
            "company": company,
            "period_start": period_start,
            "period_end": period_end,
            "pack": center["pack"],
            "tracker": center["tracker"],
            "summary": center["summary"],
            "gstr2b": center["gstr2b"],
            "validations": center["validations"],
            "evidence_count": len(center["evidence_documents"]),
            "critical_count": sum(1 for item in center["validations"] if item["severity"] == "critical"),
            "warning_count": sum(1 for item in center["validations"] if item["severity"] == "warning"),
        })
    rows.sort(key=lambda row: (row["summary"]["score"], -row["critical_count"], row["company"].name))
    totals = {
        "companies": len(rows),
        "closed": sum(1 for row in rows if row["summary"]["score"] >= 80 and not row["critical_count"]),
        "critical": sum(row["critical_count"] for row in rows),
        "warnings": sum(row["warning_count"] for row in rows),
        "open_notices": sum(row["summary"]["open_notices"] for row in rows),
        "overdue_notices": sum(row["summary"]["overdue_notices"] for row in rows),
        "itc_at_risk": sum((row["summary"]["itc_at_risk"] for row in rows), Decimal("0.00")),
        "evidence_count": sum(row["evidence_count"] for row in rows),
        "missing_evidence": sum(1 for row in rows if row["summary"]["missing_evidence"]),
    }
    return {"rows": rows, "totals": totals}


@transaction.atomic
def save_gst_post_filing_tracker(company, period_start, period_end, user, data):
    pack = GSTFilingPack.objects.filter(
        company=company,
        period_start=period_start,
        period_end=period_end,
    ).first()
    report = get_gst_report(company, period_start, period_end)
    gstr2b = _gstr2b_summary(company, period_start, period_end)
    tracker, _ = GSTPostFilingTracker.objects.get_or_create(
        company=company,
        period_start=period_start,
        period_end=period_end,
        defaults={"pack": pack},
    )
    if pack and tracker.pack_id != pack.pk:
        tracker.pack = pack

    tracker.gstr1_status = _choice(data.get("gstr1_status"), GSTPostFilingTracker.RETURN_STATUS_CHOICES, tracker.gstr1_status)
    tracker.gstr1_arn = (data.get("gstr1_arn") or "").strip()
    tracker.gstr1_filed_at = _parse_datetime_value(data.get("gstr1_filed_at"))
    tracker.gstr3b_status = _choice(data.get("gstr3b_status"), GSTPostFilingTracker.RETURN_STATUS_CHOICES, tracker.gstr3b_status)
    tracker.gstr3b_arn = (data.get("gstr3b_arn") or "").strip()
    tracker.gstr3b_filed_at = _parse_datetime_value(data.get("gstr3b_filed_at"))
    tracker.ims_status = _choice(data.get("ims_status"), GSTPostFilingTracker.IMS_STATUS_CHOICES, tracker.ims_status)
    tracker.payment_status = _choice(
        data.get("payment_status"),
        GSTPostFilingTracker.PAYMENT_STATUS_CHOICES,
        _default_payment_status(report, tracker),
    )
    tracker.payment_challan_reference = (data.get("payment_challan_reference") or "").strip()
    tracker.payment_date = _parse_date_value(data.get("payment_date"))
    tracker.itc_at_risk = _decimal(data.get("itc_at_risk"), gstr2b["estimated_itc_at_risk"])
    tracker.portal_evidence_reference = (data.get("portal_evidence_reference") or "").strip()
    tracker.notes = (data.get("notes") or "").strip()
    tracker.updated_by = user
    tracker.save()

    _sync_filings_from_tracker(tracker, user)
    _sync_pack_from_tracker(tracker, user)
    return tracker


@transaction.atomic
def create_gst_notice_from_post_filing(company, period_start, period_end, user, data):
    title = (data.get("notice_title") or "").strip()
    if not title:
        raise ValueError("Notice title is required.")

    filing_type = data.get("notice_related_return") or ComplianceFiling.TYPE_GSTR3B
    related_filing = _ensure_filing(company, period_start, period_end, filing_type, user)
    notice = ComplianceNotice.objects.create(
        company=company,
        notice_type=ComplianceNotice.TYPE_GST,
        title=title,
        reference_number=(data.get("notice_reference") or "").strip(),
        issue_date=_parse_date_value(data.get("notice_issue_date")) or timezone.localdate(),
        response_due_date=_parse_date_value(data.get("notice_due_date")),
        status=_choice(data.get("notice_status"), ComplianceNotice.STATUS_CHOICES, ComplianceNotice.STATUS_RECEIVED),
        priority=_choice(data.get("notice_priority"), PracticeTask.PRIORITY_CHOICES, PracticeTask.PRIORITY_HIGH),
        assigned_to=user,
        created_by=user,
        related_filing=related_filing,
        portal_status=(data.get("notice_portal_status") or "").strip(),
        description=(data.get("notice_description") or "").strip(),
        response_summary=(data.get("notice_response_summary") or "").strip(),
    )
    sync_task_for_notice(notice, user=user)
    return notice


@transaction.atomic
def upload_gst_evidence(company, period_start, period_end, user, data, uploaded_file):
    if not uploaded_file:
        raise ValueError("Evidence file is required.")
    validate_uploaded_file(uploaded_file, allowed_extensions=DOCUMENT_EXTENSIONS, max_mb=20)

    pack = GSTFilingPack.objects.filter(
        company=company,
        period_start=period_start,
        period_end=period_end,
    ).first()
    tracker = (
        GSTPostFilingTracker.objects.filter(
            company=company,
            period_start=period_start,
            period_end=period_end,
        ).first()
    )
    if not tracker:
        tracker = GSTPostFilingTracker.objects.create(
            company=company,
            period_start=period_start,
            period_end=period_end,
            pack=pack,
            updated_by=user,
        )
    elif pack and tracker.pack_id != pack.pk:
        tracker.pack = pack
        tracker.updated_by = user
        tracker.save(update_fields=["pack", "updated_by", "updated_at"])

    evidence_type = _choice(data.get("evidence_type"), GSTEvidenceDocument.EVIDENCE_TYPE_CHOICES, GSTEvidenceDocument.TYPE_OTHER)
    return_type = _choice(data.get("return_type"), GSTEvidenceDocument.RETURN_TYPE_CHOICES, GSTEvidenceDocument.RETURN_OTHER)
    filing = _filing_for_return_type(company, period_start, period_end, return_type)
    notice = _notice_for_data(company, data)
    title = (data.get("evidence_title") or "").strip() or dict(GSTEvidenceDocument.EVIDENCE_TYPE_CHOICES).get(evidence_type, "GST Evidence")

    document = GSTEvidenceDocument.objects.create(
        company=company,
        period_start=period_start,
        period_end=period_end,
        tracker=tracker,
        pack=pack,
        filing=filing,
        notice=notice,
        evidence_type=evidence_type,
        return_type=return_type,
        title=title,
        file=uploaded_file,
        external_reference=(data.get("external_reference") or "").strip(),
        arn_ack_number=(data.get("evidence_arn_ack_number") or "").strip(),
        challan_reference=(data.get("evidence_challan_reference") or "").strip(),
        notes=(data.get("evidence_notes") or "").strip(),
        uploaded_by=user,
    )
    _apply_evidence_to_tracker(tracker, document, user)
    return document


@transaction.atomic
def update_gst_notice_from_post_filing(notice, user, data):
    notice.status = _choice(data.get("status"), ComplianceNotice.STATUS_CHOICES, notice.status)
    notice.portal_status = (data.get("portal_status") or "").strip()
    notice.response_summary = (data.get("response_summary") or "").strip()
    if notice.status == ComplianceNotice.STATUS_CLOSED:
        notice.closed_at = notice.closed_at or timezone.now()
        notice.closed_by = notice.closed_by or user
    else:
        notice.closed_at = None
        notice.closed_by = None
    notice.save()
    sync_task_for_notice(notice, user=user)
    return notice


def _tracker_values(tracker, pack, report, gstr2b):
    return {
        "gstr1_status": tracker.gstr1_status if tracker else _default_return_status(pack),
        "gstr1_arn": tracker.gstr1_arn if tracker else "",
        "gstr1_filed_at": tracker.gstr1_filed_at if tracker else None,
        "gstr3b_status": tracker.gstr3b_status if tracker else _default_return_status(pack),
        "gstr3b_arn": tracker.gstr3b_arn if tracker else (pack.arn_ack_number if pack else ""),
        "gstr3b_filed_at": tracker.gstr3b_filed_at if tracker else (pack.filed_at if pack else None),
        "ims_status": tracker.ims_status if tracker else (
            GSTPostFilingTracker.IMS_EXCEPTIONS if gstr2b["pending_actions"] else GSTPostFilingTracker.IMS_NOT_CHECKED
        ),
        "payment_status": tracker.payment_status if tracker else _default_payment_status(report, tracker),
        "payment_challan_reference": tracker.payment_challan_reference if tracker else "",
        "payment_date": tracker.payment_date if tracker else None,
        "itc_at_risk": tracker.itc_at_risk if tracker else gstr2b["estimated_itc_at_risk"],
        "portal_evidence_reference": tracker.portal_evidence_reference if tracker else "",
        "notes": tracker.notes if tracker else "",
    }


def _summary(pack, tracker, filings, notices, evidence, report, gstr2b, tracker_values):
    open_notices = [notice for notice in notices if notice.is_open]
    overdue_notices = [
        notice for notice in open_notices
        if notice.response_due_date and notice.response_due_date < timezone.localdate()
    ]
    ack_evidence = [
        document for document in evidence
        if document.evidence_type in {GSTEvidenceDocument.TYPE_GSTR1_ACK, GSTEvidenceDocument.TYPE_GSTR3B_ACK}
    ]
    challan_evidence = [document for document in evidence if document.evidence_type == GSTEvidenceDocument.TYPE_CHALLAN]
    notice_evidence = [
        document for document in evidence
        if document.evidence_type in {GSTEvidenceDocument.TYPE_NOTICE, GSTEvidenceDocument.TYPE_RESPONSE, GSTEvidenceDocument.TYPE_DRC03}
    ]
    missing_evidence = not ack_evidence
    if tracker_values["payment_status"] == GSTPostFilingTracker.PAYMENT_PAID and not challan_evidence:
        missing_evidence = True
    if open_notices and not notice_evidence:
        missing_evidence = True

    score = 100
    if not pack:
        score -= 25
    elif not pack.is_filed:
        score -= 15
    if tracker_values["gstr1_status"] not in FILED_RETURN_STATUSES:
        score -= 15
    if tracker_values["gstr3b_status"] not in FILED_RETURN_STATUSES:
        score -= 20
    if tracker_values["payment_status"] in {
        GSTPostFilingTracker.PAYMENT_PENDING,
        GSTPostFilingTracker.PAYMENT_SHORT_PAID,
    }:
        score -= 15
    if gstr2b["pending_actions"]:
        score -= min(20, gstr2b["pending_actions"] * 4)
    if overdue_notices:
        score -= min(25, len(overdue_notices) * 10)
    elif open_notices:
        score -= min(15, len(open_notices) * 5)
    if missing_evidence:
        score -= 10

    return {
        "score": max(0, score),
        "pack_status": pack.get_status_display() if pack else "Missing",
        "pack_is_filed": bool(pack and pack.is_filed),
        "arn": tracker_values["gstr3b_arn"] or tracker_values["gstr1_arn"] or (pack.arn_ack_number if pack else ""),
        "net_tax_payable": report["net_tax_payable"],
        "itc": report["tot_itc"],
        "itc_at_risk": tracker_values["itc_at_risk"],
        "pending_2b": gstr2b["pending_actions"],
        "open_notices": len(open_notices),
        "overdue_notices": len(overdue_notices),
        "tracker_saved": bool(tracker),
        "filings_open": sum(1 for filing in filings.values() if filing.is_open),
        "evidence_count": len(evidence),
        "ack_evidence_count": len(ack_evidence),
        "challan_evidence_count": len(challan_evidence),
        "notice_evidence_count": len(notice_evidence),
        "missing_evidence": missing_evidence,
    }


def _validations(pack, tracker, filings, notices, evidence, report, gstr2b, tracker_values):
    validations = []
    validations.append(_validation(
        "filing_pack",
        "GST filing pack",
        "ok" if pack and pack.is_filed else "critical",
        "GST filing pack is filed." if pack and pack.is_filed else "Mark the GST filing pack filed and capture ARN before period closure.",
        reverse("core:gst_filing_pack"),
    ))
    validations.append(_validation(
        "gstr1_arn",
        "GSTR-1 ARN",
        "ok" if tracker_values["gstr1_status"] in FILED_RETURN_STATUSES and tracker_values["gstr1_arn"] else "warning",
        "GSTR-1 ARN is captured." if tracker_values["gstr1_arn"] else "Capture GSTR-1 ARN/evidence from the GST portal.",
        reverse("core:gst_post_filing"),
    ))
    validations.append(_validation(
        "gstr3b_arn",
        "GSTR-3B ARN",
        "ok" if tracker_values["gstr3b_status"] in FILED_RETURN_STATUSES and tracker_values["gstr3b_arn"] else "critical",
        "GSTR-3B ARN is captured." if tracker_values["gstr3b_arn"] else "Capture GSTR-3B ARN/evidence from the GST portal.",
        reverse("core:gst_post_filing"),
    ))
    payment_clear = (
        tracker_values["payment_status"] in {
            GSTPostFilingTracker.PAYMENT_NOT_REQUIRED,
            GSTPostFilingTracker.PAYMENT_PAID,
        }
    )
    validations.append(_validation(
        "payment",
        "Tax payment",
        "ok" if payment_clear else "critical",
        "GST payment is settled or not required." if payment_clear else "Capture challan/CIN and mark tax payment settled.",
        reverse("core:gst_post_filing"),
    ))
    has_ack_evidence = any(
        document.evidence_type in {GSTEvidenceDocument.TYPE_GSTR1_ACK, GSTEvidenceDocument.TYPE_GSTR3B_ACK}
        for document in evidence
    )
    validations.append(_validation(
        "evidence_vault",
        "Evidence vault",
        "ok" if has_ack_evidence else "warning",
        "GST acknowledgement evidence is attached." if has_ack_evidence else "Upload ARN acknowledgement PDFs/screenshots to the GST evidence vault.",
        reverse("core:gst_post_filing"),
    ))
    validations.append(_validation(
        "ims_2b",
        "IMS / 2B follow-up",
        "ok" if not gstr2b["pending_actions"] and tracker_values["ims_status"] != GSTPostFilingTracker.IMS_EXCEPTIONS else "warning",
        "IMS/2B follow-up is clear." if not gstr2b["pending_actions"] else f"{gstr2b['pending_actions']} IMS/2B action(s) still need follow-up.",
        reverse("core:gst_workbench"),
    ))
    overdue = [
        notice for notice in notices
        if notice.is_open and notice.response_due_date and notice.response_due_date < timezone.localdate()
    ]
    validations.append(_validation(
        "notices",
        "GST notices",
        "critical" if overdue else ("warning" if any(notice.is_open for notice in notices) else "ok"),
        f"{len(overdue)} GST notice(s) are overdue." if overdue else "GST notices are tracked for this period.",
        reverse("core:compliance_notices"),
    ))
    return validations


def _validation(code, title, severity, description, action_url):
    return {
        "code": code,
        "title": title,
        "severity": severity,
        "description": description,
        "action_url": action_url,
    }


def _gst_filings(company, period_start, period_end):
    qs = ComplianceFiling.objects.filter(
        company=company,
        filing_type__in=[
            ComplianceFiling.TYPE_GST_IMS,
            ComplianceFiling.TYPE_GSTR1,
            ComplianceFiling.TYPE_GSTR3B,
        ],
        period_start=period_start,
        period_end=period_end,
    ).select_related("assigned_to", "reviewer", "filed_by", "related_task")
    return {filing.filing_type: filing for filing in qs}


def _gst_notices(company, period_start, period_end, filings):
    filing_ids = [filing.pk for filing in filings.values()]
    query = Q(issue_date__gte=period_start, issue_date__lte=period_end) | Q(
        response_due_date__gte=period_start,
        response_due_date__lte=period_end,
    )
    if filing_ids:
        query |= Q(related_filing_id__in=filing_ids)
    return list(
        ComplianceNotice.objects.filter(company=company, notice_type=ComplianceNotice.TYPE_GST)
        .filter(query)
        .select_related("assigned_to", "related_filing", "related_task")
        .distinct()
        .order_by("status", "response_due_date", "-priority")
    )


def _evidence_documents(company, period_start, period_end):
    return list(
        GSTEvidenceDocument.objects.filter(
            company=company,
            period_start=period_start,
            period_end=period_end,
        )
        .select_related("uploaded_by", "filing", "notice", "tracker", "pack")
        .order_by("-uploaded_at")
    )


def _filing_rows(filings):
    rows = []
    for filing_type, label in [
        (ComplianceFiling.TYPE_GST_IMS, "GST IMS Review"),
        (ComplianceFiling.TYPE_GSTR1, "GSTR-1"),
        (ComplianceFiling.TYPE_GSTR3B, "GSTR-3B"),
    ]:
        rows.append({"filing_type": filing_type, "label": label, "filing": filings.get(filing_type)})
    return rows


def _filing_for_return_type(company, period_start, period_end, return_type):
    filing_type_map = {
        GSTEvidenceDocument.RETURN_GSTR1: ComplianceFiling.TYPE_GSTR1,
        GSTEvidenceDocument.RETURN_GSTR3B: ComplianceFiling.TYPE_GSTR3B,
        GSTEvidenceDocument.RETURN_IMS: ComplianceFiling.TYPE_GST_IMS,
    }
    filing_type = filing_type_map.get(return_type)
    if not filing_type:
        return None
    return ComplianceFiling.objects.filter(
        company=company,
        filing_type=filing_type,
        period_start=period_start,
        period_end=period_end,
    ).first()


def _notice_for_data(company, data):
    notice_id = data.get("notice_id") or data.get("evidence_notice_id")
    if not notice_id:
        return None
    return ComplianceNotice.objects.filter(company=company, pk=notice_id, notice_type=ComplianceNotice.TYPE_GST).first()


def _apply_evidence_to_tracker(tracker, document, user):
    update_fields = ["updated_by", "updated_at"]
    tracker.updated_by = user
    if document.evidence_type == GSTEvidenceDocument.TYPE_GSTR1_ACK:
        if document.arn_ack_number:
            tracker.gstr1_arn = document.arn_ack_number
            update_fields.append("gstr1_arn")
        if tracker.gstr1_status in {
            GSTPostFilingTracker.STATUS_NOT_CHECKED,
            GSTPostFilingTracker.STATUS_PENDING,
        }:
            tracker.gstr1_status = GSTPostFilingTracker.STATUS_FILED
            update_fields.append("gstr1_status")
    elif document.evidence_type == GSTEvidenceDocument.TYPE_GSTR3B_ACK:
        if document.arn_ack_number:
            tracker.gstr3b_arn = document.arn_ack_number
            update_fields.append("gstr3b_arn")
        if tracker.gstr3b_status in {
            GSTPostFilingTracker.STATUS_NOT_CHECKED,
            GSTPostFilingTracker.STATUS_PENDING,
        }:
            tracker.gstr3b_status = GSTPostFilingTracker.STATUS_FILED
            update_fields.append("gstr3b_status")
    elif document.evidence_type == GSTEvidenceDocument.TYPE_CHALLAN:
        if document.challan_reference:
            tracker.payment_challan_reference = document.challan_reference
            update_fields.append("payment_challan_reference")
        if tracker.payment_status == GSTPostFilingTracker.PAYMENT_PENDING:
            tracker.payment_status = GSTPostFilingTracker.PAYMENT_PAID
            update_fields.append("payment_status")
    if document.external_reference:
        tracker.portal_evidence_reference = document.external_reference
        update_fields.append("portal_evidence_reference")
    tracker.save(update_fields=sorted(set(update_fields)))
    _sync_filings_from_tracker(tracker, user)
    _sync_pack_from_tracker(tracker, user)


def _gstr2b_summary(company, period_start, period_end):
    from gstr2b.models import PortalGSTR2BEntry

    entries = PortalGSTR2BEntry.objects.filter(
        company=company,
        invoice_date__gte=period_start,
        invoice_date__lte=period_end,
    )
    pending = entries.filter(action_status__in=["pending", "rejected"])
    missing_in_books = entries.filter(match_status="missing_in_books")
    estimated_risk = pending.aggregate(total=Sum("tax_amount"))["total"] or Decimal("0.00")
    estimated_risk += missing_in_books.aggregate(total=Sum("tax_amount"))["total"] or Decimal("0.00")
    return {
        "entries": entries.count(),
        "matched": entries.filter(match_status="matched").count(),
        "missing_in_books": missing_in_books.count(),
        "pending_actions": pending.count(),
        "estimated_itc_at_risk": estimated_risk,
    }


def _sync_filings_from_tracker(tracker, user):
    _sync_return_filing(
        tracker,
        user,
        ComplianceFiling.TYPE_GSTR1,
        "GSTR-1",
        tracker.gstr1_status,
        tracker.gstr1_arn,
        tracker.gstr1_filed_at,
    )
    _sync_return_filing(
        tracker,
        user,
        ComplianceFiling.TYPE_GSTR3B,
        "GSTR-3B",
        tracker.gstr3b_status,
        tracker.gstr3b_arn,
        tracker.gstr3b_filed_at,
    )
    ims_status_map = {
        GSTPostFilingTracker.IMS_NOT_CHECKED: ComplianceFiling.STATUS_NOT_STARTED,
        GSTPostFilingTracker.IMS_IN_PROGRESS: ComplianceFiling.STATUS_IN_PROGRESS,
        GSTPostFilingTracker.IMS_COMPLETED: ComplianceFiling.STATUS_FILED,
        GSTPostFilingTracker.IMS_EXCEPTIONS: ComplianceFiling.STATUS_BLOCKED,
    }
    filing = _ensure_filing(
        tracker.company,
        tracker.period_start,
        tracker.period_end,
        ComplianceFiling.TYPE_GST_IMS,
        user,
    )
    filing.status = ims_status_map.get(tracker.ims_status, ComplianceFiling.STATUS_NOT_STARTED)
    filing.portal_status = tracker.get_ims_status_display()
    if filing.status == ComplianceFiling.STATUS_FILED:
        filing.filed_by = filing.filed_by or user
        filing.filed_at = filing.filed_at or timezone.now()
    filing.save()
    sync_task_for_filing(filing, user=user)


def _sync_return_filing(tracker, user, filing_type, label, return_status, arn, filed_at):
    filing = _ensure_filing(tracker.company, tracker.period_start, tracker.period_end, filing_type, user)
    filing.portal_status = dict(GSTPostFilingTracker.RETURN_STATUS_CHOICES).get(return_status, return_status)
    filing.arn_ack_number = arn
    if return_status in FILED_RETURN_STATUSES:
        filing.status = ComplianceFiling.STATUS_FILED
        filing.filed_by = filing.filed_by or user
        filing.filed_at = filed_at or filing.filed_at or timezone.now()
    elif return_status == GSTPostFilingTracker.STATUS_PENDING:
        filing.status = ComplianceFiling.STATUS_IN_PROGRESS
        filing.filed_by = None
        filing.filed_at = None
    else:
        filing.status = ComplianceFiling.STATUS_NOT_STARTED
        filing.filed_by = None
        filing.filed_at = None
    filing.title = f"{label} - {tracker.period_start:%b %Y}"
    filing.save()
    sync_task_for_filing(filing, user=user)


def _sync_pack_from_tracker(tracker, user):
    pack = tracker.pack
    if not pack:
        return
    if tracker.gstr1_status in FILED_RETURN_STATUSES and tracker.gstr3b_status in FILED_RETURN_STATUSES:
        pack.status = GSTFilingPack.STATUS_FILED
        pack.arn_ack_number = tracker.gstr3b_arn or tracker.gstr1_arn or pack.arn_ack_number
        pack.filed_by = pack.filed_by or user
        pack.filed_at = tracker.gstr3b_filed_at or tracker.gstr1_filed_at or pack.filed_at or timezone.now()
        pack.save()


def _ensure_filing(company, period_start, period_end, filing_type, user):
    labels = {
        ComplianceFiling.TYPE_GST_IMS: "GST IMS Review",
        ComplianceFiling.TYPE_GSTR1: "GSTR-1",
        ComplianceFiling.TYPE_GSTR3B: "GSTR-3B",
    }
    filing, _ = ComplianceFiling.objects.get_or_create(
        company=company,
        filing_type=filing_type,
        period_start=period_start,
        period_end=period_end,
        defaults={
            "title": f"{labels.get(filing_type, 'GST Filing')} - {period_start:%b %Y}",
            "status": ComplianceFiling.STATUS_NOT_STARTED,
            "priority": PracticeTask.PRIORITY_NORMAL,
            "due_date": _due_date(period_start, filing_type),
            "created_by": user,
            "source": ComplianceFiling.SOURCE_PORTAL,
        },
    )
    return filing


def _due_date(period_start, filing_type):
    day = 10
    if filing_type == ComplianceFiling.TYPE_GSTR1:
        day = 11
    elif filing_type == ComplianceFiling.TYPE_GSTR3B:
        day = 20
    year = period_start.year + (1 if period_start.month == 12 else 0)
    month = 1 if period_start.month == 12 else period_start.month + 1
    return date(year, month, day)


def _default_return_status(pack):
    if pack and pack.is_filed:
        return GSTPostFilingTracker.STATUS_FILED
    if pack:
        return GSTPostFilingTracker.STATUS_PENDING
    return GSTPostFilingTracker.STATUS_NOT_CHECKED


def _default_payment_status(report, tracker):
    if tracker:
        return tracker.payment_status
    return (
        GSTPostFilingTracker.PAYMENT_PENDING
        if report["net_tax_payable"] and report["net_tax_payable"] > Decimal("0.00")
        else GSTPostFilingTracker.PAYMENT_NOT_REQUIRED
    )


def _choice(value, choices, fallback):
    allowed = {item[0] for item in choices}
    return value if value in allowed else fallback


def _decimal(value, fallback):
    if value in (None, ""):
        return fallback
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return fallback


def _parse_date_value(value):
    if not value:
        return None
    return parse_date(value)


def _parse_datetime_value(value):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed
