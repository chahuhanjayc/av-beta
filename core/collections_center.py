"""Cross-client receivables and collections command center."""

import csv
from decimal import Decimal
from email.utils import formataddr
from urllib.parse import quote, urlencode

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from vouchers.models import Voucher
from vouchers.utils import generate_upi_qr

from .models import AuditLog, PracticeTask
from .phone import normalize_phone_number


ZERO = Decimal("0.00")
TASK_REFERENCE_PREFIX = "COLLECT"


def build_collections_center(companies, *, as_of_date=None, risk_filter="active"):
    as_of_date = as_of_date or timezone.localdate()
    rows = _invoice_rows(companies, as_of_date)
    party_rows = _party_rows(rows)

    if risk_filter and risk_filter != "active":
        rows = [row for row in rows if _matches_risk(row, risk_filter)]
        party_rows = [row for row in party_rows if _matches_party_risk(row, risk_filter)]

    totals = _totals(rows, party_rows)
    return {
        "rows": rows,
        "party_rows": party_rows,
        "totals": totals,
        "as_of_date": as_of_date,
        "risk_filter": risk_filter,
    }


def collections_csv_response(center):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="collections_command_center.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "Client",
        "Invoice",
        "Invoice Date",
        "Due Date",
        "Status",
        "Days Overdue",
        "Outstanding",
        "Email",
        "WhatsApp",
        "Task Exists",
        "Credit Limit",
        "Credit Exposure",
    ])
    for row in center["rows"]:
        writer.writerow([
            row["company"].name,
            row["party_name"],
            row["voucher"].number or row["voucher"].pk,
            row["voucher"].date.isoformat(),
            row["voucher"].due_date.isoformat() if row["voucher"].due_date else "",
            row["status_label"],
            row["days_overdue"] if row["days_overdue"] is not None else "",
            f"{row['outstanding']:.2f}",
            row["email"],
            row["whatsapp_number"],
            "Yes" if row["task_exists"] else "No",
            f"{row['credit_limit']:.2f}" if row["credit_limit"] is not None else "",
            f"{row['credit_exposure']:.2f}" if row["credit_exposure"] is not None else "",
        ])
    return response


def create_collection_tasks(rows, user, manageable_company_ids, selected_ids=None):
    selected_ids = {int(value) for value in selected_ids or [] if str(value).isdigit()}
    created = 0
    existing = 0
    as_of_date = timezone.localdate()

    for row in rows:
        if row["company"].pk not in manageable_company_ids:
            continue
        if selected_ids and row["voucher"].pk not in selected_ids:
            continue
        if not selected_ids and not row["is_overdue"]:
            continue
        task, was_created = PracticeTask.objects.get_or_create(
            company=row["company"],
            reference=row["task_reference"],
            defaults={
                "title": f"Collect overdue invoice {row['voucher'].number or row['voucher'].pk}",
                "task_type": PracticeTask.TYPE_OTHER,
                "priority": row["task_priority"],
                "status": PracticeTask.STATUS_OPEN,
                "due_date": as_of_date,
                "created_by": user,
                "description": (
                    f"Invoice {row['voucher'].number or row['voucher'].pk} is {row['days_overdue'] or 0} day(s) overdue.\n"
                    f"Client: {row['party_name'] or 'Not identified'}\n"
                    f"Outstanding: Rs.{row['outstanding']:.2f}\n"
                    f"Email: {row['email'] or '-'}\n"
                    f"WhatsApp: {row['whatsapp_number'] or '-'}"
                ),
            },
        )
        if was_created:
            created += 1
        else:
            existing += 1
    return {"created": created, "existing": existing}


def send_collection_emails(request, rows, user, manageable_company_ids, selected_ids):
    selected_ids = {int(value) for value in selected_ids or [] if str(value).isdigit()}
    sent = 0
    skipped = 0
    failed = 0
    if not selected_ids:
        return {"sent": 0, "skipped": 0, "failed": 0, "empty": True}

    for row in rows:
        if row["voucher"].pk not in selected_ids:
            continue
        if row["company"].pk not in manageable_company_ids or not row["email"]:
            skipped += 1
            continue
        try:
            validate_email(row["email"])
            _send_collection_email(request, row)
        except Exception:
            failed += 1
            continue
        AuditLog.objects.create(
            company=row["company"],
            user=user,
            action=AuditLog.ACTION_UPDATE,
            model_name="Voucher",
            record_id=row["voucher"].pk,
            object_repr=str(row["voucher"]),
            old_data={},
            new_data={
                "collection_reminder_sent_to": row["email"],
                "client_ledger": row["party_name"],
                "outstanding_amount": str(row["outstanding"]),
                "source": "collections_command_center",
            },
        )
        sent += 1
    return {"sent": sent, "skipped": skipped, "failed": failed, "empty": False}


