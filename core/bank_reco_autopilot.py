"""Cross-client bank reconciliation autopilot and exception queue."""

import csv
import io
import zipfile
from datetime import date as date_cls, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from .models import AuditLog, BankStatement, PracticeTask


ZERO = Decimal("0.00")
TASK_REFERENCE_PREFIX = "BANKAUTO"
STALE_PENDING_DAYS = 7


def build_bank_reco_autopilot(companies, *, as_of_date=None, focus="attention"):
    as_of_date = as_of_date or timezone.localdate()
    company_list = list(companies)
    statements = _statements_for_companies(company_list)

    statements_by_company = {}
    for statement in statements:
        statements_by_company.setdefault(statement.company_id, []).append(statement)

    company_rows = []
    statement_rows = []
    for company in company_list:
        company_statements = statements_by_company.get(company.pk, [])
        row = _company_row(company, company_statements, as_of_date)
        company_rows.append(row)
        if company_statements:
            statement_rows.extend(_statement_row(statement, as_of_date) for statement in company_statements)
        else:
            statement_rows.append(_missing_statement_row(company))

    _attach_task_flags(company_rows, statement_rows)
    _attach_client_request_counts(company_rows, statement_rows)
    company_rows = _filter_rows(company_rows, focus)
    statement_rows = _filter_rows(statement_rows, focus)
    company_rows.sort(key=_sort_key)
    statement_rows.sort(key=_statement_sort_key)

    return {
        "company_rows": company_rows,
        "statement_rows": statement_rows,
        "totals": _totals(company_rows, statement_rows),
        "as_of_date": as_of_date,
        "focus": focus,
    }


def bank_reco_autopilot_csv_response(center):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="bank_reco_autopilot.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "Statement Date",
        "Bank Account",
        "Status",
        "Rows",
        "Pending Rows",
        "Pending Amount",
        "Duplicate Rows",
        "High Confidence Rows",
        "Suggested Ledger Rows",
        "Auto Postable Rows",
        "Stale Pending Rows",
        "Missing Bank Ledger",
        "Task Exists",
        "Client Requests",
        "Open Client Requests",
        "Uploaded Evidence",
    ])
    for row in center["statement_rows"]:
        writer.writerow([
            row["company"].name,
            row["statement"].statement_date.isoformat() if row["statement"] else "",
            row["bank_account_name"],
            row["status_label"],
            row["row_count"],
            row["pending_count"],
            f"{row['pending_amount']:.2f}",
            row["duplicate_count"],
            row["high_confidence_count"],
            row["suggested_count"],
            row["auto_postable_count"],
            row["stale_count"],
            "Yes" if row["missing_bank_ledger"] else "No",
            "Yes" if row["task_exists"] else "No",
            row["client_request_count"],
            row["client_request_open_count"],
            row["client_request_uploaded_count"],
        ])
    return response


def bank_reco_working_paper_zip_response(center):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bank_reco_summary.csv", _working_paper_summary_csv(center))
        archive.writestr("bank_reco_exceptions.csv", _working_paper_exceptions_csv(center))
        archive.writestr("bank_reco_client_requests.csv", _working_paper_client_requests_csv(center))
        archive.writestr("README.txt", _working_paper_readme(center))

    response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="bank_reco_working_paper_pack.zip"'
    return response


def create_bank_reco_tasks(work_rows, user, manageable_company_ids, selected_keys=None):
    selected_keys = {value for value in selected_keys or [] if value}
    created = 0
    existing = 0
    skipped = 0
    as_of_date = timezone.localdate()

    for row in work_rows:
        if row["company"].pk not in manageable_company_ids:
            skipped += 1
            continue
        if selected_keys and row["selection_key"] not in selected_keys:
            continue
        if not selected_keys and not row["needs_attention"]:
            continue
        task, was_created = PracticeTask.objects.get_or_create(
            company=row["company"],
            reference=row["task_reference"],
            defaults={
                "title": _task_title(row),
                "task_type": PracticeTask.TYPE_BANK,
                "priority": row["task_priority"],
                "status": PracticeTask.STATUS_OPEN,
                "due_date": as_of_date,
                "created_by": user,
                "description": _task_description(row),
            },
        )
        if was_created:
            created += 1
        else:
            existing += 1
    return {"created": created, "existing": existing, "skipped": skipped}


def create_bank_reco_client_requests(work_rows, user, manageable_company_ids, selected_keys=None, *, as_of_date=None):
    from portal.models import ClientDocumentRequest

    selected_keys = {value for value in selected_keys or [] if value}
    as_of_date = as_of_date or timezone.localdate()
    created = 0
    existing = 0
    task_created = 0
    task_existing = 0
    skipped = 0

    for work_row in work_rows:
        if selected_keys and work_row["selection_key"] not in selected_keys:
            continue
        if not selected_keys and not work_row["needs_attention"]:
            continue
        if work_row["company"].pk not in manageable_company_ids:
            skipped += 1
            continue

        if not work_row["statement"]:
            doc_request, was_created = _create_missing_statement_request(work_row, user)
            if was_created:
                created += 1
                _audit_client_request(doc_request, user, "missing_statement")
            else:
                existing += 1
            new_tasks, old_tasks = _ensure_client_request_task(doc_request, user)
            task_created += new_tasks
            task_existing += old_tasks
            continue

        candidate_rows = _client_request_candidate_bank_rows(work_row["statement"], as_of_date)
        if not candidate_rows:
            skipped += 1
            continue
        for bank_row in candidate_rows:
            source_reference = _bank_row_source_reference(bank_row)
            doc_request, was_created = ClientDocumentRequest.objects.get_or_create(
                company=bank_row.statement.company,
                source_reference=source_reference,
                defaults=_bank_row_request_defaults(bank_row, user),
            )
            if was_created:
                created += 1
                _audit_client_request(doc_request, user, "bank_row")
            else:
                existing += 1
            new_tasks, old_tasks = _ensure_client_request_task(doc_request, user)
            task_created += new_tasks
            task_existing += old_tasks

    return {
        "created": created,
        "existing": existing,
        "task_created": task_created,
        "task_existing": task_existing,
        "skipped": skipped,
    }