def risk_filter_choices():
    return [
        ("active", "All Active"),
        ("critical", "Critical"),
        ("overdue", "Overdue"),
        ("due_soon", "Due Soon"),
        ("no_contact", "No Contact"),
    ]


def _invoice_rows(companies, as_of_date):
    vouchers = (
        Voucher.objects.filter(
            company__in=companies,
            voucher_type="Sales",
            status="APPROVED",
            outstanding_amount__gt=0,
        )
        .select_related("company")
        .prefetch_related("items__ledger__account_group")
        .order_by("due_date", "date", "number", "id")
    )

    rows = []
    for voucher in vouchers:
        party = _sales_party_ledger(voucher)
        outstanding = voucher.outstanding_amount or ZERO
        days_overdue = (as_of_date - voucher.due_date).days if voucher.due_date else None
        days_to_due = (voucher.due_date - as_of_date).days if voucher.due_date else None
        status, status_label = _collection_status(days_overdue, days_to_due)
        email = (party.email or "").strip() if party else ""
        whatsapp_number = _normalised_whatsapp(party.whatsapp_number if party else "")
        credit_limit = party.credit_limit if party and party.credit_limit is not None else None
        credit_exposure = None
        if credit_limit is not None:
            credit_exposure = max(ZERO, _ledger_outstanding(voucher.company, party) - credit_limit)
        task_reference = f"{TASK_REFERENCE_PREFIX}:{voucher.company_id}:{voucher.pk}"
        rows.append({
            "voucher": voucher,
            "company": voucher.company,
            "party": party,
            "party_name": party.name if party else "Unidentified client",
            "email": email,
            "whatsapp_number": whatsapp_number,
            "invoice_total": voucher.total_amount(),
            "outstanding": outstanding,
            "days_overdue": days_overdue,
            "days_to_due": days_to_due,
            "is_overdue": bool(days_overdue is not None and days_overdue > 0),
            "status": status,
            "status_label": status_label,
            "status_class": _status_class(status),
            "task_priority": _task_priority(status, days_overdue),
            "credit_limit": credit_limit,
            "credit_exposure": credit_exposure,
            "contact_ready": bool(email or whatsapp_number),
            "email_ready": bool(email),
            "whatsapp_ready": bool(whatsapp_number),
            "task_reference": task_reference,
            "task_exists": False,
            "voucher_url": _switch_url(voucher.company, reverse("vouchers:detail", args=[voucher.pk])),
            "outstanding_url": _switch_url(
                voucher.company,
                f"{reverse('vouchers:outstanding')}?{urlencode({'type': 'Sales', 'status': 'outstanding'})}",
            ),
        })

    references = [row["task_reference"] for row in rows]
    existing = set(
        PracticeTask.objects.filter(reference__in=references)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
        .values_list("reference", flat=True)
    )
    for row in rows:
        row["task_exists"] = row["task_reference"] in existing
        row["whatsapp_url"] = _whatsapp_url(row)
    return rows


def _party_rows(rows):
    grouped = {}
    for row in rows:
        key = (row["company"].pk, row["party"].pk if row["party"] else f"voucher:{row['voucher'].pk}")
        if key not in grouped:
            grouped[key] = {
                "company": row["company"],
                "party": row["party"],
                "party_name": row["party_name"],
                "email": row["email"],
                "whatsapp_number": row["whatsapp_number"],
                "invoice_count": 0,
                "outstanding": ZERO,
                "overdue_amount": ZERO,
                "critical_amount": ZERO,
                "max_overdue": 0,
                "contact_ready": False,
                "credit_limit": row["credit_limit"],
                "credit_exposure": row["credit_exposure"],
                "status": "current",
                "status_label": "Current",
                "status_class": "success",
            }
        item = grouped[key]
        item["invoice_count"] += 1
        item["outstanding"] += row["outstanding"]
        item["contact_ready"] = item["contact_ready"] or row["contact_ready"]
        if row["is_overdue"]:
            item["overdue_amount"] += row["outstanding"]
            item["max_overdue"] = max(item["max_overdue"], row["days_overdue"] or 0)
        if row["status"] == "critical":
            item["critical_amount"] += row["outstanding"]

    party_rows = list(grouped.values())
    for row in party_rows:
        if row["credit_exposure"] and row["credit_exposure"] > ZERO:
            row["status"] = "credit_exceeded"
            row["status_label"] = "Credit exceeded"
            row["status_class"] = "danger"
        elif row["max_overdue"] > 90:
            row["status"] = "critical"
            row["status_label"] = "Critical"
            row["status_class"] = "danger"
        elif row["max_overdue"] > 0:
            row["status"] = "overdue"
            row["status_label"] = "Overdue"
            row["status_class"] = "warning"
        elif not row["contact_ready"]:
            row["status"] = "no_contact"
            row["status_label"] = "No contact"
            row["status_class"] = "danger"
    return sorted(party_rows, key=lambda item: (-item["overdue_amount"], -item["outstanding"], item["party_name"]))


def _totals(rows, party_rows):
    total_outstanding = sum((row["outstanding"] for row in rows), ZERO)
    overdue_amount = sum((row["outstanding"] for row in rows if row["is_overdue"]), ZERO)
    critical_amount = sum((row["outstanding"] for row in rows if row["status"] == "critical"), ZERO)
    due_soon_amount = sum((row["outstanding"] for row in rows if row["status"] == "due_soon"), ZERO)
    no_contact = sum(1 for row in rows if not row["contact_ready"])
    score = 100
    if total_outstanding:
        overdue_ratio = overdue_amount / total_outstanding
        critical_ratio = critical_amount / total_outstanding
        score = 100 - int(overdue_ratio * 35) - int(critical_ratio * 35) - min(20, no_contact * 4)
    return {
        "invoice_count": len(rows),
        "party_count": len(party_rows),
        "total_outstanding": total_outstanding,
        "overdue_amount": overdue_amount,
        "critical_amount": critical_amount,
        "due_soon_amount": due_soon_amount,
        "overdue_count": sum(1 for row in rows if row["is_overdue"]),
        "critical_count": sum(1 for row in rows if row["status"] == "critical"),
        "due_soon_count": sum(1 for row in rows if row["status"] == "due_soon"),
        "no_contact": no_contact,
        "email_ready": sum(1 for row in rows if row["email_ready"]),
        "whatsapp_ready": sum(1 for row in rows if row["whatsapp_ready"]),
        "task_exists": sum(1 for row in rows if row["task_exists"]),
        "collection_score": max(0, min(100, score)),
    }


def _matches_risk(row, risk_filter):
    if risk_filter == "critical":
        return row["status"] == "critical"
    if risk_filter == "overdue":
        return row["is_overdue"]
    if risk_filter == "due_soon":
        return row["status"] == "due_soon"
    if risk_filter == "no_contact":
        return not row["contact_ready"]
    return True


def _matches_party_risk(row, risk_filter):
    if risk_filter == "critical":
        return row["status"] in {"critical", "credit_exceeded"}
    if risk_filter == "overdue":
        return row["max_overdue"] > 0
    if risk_filter == "due_soon":
        return row["status"] == "due_soon"
    if risk_filter == "no_contact":
        return not row["contact_ready"]
    return True


def _sales_party_ledger(voucher):
    for item in voucher.items.all():
        if item.entry_type == "DR" and item.ledger.account_group.nature == "Asset":
            return item.ledger
    for item in voucher.items.all():
        if item.entry_type == "DR":
            return item.ledger
    return None


def _ledger_outstanding(company, ledger):
    if not ledger:
        return ZERO
    vouchers = (
        Voucher.objects.filter(
            company=company,
            voucher_type="Sales",
            status="APPROVED",
            outstanding_amount__gt=0,
            items__ledger=ledger,
            items__entry_type="DR",
        )
        .distinct()
    )
    return sum((voucher.outstanding_amount or ZERO for voucher in vouchers), ZERO)


def _collection_status(days_overdue, days_to_due):
    if days_overdue is None:
        return "no_due_date", "No due date"
    if days_overdue > 90:
        return "critical", "Critical"
    if days_overdue > 0:
        return "overdue", "Overdue"
    if days_to_due is not None and days_to_due <= 7:
        return "due_soon", "Due soon"
    return "current", "Current"