def close_bank_reco_uploaded_evidence(work_rows, user, manageable_company_ids, selected_keys=None):
    from portal.models import ClientDocumentRequest

    selected_keys = {value for value in selected_keys or [] if value}
    uploaded_requests = []
    skipped = 0

    for work_row in work_rows:
        if selected_keys and work_row["selection_key"] not in selected_keys:
            continue
        if not selected_keys and not work_row.get("client_request_uploaded_count"):
            continue
        if work_row["company"].pk not in manageable_company_ids:
            skipped += 1
            continue
        source_refs = _client_request_source_refs_for_work_row(work_row)
        if not source_refs:
            skipped += 1
            continue
        uploaded_requests.extend(
            ClientDocumentRequest.objects.filter(
                company=work_row["company"],
                source_reference__in=source_refs,
                document_type=ClientDocumentRequest.TYPE_BANK,
                status=ClientDocumentRequest.STATUS_UPLOADED,
            ).select_related("related_task")
        )

    closed = 0
    task_closed = 0
    now = timezone.now()
    seen_request_ids = set()
    for doc_request in uploaded_requests:
        if doc_request.pk in seen_request_ids:
            continue
        seen_request_ids.add(doc_request.pk)
        old_data = {
            "status": doc_request.status,
            "related_task_id": doc_request.related_task_id,
            "uploaded_submission_id": doc_request.uploaded_submission_id,
        }
        doc_request.status = ClientDocumentRequest.STATUS_CLOSED
        doc_request.closed_at = now
        doc_request.save(update_fields=["status", "closed_at", "updated_at"])
        closed += 1

        if doc_request.related_task_id:
            task = doc_request.related_task
            task.status = PracticeTask.STATUS_DONE
            task.completed_by = user
            task.completed_at = now
            task.description = (task.description + "\n\nBank evidence reviewed and request closed.").strip()
            task.save(update_fields=["status", "completed_by", "completed_at", "description", "updated_at"])
            task_closed += 1

        _mark_bank_row_evidence_reviewed(doc_request.source_reference)
        AuditLog.objects.create(
            company=doc_request.company,
            user=user,
            action=AuditLog.ACTION_UPDATE,
            model_name="ClientDocumentRequest",
            record_id=doc_request.pk,
            object_repr=doc_request.title[:200],
            old_data=old_data,
            new_data={
                "status": doc_request.status,
                "closed_at": doc_request.closed_at.isoformat() if doc_request.closed_at else "",
                "source": "bank_reco_autopilot_close_evidence",
                "source_reference": doc_request.source_reference,
                "uploaded_submission_id": doc_request.uploaded_submission_id,
                "related_task_id": doc_request.related_task_id,
            },
        )

    return {"closed": closed, "task_closed": task_closed, "skipped": skipped}


def post_bank_reco_auto_ready_vouchers(work_rows, user, manageable_company_ids, selected_keys=None):
    selected_keys = {value for value in selected_keys or [] if value}
    created = 0
    skipped = 0
    failed = 0
    task_closed = 0
    errors = []
    seen_statement_ids = set()

    for work_row in work_rows:
        if selected_keys and work_row["selection_key"] not in selected_keys:
            continue
        if work_row["company"].pk not in manageable_company_ids:
            skipped += 1
            continue
        if not work_row["statement"]:
            skipped += 1
            continue
        if not selected_keys and not work_row.get("auto_postable_count"):
            continue
        statement_id = work_row["statement"].pk
        if statement_id in seen_statement_ids:
            continue
        seen_statement_ids.add(statement_id)

        rows = _auto_postable_bank_rows(work_row["statement"])
        if not rows:
            skipped += 1
            continue
        for row in rows:
            try:
                with transaction.atomic():
                    locked_row = _lock_bank_row_for_auto_post(row.pk)
                    _create_voucher_from_auto_ready_bank_row(locked_row, user)
                    created += 1
            except Exception as exc:
                failed += 1
                if len(errors) < 3:
                    errors.append(_format_auto_post_error(row, exc))
        task_closed += _close_completed_bank_statement_task(work_row["statement"], user)

    return {"created": created, "skipped": skipped, "failed": failed, "task_closed": task_closed, "errors": errors}


def bank_reco_focus_choices():
    return [
        ("attention", "Needs Attention"),
        ("critical", "Critical"),
        ("auto_ready", "Auto Ready"),
        ("duplicates", "Duplicates"),
        ("client_waiting", "Waiting on Client"),
        ("client_uploaded", "Uploaded Evidence"),
        ("no_statement", "No Statement"),
        ("clean", "Clean"),
        ("all", "All"),
    ]


def _statements_for_companies(companies):
    return list(
        BankStatement.objects.filter(company__in=companies)
        .select_related("company", "account_ledger")
        .prefetch_related("rows")
        .order_by("company__name", "-statement_date", "-uploaded_at", "-id")
    )


def _company_row(company, statements, as_of_date):
    metrics = _empty_metrics()
    latest_statement = statements[0] if statements else None
    for statement in statements:
        statement_metrics = _metrics_for_rows(statement.rows.all(), as_of_date)
        for key in metrics:
            metrics[key] += statement_metrics[key]
        if not statement.account_ledger_id:
            metrics["missing_bank_ledger"] += 1

    row = {
        "company": company,
        "statement": latest_statement,
        "selection_key": f"company:{company.pk}",
        "task_reference": f"{TASK_REFERENCE_PREFIX}:{company.pk}:CLIENT",
        "statement_count": len(statements),
        "latest_statement_date": latest_statement.statement_date if latest_statement else None,
        "bank_account_name": latest_statement.account_ledger.name if latest_statement and latest_statement.account_ledger_id else "",
        "action_url": _company_action_url(company, latest_statement),
        "report_url": _switch_url(company, reverse("core:bank_reconciliation_report")),
        "upload_url": _switch_url(company, reverse("core:bank_statement_upload")),
        "client_request_url": _client_request_url(company),
        **metrics,
    }
    _classify_row(row)
    return row


def _statement_row(statement, as_of_date):
    metrics = _metrics_for_rows(statement.rows.all(), as_of_date)
    if not statement.account_ledger_id:
        metrics["missing_bank_ledger"] = 1
    row = {
        "company": statement.company,
        "statement": statement,
        "selection_key": f"statement:{statement.pk}",
        "task_reference": f"{TASK_REFERENCE_PREFIX}:{statement.company_id}:STMT:{statement.pk}",
        "statement_count": 1,
        "latest_statement_date": statement.statement_date,
        "bank_account_name": statement.account_ledger.name if statement.account_ledger_id else "Missing bank ledger",
        "action_url": _switch_url(statement.company, reverse("core:bank_statement_detail", args=[statement.pk])),
        "report_url": _switch_url(statement.company, reverse("core:bank_reconciliation_report")),
        "upload_url": _switch_url(statement.company, reverse("core:bank_statement_upload")),
        "client_request_url": _client_request_url(statement.company),
        **metrics,
    }
    _classify_row(row)
    return row


def _missing_statement_row(company):
    row = {
        "company": company,
        "statement": None,
        "selection_key": f"company:{company.pk}:no_statement",
        "task_reference": f"{TASK_REFERENCE_PREFIX}:{company.pk}:NO_STATEMENT",
        "statement_count": 0,
        "latest_statement_date": None,
        "bank_account_name": "",
        "action_url": _switch_url(company, reverse("core:bank_statement_upload")),
        "report_url": _switch_url(company, reverse("core:bank_reconciliation_report")),
        "upload_url": _switch_url(company, reverse("core:bank_statement_upload")),
        "client_request_url": _client_request_url(company),
        **_empty_metrics(),
    }
    row["status"] = "no_statement"
    row["status_label"] = "No Statement"
    row["status_class"] = "danger"
    row["status_rank"] = 0
    row["needs_attention"] = True
    row["task_priority"] = PracticeTask.PRIORITY_HIGH
    row["task_exists"] = False
    _set_empty_client_request_counts(row)
    return row


def _metrics_for_rows(rows, as_of_date):
    metrics = _empty_metrics()
    for row in rows:
        amount = abs(row.amount)
        metrics["row_count"] += 1
        if not row.is_reconciled:
            metrics["pending_count"] += 1
            metrics["pending_amount"] += amount
            if (as_of_date - row.date).days > STALE_PENDING_DAYS:
                metrics["stale_count"] += 1
            if row.suggested_ledger_id:
                metrics["suggested_count"] += 1
            if row.match_confidence >= 70:
                metrics["high_confidence_count"] += 1
            if row.suggested_ledger_id and row.match_confidence >= 70 and not row.potential_duplicate:
                metrics["auto_postable_count"] += 1
        if row.potential_duplicate:
            metrics["duplicate_count"] += 1
    return metrics


def _empty_metrics():
    return {
        "row_count": 0,
        "pending_count": 0,
        "pending_amount": ZERO,
        "duplicate_count": 0,
        "suggested_count": 0,
        "high_confidence_count": 0,
        "auto_postable_count": 0,
        "stale_count": 0,
        "missing_bank_ledger": 0,
    }