def _status_class(status):
    if status in {"critical", "no_contact"}:
        return "danger"
    if status in {"overdue", "due_soon", "no_due_date"}:
        return "warning"
    return "success"


def _task_priority(status, days_overdue):
    if status == "critical" or (days_overdue or 0) > 60:
        return PracticeTask.PRIORITY_CRITICAL
    if status in {"overdue", "due_soon", "no_contact"}:
        return PracticeTask.PRIORITY_HIGH
    return PracticeTask.PRIORITY_NORMAL


def _normalised_whatsapp(value):
    if not value:
        return ""
    try:
        return normalize_phone_number(value)
    except ValueError:
        return ""


def _whatsapp_url(row):
    message = (
        f"Dear {row['party_name']}, payment reminder for invoice {row['voucher'].number or row['voucher'].pk} "
        f"from {row['company'].name}. Outstanding Rs.{row['outstanding']:.2f}. "
        f"Due date: {row['voucher'].due_date.strftime('%d %b %Y') if row['voucher'].due_date else 'as per terms'}. "
        "Please share payment status."
    )
    encoded = quote(message)
    if row["whatsapp_number"]:
        return f"https://wa.me/{row['whatsapp_number'].lstrip('+')}?text={encoded}"
    return f"https://wa.me/?text={encoded}"


def _switch_url(company, next_url):
    return f"{reverse('core:switch_company', args=[company.pk])}?{urlencode({'next': next_url})}"


def _invoice_context(company, voucher, client_name):
    return {
        "voucher_number": voucher.number,
        "company_name": company.name,
        "client_name": client_name or "Customer",
        "amount": f"Rs. {voucher.total_debit():.2f}",
        "outstanding": f"Rs. {(voucher.outstanding_amount or ZERO):.2f}",
        "due_date": voucher.due_date.strftime("%d %b %Y") if voucher.due_date else "as per agreed payment terms",
        "aging_line": _aging_line(voucher),
    }


def _aging_line(voucher):
    if not voucher.due_date:
        return "The invoice is currently unpaid."
    days = (timezone.localdate() - voucher.due_date).days
    if days > 0:
        return f"The invoice is {days} day(s) overdue."
    if days == 0:
        return "The invoice is due today."
    return f"The invoice is due in {abs(days)} day(s)."


def _format_template(template, context):
    try:
        return (template or "").format(**context)
    except (KeyError, ValueError):
        return template or ""


def _from_email(company):
    if company.invoice_email_from_address:
        return formataddr((company.invoice_email_from_name or company.name, company.invoice_email_from_address))
    return settings.DEFAULT_FROM_EMAIL


def _reply_to(company):
    if company.invoice_email_reply_to:
        return [company.invoice_email_reply_to]
    if company.invoice_email_from_address:
        return [company.invoice_email_from_address]
    return None


def _render_invoice_pdf_bytes(request, voucher, company):
    import weasyprint

    html = render_to_string("vouchers/invoice_pdf.html", {
        "voucher": voucher,
        "company": company,
        "qr_code": generate_upi_qr(voucher),
        "today": timezone.localdate(),
    }, request=request)
    return weasyprint.HTML(string=html, base_url=request.build_absolute_uri("/")).write_pdf()


def _send_collection_email(request, row):
    company = row["company"]
    voucher = row["voucher"]
    context = _invoice_context(company, voucher, row["party_name"])
    subject = _format_template(
        company.payment_reminder_email_subject or "Payment reminder: Invoice {voucher_number} from {company_name}",
        context,
    )
    body = _format_template(
        company.payment_reminder_email_body or (
            "Dear {client_name},\n\n"
            "This is a payment reminder for invoice {voucher_number} from {company_name}.\n"
            "Outstanding amount: {outstanding}\n"
            "Due date: {due_date}\n"
            "{aging_line}\n\n"
            "Please ignore this message if payment has already been made.\n\n"
            "Regards,\n{company_name}"
        ),
        context,
    )
    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=_from_email(company),
        to=[row["email"]],
        reply_to=_reply_to(company),
    )
    email.attach(
        f"Invoice_{voucher.number or voucher.pk}.pdf",
        _render_invoice_pdf_bytes(request, voucher, company),
        "application/pdf",
    )
    email.send(fail_silently=False)