def _classify_row(row):
    if row["statement_count"] == 0:
        row["status"] = "no_statement"
        row["status_label"] = "No Statement"
        row["status_class"] = "danger"
        row["status_rank"] = 0
        row["task_priority"] = PracticeTask.PRIORITY_HIGH
    elif row["missing_bank_ledger"]:
        row["status"] = "critical"
        row["status_label"] = "Missing Ledger"
        row["status_class"] = "danger"
        row["status_rank"] = 0
        row["task_priority"] = PracticeTask.PRIORITY_CRITICAL
    elif row["duplicate_count"]:
        row["status"] = "critical"
        row["status_label"] = "Duplicate Review"
        row["status_class"] = "danger"
        row["status_rank"] = 0
        row["task_priority"] = PracticeTask.PRIORITY_CRITICAL
    elif row["stale_count"]:
        row["status"] = "critical"
        row["status_label"] = "Stale Pending"
        row["status_class"] = "danger"
        row["status_rank"] = 0
        row["task_priority"] = PracticeTask.PRIORITY_HIGH
    elif row["high_confidence_count"] or row["suggested_count"]:
        row["status"] = "auto_ready"
        row["status_label"] = "Auto Ready"
        row["status_class"] = "primary"
        row["status_rank"] = 1
        row["task_priority"] = PracticeTask.PRIORITY_NORMAL
    elif row["pending_count"]:
        row["status"] = "pending"
        row["status_label"] = "Needs Matching"
        row["status_class"] = "warning"
        row["status_rank"] = 2
        row["task_priority"] = PracticeTask.PRIORITY_NORMAL
    else:
        row["status"] = "clean"
        row["status_label"] = "Clean"
        row["status_class"] = "success"
        row["status_rank"] = 3
        row["task_priority"] = PracticeTask.PRIORITY_LOW
    row["needs_attention"] = row["status"] != "clean"
    row["task_exists"] = False


def _attach_task_flags(company_rows, statement_rows):
    references = [row["task_reference"] for row in [*company_rows, *statement_rows]]
    existing_refs = set(
        PracticeTask.objects.filter(reference__in=references)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
        .values_list("reference", flat=True)
    )
    for row in [*company_rows, *statement_rows]:
        row["task_exists"] = row["task_reference"] in existing_refs


def _attach_client_request_counts(company_rows, statement_rows):
    from portal.models import ClientDocumentRequest

    ref_map = {}
    company_refs = {}
    for row in statement_rows:
        refs = _client_request_source_refs_for_work_row(row)
        ref_map[row["selection_key"]] = refs
        company_refs.setdefault(row["company"].pk, set()).update(refs)

    all_refs = sorted({ref for refs in ref_map.values() for ref in refs})
    request_rows = list(
        ClientDocumentRequest.objects.filter(source_reference__in=all_refs)
        .values_list("source_reference", "status")
    )
    counts_by_ref = {}
    for source_reference, status in request_rows:
        counts = counts_by_ref.setdefault(source_reference, _empty_client_request_counts())
        if status == ClientDocumentRequest.STATUS_OPEN:
            counts["client_request_count"] += 1
            counts["client_request_open_count"] += 1
        elif status == ClientDocumentRequest.STATUS_UPLOADED:
            counts["client_request_count"] += 1
            counts["client_request_uploaded_count"] += 1
        elif status == ClientDocumentRequest.STATUS_CLOSED:
            counts["client_request_closed_count"] += 1
        elif status == ClientDocumentRequest.STATUS_CANCELLED:
            counts["client_request_cancelled_count"] += 1

    for row in statement_rows:
        _set_aggregate_client_request_counts(row, ref_map.get(row["selection_key"], []), counts_by_ref)
    for row in company_rows:
        _set_aggregate_client_request_counts(row, company_refs.get(row["company"].pk, set()), counts_by_ref)


def _empty_client_request_counts():
    return {
        "client_request_count": 0,
        "client_request_open_count": 0,
        "client_request_uploaded_count": 0,
        "client_request_closed_count": 0,
        "client_request_cancelled_count": 0,
    }


def _set_empty_client_request_counts(row):
    row.update(_empty_client_request_counts())


def _set_aggregate_client_request_counts(row, refs, counts_by_ref):
    totals = _empty_client_request_counts()
    for ref in refs:
        counts = counts_by_ref.get(ref)
        if not counts:
            continue
        for key in totals:
            totals[key] += counts[key]
    row.update(totals)


def _client_request_source_refs_for_work_row(work_row):
    if work_row["statement"]:
        return [
            _bank_row_source_reference(bank_row)
            for bank_row in work_row["statement"].rows.all()
            if not bank_row.is_reconciled
        ]
    return [_missing_statement_source_reference(work_row["company"])]


def _working_paper_request_map(center):
    from portal.models import ClientDocumentRequest

    refs = []
    for work_row in center["statement_rows"]:
        refs.extend(_client_request_source_refs_for_work_row(work_row))
    requests = (
        ClientDocumentRequest.objects.filter(source_reference__in=refs)
        .select_related("company", "portal_user", "uploaded_submission", "related_task")
        .order_by("company__name", "source_reference", "status", "-created_at")
    )
    request_map = {}
    for doc_request in requests:
        request_map.setdefault(doc_request.source_reference, []).append(doc_request)
    return request_map


def _csv_string(headers, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue()


def _working_paper_summary_csv(center):
    headers = [
        "Section",
        "Company",
        "Statement Date",
        "Bank Account",
        "Status",
        "Rows",
        "Pending Rows",
        "Pending Amount",
        "Duplicates",
        "Auto Ready",
        "Auto Postable",
        "Waiting Client Requests",
        "Uploaded Evidence",
    ]
    rows = [
        [
            "Totals",
            "",
            center["as_of_date"].isoformat(),
            "",
            f"Reco Score {center['totals']['autopilot_score']}%",
            center["totals"]["row_count"],
            center["totals"]["pending_count"],
            f"{center['totals']['pending_amount']:.2f}",
            center["totals"]["duplicate_count"],
            center["totals"]["high_confidence_count"],
            center["totals"]["auto_postable_count"],
            center["totals"]["client_request_open_count"],
            center["totals"]["client_request_uploaded_count"],
        ]
    ]
    for row in center["statement_rows"]:
        rows.append([
            "Statement",
            row["company"].name,
            row["latest_statement_date"].isoformat() if row["latest_statement_date"] else "",
            row["bank_account_name"],
            row["status_label"],
            row["row_count"],
            row["pending_count"],
            f"{row['pending_amount']:.2f}",
            row["duplicate_count"],
            row["high_confidence_count"],
            row["auto_postable_count"],
            row["client_request_open_count"],
            row["client_request_uploaded_count"],
        ])
    return _csv_string(headers, rows)


def _working_paper_exceptions_csv(center):
    request_map = _working_paper_request_map(center)
    headers = [
        "Company",
        "Statement Date",
        "Bank Account",
        "Row Date",
        "Description",
        "Debit",
        "Credit",
        "Balance",
        "Match Confidence",
        "Match Reason",
        "Suggested Ledger",
        "Potential Duplicate",
        "Client Request Status",
        "Evidence File",
        "Source Reference",
    ]
    rows = []
    for work_row in center["statement_rows"]:
        statement = work_row["statement"]
        if not statement:
            source_reference = _missing_statement_source_reference(work_row["company"])
            rows.append([
                work_row["company"].name,
                "",
                "",
                "",
                "No bank statement uploaded",
                "",
                "",
                "",
                "",
                "Statement upload pending",
                "",
                "",
                _request_status_summary(request_map.get(source_reference, [])),
                _request_evidence_summary(request_map.get(source_reference, [])),
                source_reference,
            ])
            continue
        for bank_row in statement.rows.all():
            if bank_row.is_reconciled:
                continue
            source_reference = _bank_row_source_reference(bank_row)
            linked_requests = request_map.get(source_reference, [])
            rows.append([
                statement.company.name,
                statement.statement_date.isoformat(),
                statement.account_ledger.name if statement.account_ledger_id else "",
                bank_row.date.isoformat(),
                bank_row.description,
                f"{bank_row.debit:.2f}",
                f"{bank_row.credit:.2f}",
                f"{bank_row.balance:.2f}" if bank_row.balance is not None else "",
                bank_row.match_confidence,
                bank_row.match_reason,
                bank_row.suggested_ledger.name if bank_row.suggested_ledger_id else "",
                "Yes" if bank_row.potential_duplicate else "No",
                _request_status_summary(linked_requests),
                _request_evidence_summary(linked_requests),
                source_reference,
            ])
    return _csv_string(headers, rows)


def _working_paper_client_requests_csv(center):
    request_map = _working_paper_request_map(center)
    headers = [
        "Company",
        "Source Reference",
        "Title",
        "Document Type",
        "Status",
        "Due Date",
        "Uploaded At",
        "Closed At",
        "Portal User",
        "Recipient Email",
        "Related Task",
        "Evidence File",
        "Response Note",
    ]
    rows = []
    for source_reference in sorted(request_map):
        for doc_request in request_map[source_reference]:
            rows.append([
                doc_request.company.name,
                doc_request.source_reference,
                doc_request.title,
                doc_request.get_document_type_display(),
                doc_request.get_status_display(),
                doc_request.due_date.isoformat() if doc_request.due_date else "",
                doc_request.uploaded_at.isoformat() if doc_request.uploaded_at else "",
                doc_request.closed_at.isoformat() if doc_request.closed_at else "",
                doc_request.portal_user.name if doc_request.portal_user else "",
                doc_request.recipient_email or "",
                doc_request.related_task.get_status_display() if doc_request.related_task else "",
                doc_request.uploaded_submission.file.name if doc_request.uploaded_submission_id and doc_request.uploaded_submission.file else "",
                doc_request.response_note,
            ])
    return _csv_string(headers, rows)


def _working_paper_readme(center):
    generated_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S %Z")
    return (
        "Bank Reconciliation Working Paper Pack\n"
        f"Generated at: {generated_at}\n"
        f"As of: {center['as_of_date'].isoformat()}\n"
        f"Focus: {center['focus']}\n\n"
        "Files:\n"
        "- bank_reco_summary.csv: portfolio and statement-level reconciliation metrics.\n"
        "- bank_reco_exceptions.csv: unreconciled rows, match reasons, and linked evidence status.\n"
        "- bank_reco_client_requests.csv: client evidence requests linked to bank exceptions.\n"
    )


def _request_status_summary(document_requests):
    if not document_requests:
        return ""
    return "; ".join(doc_request.get_status_display() for doc_request in document_requests)


def _request_evidence_summary(document_requests):
    evidence = []
    for doc_request in document_requests:
        if doc_request.uploaded_submission_id and doc_request.uploaded_submission.file:
            evidence.append(doc_request.uploaded_submission.file.name)
    return "; ".join(evidence)


def _filter_rows(rows, focus):
    if focus == "all":
        return list(rows)
    return [row for row in rows if _matches_focus(row, focus)]


def _matches_focus(row, focus):
    if focus == "attention":
        return row["needs_attention"]
    if focus == "critical":
        return row["status"] in {"critical", "no_statement"}
    if focus == "auto_ready":
        return row["status"] == "auto_ready"
    if focus == "duplicates":
        return row["duplicate_count"] > 0
    if focus == "client_waiting":
        return row.get("client_request_open_count", 0) > 0
    if focus == "client_uploaded":
        return row.get("client_request_uploaded_count", 0) > 0
    if focus == "no_statement":
        return row["status"] == "no_statement"
    if focus == "clean":
        return row["status"] == "clean"
    return True


def _totals(company_rows, statement_rows):
    all_company_count = len(company_rows)
    attention_companies = sum(1 for row in company_rows if row["needs_attention"])
    clean_companies = sum(1 for row in company_rows if row["status"] == "clean")
    statement_count = sum(row["statement_count"] for row in company_rows)
    row_count = sum(row["row_count"] for row in company_rows)
    pending_count = sum(row["pending_count"] for row in company_rows)
    duplicate_count = sum(row["duplicate_count"] for row in company_rows)
    high_confidence_count = sum(row["high_confidence_count"] for row in company_rows)
    auto_postable_count = sum(row["auto_postable_count"] for row in company_rows)
    pending_amount = sum((row["pending_amount"] for row in company_rows), ZERO)
    no_statement_count = sum(1 for row in company_rows if row["status"] == "no_statement")
    missing_bank_ledger = sum(row["missing_bank_ledger"] for row in company_rows)
    client_request_count = sum(row.get("client_request_count", 0) for row in statement_rows)
    client_request_open_count = sum(row.get("client_request_open_count", 0) for row in statement_rows)
    client_request_uploaded_count = sum(row.get("client_request_uploaded_count", 0) for row in statement_rows)
    blocker_units = pending_count + (duplicate_count * 2) + (no_statement_count * 3) + (missing_bank_ledger * 2)
    denominator = max(row_count + (all_company_count * 2), 1)
    score = max(0, min(100, round(100 - ((blocker_units / denominator) * 100))))
    return {
        "company_count": all_company_count,
        "attention_companies": attention_companies,
        "clean_companies": clean_companies,
        "statement_count": statement_count,
        "statement_queue_count": len(statement_rows),
        "row_count": row_count,
        "pending_count": pending_count,
        "pending_amount": pending_amount,
        "duplicate_count": duplicate_count,
        "high_confidence_count": high_confidence_count,
        "auto_postable_count": auto_postable_count,
        "no_statement_count": no_statement_count,
        "missing_bank_ledger": missing_bank_ledger,
        "client_request_count": client_request_count,
        "client_request_open_count": client_request_open_count,
        "client_request_uploaded_count": client_request_uploaded_count,
        "autopilot_score": score,
    }


def _sort_key(row):
    return (row["status_rank"], -row["pending_amount"], row["company"].name.lower())


def _statement_sort_key(row):
    statement_date = row["latest_statement_date"] or date_cls.min
    return (row["status_rank"], -row["pending_amount"], row["company"].name.lower(), -statement_date.toordinal())


def _task_title(row):
    if row["status"] == "no_statement":
        return "Upload bank statement for reconciliation"
    if row["missing_bank_ledger"]:
        return "Map bank ledger before reconciliation"
    if row["duplicate_count"]:
        return "Review duplicate bank statement rows"
    if row["stale_count"]:
        return "Clear stale bank reconciliation rows"
    return "Complete bank reconciliation"


def _task_description(row):
    statement_date = row["latest_statement_date"].isoformat() if row["latest_statement_date"] else "No statement uploaded"
    return (
        f"Company: {row['company'].name}\n"
        f"Statement date: {statement_date}\n"
        f"Bank account: {row['bank_account_name'] or '-'}\n"
        f"Status: {row['status_label']}\n"
        f"Pending rows: {row['pending_count']}\n"
        f"Pending amount: Rs.{row['pending_amount']:.2f}\n"
        f"Duplicate rows: {row['duplicate_count']}\n"
        f"High-confidence rows: {row['high_confidence_count']}\n"
        f"Suggested ledger rows: {row['suggested_count']}\n"
        f"Stale pending rows: {row['stale_count']}"
    )


def _auto_postable_bank_rows(statement):
    return list(
        statement.rows.filter(
            is_reconciled=False,
            suggested_ledger__isnull=False,
            match_confidence__gte=70,
            potential_duplicate=False,
        )
        .select_related("statement", "statement__company", "statement__account_ledger", "suggested_ledger")
        .order_by("date", "row_number", "id")
    )


def _lock_bank_row_for_auto_post(row_id):
    from .models import BankStatementRow

    return (
        BankStatementRow.objects.select_for_update()
        .select_related("statement", "statement__company", "statement__account_ledger", "suggested_ledger")
        .get(pk=row_id)
    )


def _create_voucher_from_auto_ready_bank_row(row, user):
    from vouchers.models import Voucher, VoucherItem

    if row.is_reconciled:
        raise ValueError("Bank row is already reconciled.")
    if row.potential_duplicate:
        raise ValueError("Potential duplicate bank row requires manual review.")
    if not row.suggested_ledger_id or row.match_confidence < 70:
        raise ValueError("Bank row is missing a high-confidence ledger suggestion.")
    if not row.statement.account_ledger_id:
        raise ValueError("Statement has no bank account ledger linked.")
    if row.suggested_ledger.company_id != row.statement.company_id:
        raise ValueError("Suggested ledger belongs to another company.")

    amount = abs(row.amount)
    if amount <= 0:
        raise ValueError("Bank row has no debit or credit amount.")

    source_reference = _bank_row_source_reference(row)
    existing_voucher = Voucher.objects.filter(
        company=row.statement.company,
        source_system="bank_reco_autopilot",
        source_reference=source_reference,
    ).first()
    if existing_voucher:
        row.is_reconciled = True
        row.matched_voucher = existing_voucher
        row.match_confidence = 100
        row.match_reason = "Linked to existing Bank Autopilot voucher"
        row.save(update_fields=["is_reconciled", "matched_voucher", "match_confidence", "match_reason"])
        return existing_voucher

    is_withdrawal = row.debit > 0
    voucher = Voucher.objects.create(
        company=row.statement.company,
        date=row.date,
        voucher_type="Payment" if is_withdrawal else "Receipt",
        narration=f"Auto-posted from bank statement: {row.description}",
        source_system="bank_reco_autopilot",
        source_reference=source_reference,
    )
    VoucherItem.objects.create(
        voucher=voucher,
        ledger=row.suggested_ledger,
        entry_type="DR" if is_withdrawal else "CR",
        amount=amount,
        narration=row.description,
    )
    VoucherItem.objects.create(
        voucher=voucher,
        ledger=row.statement.account_ledger,
        entry_type="CR" if is_withdrawal else "DR",
        amount=amount,
        narration=row.description,
    )
    voucher.validate_balance()
    voucher.approve(user)

    row.is_reconciled = True
    row.matched_voucher = voucher
    row.match_confidence = 100
    row.match_reason = "Voucher auto-posted from Bank Autopilot"
    row.save(update_fields=["is_reconciled", "matched_voucher", "match_confidence", "match_reason"])
    _audit_auto_posted_voucher(row, voucher, user)
    return voucher


def _audit_auto_posted_voucher(row, voucher, user):
    AuditLog.objects.create(
        company=row.statement.company,
        user=user,
        action=AuditLog.ACTION_CREATE,
        model_name="Voucher",
        record_id=voucher.pk,
        object_repr=str(voucher)[:200],
        old_data={},
        new_data={
            "source": "bank_reco_autopilot_auto_post",
            "source_reference": _bank_row_source_reference(row),
            "bank_row_id": row.pk,
            "suggested_ledger_id": row.suggested_ledger_id,
            "amount": f"{abs(row.amount):.2f}",
        },
    )


def _close_completed_bank_statement_task(statement, user):
    if statement.rows.filter(is_reconciled=False).exists():
        return 0

    now = timezone.now()
    tasks = PracticeTask.objects.filter(
        company=statement.company,
        task_type=PracticeTask.TYPE_BANK,
        reference=f"{TASK_REFERENCE_PREFIX}:{statement.company_id}:STMT:{statement.pk}",
    ).exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])

    closed = 0
    for task in tasks:
        old_status = task.status
        task.status = PracticeTask.STATUS_DONE
        task.completed_by = user
        task.completed_at = now
        task.description = (
            f"{task.description}\n\nBank statement fully reconciled by Bank Autopilot."
        ).strip()
        task.save(update_fields=["status", "completed_by", "completed_at", "description", "updated_at"])
        closed += 1
        AuditLog.objects.create(
            company=statement.company,
            user=user,
            action=AuditLog.ACTION_UPDATE,
            model_name="PracticeTask",
            record_id=task.pk,
            object_repr=task.title[:200],
            old_data={"status": old_status},
            new_data={
                "status": task.status,
                "source": "bank_reco_autopilot_auto_close_task",
                "statement_id": statement.pk,
            },
        )
    return closed


def _format_auto_post_error(row, exc):
    if isinstance(exc, ValidationError):
        message = "; ".join(exc.messages)
    else:
        message = str(exc)
    return f"{row.statement.company.name} row {row.row_number or row.pk}: {message}"


def _client_request_candidate_bank_rows(statement, as_of_date):
    rows = (
        statement.rows.filter(is_reconciled=False)
        .select_related("statement", "statement__company", "suggested_ledger")
        .order_by("date", "row_number", "id")
    )
    candidates = []
    for row in rows:
        stale = (as_of_date - row.date).days > STALE_PENDING_DAYS
        internally_actionable = bool(row.suggested_ledger_id and row.match_confidence >= 70 and not row.potential_duplicate)
        if internally_actionable and not stale:
            continue
        candidates.append(row)
    return candidates


def _create_missing_statement_request(work_row, user):
    from portal.models import ClientDocumentRequest

    company = work_row["company"]
    portal_user = _default_portal_user(company)
    contact = _contact_from_portal_user(portal_user)
    return ClientDocumentRequest.objects.get_or_create(
        company=company,
        source_reference=_missing_statement_source_reference(company),
        defaults={
            "portal_user": portal_user,
            "recipient_email": contact["email"],
            "recipient_whatsapp_number": contact["whatsapp"],
            "title": "Upload latest bank statement",
            "document_type": ClientDocumentRequest.TYPE_BANK,
            "status": ClientDocumentRequest.STATUS_OPEN,
            "due_date": timezone.localdate() + timedelta(days=2),
            "notes": (
                "Please upload the latest bank statement so the CA team can complete bank reconciliation.\n"
                f"Bank reconciliation status: {work_row['status_label']}."
            ),
            "requested_by": user,
        },
    )


def _bank_row_request_defaults(bank_row, user):
    from portal.models import ClientDocumentRequest

    portal_user = _portal_user_for_bank_row(bank_row)
    contact = _contact_from_portal_user(portal_user, getattr(bank_row, "suggested_ledger", None))
    return {
        "portal_user": portal_user,
        "recipient_email": contact["email"],
        "recipient_whatsapp_number": contact["whatsapp"],
        "title": _bank_row_request_title(bank_row),
        "document_type": ClientDocumentRequest.TYPE_BANK,
        "status": ClientDocumentRequest.STATUS_OPEN,
        "due_date": timezone.localdate() + timedelta(days=2),
        "notes": _bank_row_request_notes(bank_row),
        "requested_by": user,
    }


def _bank_row_request_title(bank_row):
    amount = abs(bank_row.amount)
    direction = "receipt" if bank_row.credit > 0 else "payment"
    return f"Clarify bank {direction} Rs.{amount:.2f} on {bank_row.date:%d %b %Y}"


def _bank_row_request_notes(bank_row):
    amount = abs(bank_row.amount)
    direction = "Credit" if bank_row.credit > 0 else "Debit"
    return (
        "Please share invoice, receipt, bill, confirmation, or explanation for this bank entry.\n"
        f"Date: {bank_row.date:%d %b %Y}\n"
        f"Bank account: {bank_row.statement.account_ledger.name if bank_row.statement.account_ledger_id else '-'}\n"
        f"Type: {direction}\n"
        f"Amount: Rs.{amount:.2f}\n"
        f"Narration: {bank_row.description}\n"
        f"System reason: {bank_row.match_reason or 'No clear voucher or ledger match found.'}"
    )


def _bank_row_source_reference(bank_row):
    return f"BANKROW:{bank_row.statement.company_id}:ROW:{bank_row.pk}"


def _bank_row_id_from_source_reference(source_reference):
    parts = (source_reference or "").split(":")
    if len(parts) == 4 and parts[0] == "BANKROW" and parts[2] == "ROW" and parts[3].isdigit():
        return int(parts[3])
    return None


def _mark_bank_row_evidence_reviewed(source_reference):
    bank_row_id = _bank_row_id_from_source_reference(source_reference)
    if not bank_row_id:
        return
    from .models import BankStatementRow

    bank_row = BankStatementRow.objects.filter(pk=bank_row_id).first()
    if not bank_row or bank_row.is_reconciled:
        return
    bank_row.match_confidence = max(bank_row.match_confidence or 0, 50)
    bank_row.match_reason = "Client evidence reviewed; internal posting pending"
    bank_row.save(update_fields=["match_confidence", "match_reason"])


def _missing_statement_source_reference(company):
    return f"BANKROW:{company.pk}:NO_STATEMENT"


def _portal_user_for_bank_row(bank_row):
    ledger = getattr(bank_row, "suggested_ledger", None)
    if ledger:
        portal_user = ledger.portal_users.filter(is_active=True).order_by("name", "email").first()
        if portal_user:
            return portal_user
    return _default_portal_user(bank_row.statement.company)


def _default_portal_user(company):
    from portal.models import PortalUser

    return (
        PortalUser.objects.filter(linked_ledger__company=company, is_active=True)
        .select_related("linked_ledger")
        .order_by("name", "email")
        .first()
    )


def _contact_from_portal_user(portal_user, fallback_ledger=None):
    ledger = portal_user.linked_ledger if portal_user else fallback_ledger
    return {
        "email": (portal_user.email if portal_user else "") or (ledger.email if ledger else "") or "",
        "whatsapp": (ledger.whatsapp_number if ledger else "") or "",
    }


def _ensure_client_request_task(doc_request, user):
    if doc_request.related_task_id:
        return 0, 1
    today = timezone.localdate()
    task, was_created = PracticeTask.objects.get_or_create(
        company=doc_request.company,
        reference=f"DOCREQ:{doc_request.pk}",
        defaults={
            "title": f"Client request: {doc_request.title}",
            "task_type": PracticeTask.TYPE_DOCUMENT,
            "priority": PracticeTask.PRIORITY_HIGH if not doc_request.due_date or doc_request.due_date >= today else PracticeTask.PRIORITY_CRITICAL,
            "status": PracticeTask.STATUS_OPEN,
            "due_date": doc_request.due_date,
            "created_by": user,
            "description": (
                "Collect/review client document request.\n"
                f"Document type: {doc_request.get_document_type_display()}\n"
                f"Source reference: {doc_request.source_reference or '-'}"
            ),
        },
    )
    doc_request.related_task = task
    doc_request.save(update_fields=["related_task", "updated_at"])
    return (1, 0) if was_created else (0, 1)


def _audit_client_request(doc_request, user, source):
    AuditLog.objects.create(
        company=doc_request.company,
        user=user,
        action=AuditLog.ACTION_CREATE,
        model_name="ClientDocumentRequest",
        record_id=doc_request.pk,
        object_repr=doc_request.title[:200],
        old_data={},
        new_data={
            "source": f"bank_reco_autopilot_{source}",
            "source_reference": doc_request.source_reference,
            "document_type": doc_request.document_type,
            "due_date": doc_request.due_date.isoformat() if doc_request.due_date else "",
        },
    )


def _company_action_url(company, latest_statement):
    if latest_statement:
        return _switch_url(company, reverse("core:bank_statement_detail", args=[latest_statement.pk]))
    return _switch_url(company, reverse("core:bank_statement_upload"))


def _switch_url(company, path):
    return f"{reverse('core:switch_company', args=[company.pk])}?{urlencode({'next': path})}"


def _client_request_url(company):
    path = f"{reverse('portal:client_requests')}?{urlencode({'company': company.pk, 'type': 'bank', 'status': 'active'})}"
    return _switch_url(company, path)
