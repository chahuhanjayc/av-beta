import csv
from datetime import date as _date
from email.utils import formataddr
from urllib.parse import quote, urlencode

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import transaction, models
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import PortalGSTR2BEntry
from .parser import GSTR2BParser
from core.decorators import write_required
from core.models import AuditLog, PracticeTask
from core.phone import normalize_phone_number
from core.upload_validation import JSON_EXTENSIONS, validate_uploaded_file
from vouchers.models import Voucher, VoucherItem
from ledger.models import Ledger, AccountGroup
from decimal import Decimal


def _month_bounds(value):
    if value:
        try:
            year, month = [int(part) for part in value.split("-", 1)]
            start = _date(year, month, 1)
        except (TypeError, ValueError):
            start = timezone.localdate().replace(day=1)
    else:
        start = timezone.localdate().replace(day=1)
    if start.month == 12:
        end = _date(start.year, 12, 31)
    else:
        end = _date(start.year, start.month + 1, 1) - timezone.timedelta(days=1)
    return start, end


def _period_value(period_start):
    return period_start.strftime("%Y-%m")


def _shift_month(period_start, offset):
    month_index = period_start.year * 12 + period_start.month - 1 + offset
    year = month_index // 12
    month = month_index % 12 + 1
    return _date(year, month, 1)


def _range_bounds(start_value=None, end_value=None):
    default_end = timezone.localdate().replace(day=1)
    default_start = _shift_month(default_end, -5)
    period_start, _ = _month_bounds(start_value or _period_value(default_start))
    _, period_end = _month_bounds(end_value or _period_value(default_end))
    if period_end < period_start:
        _, period_end = _month_bounds(_period_value(period_start))
    return period_start, period_end


def _results_url(period_start, filters=None):
    params = {"period": _period_value(period_start)}
    if filters:
        params.update({key: value for key, value in filters.items() if value})
    return f"{reverse('gstr2b:results')}?{urlencode(params)}"


def _parse_filters(request):
    return {
        "status": request.GET.get("status", "all").strip() or "all",
        "action": request.GET.get("action", "all").strip() or "all",
        "q": request.GET.get("q", "").strip(),
    }


def _apply_portal_filters(qs, filters):
    if filters["status"] != "all":
        qs = qs.filter(match_status=filters["status"])
    if filters["action"] != "all":
        qs = qs.filter(action_status=filters["action"])
    if filters["q"]:
        qs = qs.filter(
            models.Q(invoice_number__icontains=filters["q"])
            | models.Q(supplier_name__icontains=filters["q"])
            | models.Q(gstin__icontains=filters["q"])
        )
    return qs


def _purchase_party_ledger(voucher):
    lines = voucher.items.select_related("ledger", "ledger__account_group").all()
    for item in lines:
        if item.entry_type == "CR" and item.ledger.account_group.nature == "Liability":
            return item.ledger
    for item in lines:
        if item.entry_type == "CR":
            return item.ledger
    return None


def _action_badge_class(action):
    return {
        "accepted": "bg-success",
        "pending": "bg-warning text-dark",
        "rejected": "bg-danger",
        "new": "bg-secondary",
    }.get(action, "bg-secondary")


def _sync_entry_action(entry, user, action, note=""):
    old_data = {
        "action_status": entry.action_status,
        "action_note": entry.action_note,
        "matched_voucher_id": entry.matched_voucher_id,
    }
    entry.action_status = action
    entry.action_note = note[:300]
    update_fields = ["action_status", "action_note", "updated_at"]
    entry.save(update_fields=update_fields)

    if entry.matched_voucher_id:
        voucher = entry.matched_voucher
        old_itc_claimed = voucher.is_itc_claimed
        new_itc_claimed = action == "accepted"
        if old_itc_claimed != new_itc_claimed:
            voucher.is_itc_claimed = new_itc_claimed
            voucher.save(update_fields=["is_itc_claimed"])
            AuditLog.objects.create(
                company=entry.company,
                user=user,
                action=AuditLog.ACTION_UPDATE,
                model_name="Voucher",
                record_id=voucher.pk,
                object_repr=voucher.number or f"Voucher #{voucher.pk}",
                old_data={"is_itc_claimed": old_itc_claimed},
                new_data={
                    "is_itc_claimed": new_itc_claimed,
                    "source": "ims_action",
                    "portal_gstr2b_entry_id": entry.pk,
                    "ims_action": action,
                },
            )

    AuditLog.objects.create(
        company=entry.company,
        user=user,
        action=AuditLog.ACTION_UPDATE,
        model_name="PortalGSTR2BEntry",
        record_id=entry.pk,
        object_repr=f"{entry.gstin} {entry.invoice_number}",
        old_data=old_data,
        new_data={
            "action_status": entry.action_status,
            "action_note": entry.action_note,
            "matched_voucher_id": entry.matched_voucher_id,
            "source": "ims_action",
        },
    )


def _create_ims_task(company, user, *, entry=None, voucher=None, period_start=None, period_end=None):
    if entry:
        reference = f"IMS2B:{entry.pk}"
        title = f"Resolve IMS/2B invoice {entry.invoice_number}"
        description = (
            f"Supplier: {entry.supplier_name or entry.gstin}\n"
            f"GSTIN: {entry.gstin}\n"
            f"Invoice: {entry.invoice_number} dated {entry.invoice_date:%d %b %Y}\n"
            f"Taxable: Rs.{entry.taxable_value:.2f}\n"
            f"Tax: Rs.{entry.tax_amount:.2f}\n"
            f"Current action: {entry.get_action_status_display()}\n"
            "Book the purchase voucher, accept/reject/pending in IMS, or keep supporting evidence."
        )
        due_date = timezone.localdate() + timezone.timedelta(days=2)
    elif voucher:
        reference = f"IMSBOOK:{voucher.pk}"
        party = _purchase_party_ledger(voucher)
        title = f"Vendor chase for purchase missing in 2B: {voucher.number or voucher.pk}"
        description = (
            f"Purchase voucher {voucher.number or voucher.pk} dated {voucher.date:%d %b %Y} is in books but not found in 2B.\n"
            f"Vendor: {party.name if party else '-'}\n"
            f"Vendor GSTIN: {party.gstin if party else '-'}\n"
            f"Tax: Rs.{voucher.total_tax:.2f}\n"
            "Ask vendor to file/amend GSTR-1/IFF or document ITC treatment."
        )
        due_date = timezone.localdate() + timezone.timedelta(days=2)
    else:
        return None, False

    task, created = PracticeTask.objects.get_or_create(
        company=company,
        reference=reference,
        defaults={
            "title": title,
            "task_type": PracticeTask.TYPE_GST,
            "priority": PracticeTask.PRIORITY_HIGH,
            "status": PracticeTask.STATUS_OPEN,
            "due_date": due_date,
            "period_start": period_start,
            "period_end": period_end,
            "created_by": user,
            "description": description,
        },
    )
    if created:
        AuditLog.objects.create(
            company=company,
            user=user,
            action=AuditLog.ACTION_CREATE,
            model_name="PracticeTask",
            record_id=task.pk,
            object_repr=task.title[:200],
            old_data={},
            new_data={"reference": task.reference, "source": "ims_2b"},
        )
    return task, created


def _vendor_key(gstin, fallback):
    return gstin or f"ledger:{fallback or 'unknown'}"


def _blank_vendor_row(key, name="", gstin=""):
    return {
        "key": key,
        "supplier_name": name or "Unknown Supplier",
        "gstin": gstin or "",
        "email": "",
        "whatsapp_number": "",
        "ledger_id": None,
        "portal_count": 0,
        "portal_tax": Decimal("0.00"),
        "matched_count": 0,
        "matched_tax": Decimal("0.00"),
        "missing_books_count": 0,
        "missing_books_tax": Decimal("0.00"),
        "missing_portal_count": 0,
        "missing_portal_tax": Decimal("0.00"),
        "pending_count": 0,
        "pending_tax": Decimal("0.00"),
        "rejected_count": 0,
        "rejected_tax": Decimal("0.00"),
        "no_action_count": 0,
        "no_action_tax": Decimal("0.00"),
        "ims_action_risk_tax": Decimal("0.00"),
        "itc_at_risk": Decimal("0.00"),
        "risk_score": 0,
        "risk_level": "Low",
        "recommended_action": "Monitor",
        "last_invoice_date": None,
    }


def _risk_level(score, itc_at_risk):
    if score >= 35 or itc_at_risk >= Decimal("100000.00"):
        return "High"
    if score >= 12 or itc_at_risk > Decimal("0.00"):
        return "Medium"
    return "Low"


def _risk_badge_class(level):
    return {
        "High": "text-bg-danger",
        "Medium": "text-bg-warning",
        "Low": "text-bg-success",
    }.get(level, "text-bg-secondary")


def _vendor_task_reference(row, period_start, period_end):
    safe_key = "".join(ch if ch.isalnum() else "-" for ch in row["key"])[:42]
    return f"IMSVENDOR:{safe_key}:{period_start:%Y%m}:{period_end:%Y%m}"[:120]


def _build_vendor_register(company, period_start, period_end, q=""):
    rows = {}
    portal_entries = PortalGSTR2BEntry.objects.filter(
        company=company,
        invoice_date__gte=period_start,
        invoice_date__lte=period_end,
    ).select_related("matched_voucher").order_by("gstin", "supplier_name")

    for entry in portal_entries:
        key = _vendor_key(entry.gstin, entry.pk)
        row = rows.setdefault(key, _blank_vendor_row(key, entry.supplier_name, entry.gstin))
        if entry.supplier_name and row["supplier_name"] == "Unknown Supplier":
            row["supplier_name"] = entry.supplier_name
        row["portal_count"] += 1
        row["portal_tax"] += entry.tax_amount or Decimal("0.00")
        if not row["last_invoice_date"] or entry.invoice_date > row["last_invoice_date"]:
            row["last_invoice_date"] = entry.invoice_date

        if entry.match_status == "matched":
            row["matched_count"] += 1
            row["matched_tax"] += entry.tax_amount or Decimal("0.00")
        elif entry.match_status == "missing_in_books":
            row["missing_books_count"] += 1
            row["missing_books_tax"] += entry.tax_amount or Decimal("0.00")

        if entry.action_status == "pending":
            row["pending_count"] += 1
            row["pending_tax"] += entry.tax_amount or Decimal("0.00")
            if entry.match_status != "missing_in_books":
                row["ims_action_risk_tax"] += entry.tax_amount or Decimal("0.00")
        elif entry.action_status == "rejected":
            row["rejected_count"] += 1
            row["rejected_tax"] += entry.tax_amount or Decimal("0.00")
            if entry.match_status != "missing_in_books":
                row["ims_action_risk_tax"] += entry.tax_amount or Decimal("0.00")
        elif entry.action_status == "new":
            row["no_action_count"] += 1
            row["no_action_tax"] += entry.tax_amount or Decimal("0.00")
            if entry.match_status != "missing_in_books":
                row["ims_action_risk_tax"] += entry.tax_amount or Decimal("0.00")

    gstins = [key for key in rows if len(key) == 15]
    ledger_by_gstin = {
        ledger.gstin: ledger
        for ledger in Ledger.objects.filter(company=company, gstin__in=gstins).select_related("account_group")
    }

    matched_voucher_ids = portal_entries.filter(
        matched_voucher_id__isnull=False,
    ).values_list("matched_voucher_id", flat=True)
    missing_portal_vouchers = Voucher.objects.filter(
        company=company,
        voucher_type="Purchase",
        status="APPROVED",
        is_itc_claimed=False,
        date__gte=period_start,
        date__lte=period_end,
    ).exclude(pk__in=matched_voucher_ids).prefetch_related("items__ledger__account_group")

    for voucher in missing_portal_vouchers:
        party = _purchase_party_ledger(voucher)
        key = _vendor_key(party.gstin if party else "", party.pk if party else voucher.pk)
        row = rows.setdefault(
            key,
            _blank_vendor_row(
                key,
                party.name if party else "Unknown Supplier",
                party.gstin if party else "",
            ),
        )
        if party:
            row["supplier_name"] = party.name
            row["gstin"] = party.gstin or row["gstin"]
            row["email"] = party.email or row["email"]
            row["whatsapp_number"] = party.whatsapp_number or row["whatsapp_number"]
            row["ledger_id"] = party.pk
        row["missing_portal_count"] += 1
        row["missing_portal_tax"] += voucher.total_tax or Decimal("0.00")
        if not row["last_invoice_date"] or voucher.date > row["last_invoice_date"]:
            row["last_invoice_date"] = voucher.date

    for key, row in rows.items():
        ledger = ledger_by_gstin.get(key)
        if ledger:
            row["supplier_name"] = ledger.name or row["supplier_name"]
            row["email"] = ledger.email or row["email"]
            row["whatsapp_number"] = ledger.whatsapp_number or row["whatsapp_number"]
            row["ledger_id"] = ledger.pk
        row["itc_at_risk"] = (
            row["missing_books_tax"]
            + row["missing_portal_tax"]
            + row["ims_action_risk_tax"]
        )
        row["risk_score"] = min(
            100,
            row["missing_portal_count"] * 18
            + row["missing_books_count"] * 14
            + row["rejected_count"] * 12
            + row["pending_count"] * 8
            + row["no_action_count"] * 5,
        )
        row["risk_level"] = _risk_level(row["risk_score"], row["itc_at_risk"])
        if row["missing_portal_count"]:
            row["recommended_action"] = "Vendor filing chase"
        elif row["missing_books_count"]:
            row["recommended_action"] = "Book or reject"
        elif row["rejected_count"]:
            row["recommended_action"] = "Document rejection"
        elif row["pending_count"]:
            row["recommended_action"] = "Follow up pending"
        elif row["no_action_count"]:
            row["recommended_action"] = "Review IMS action"
        else:
            row["recommended_action"] = "Monitor"
        row["risk_badge_class"] = _risk_badge_class(row["risk_level"])
        row["task_reference"] = _vendor_task_reference(row, period_start, period_end)
        row["task_exists"] = PracticeTask.objects.filter(
            company=company,
            reference=row["task_reference"],
        ).exists()

    results = list(rows.values())
    if q:
        needle = q.lower()
        results = [
            row for row in results
            if needle in row["supplier_name"].lower()
            or needle in row["gstin"].lower()
            or needle in row["email"].lower()
            or needle in row["whatsapp_number"].lower()
        ]
    results.sort(key=lambda row: (row["risk_score"], row["itc_at_risk"], row["missing_portal_count"]), reverse=True)
    return results


def _vendor_register_csv(rows, period_start, period_end):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="vendor_gst_register_{period_start:%Y_%m}_{period_end:%Y_%m}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow([
        "Supplier",
        "GSTIN",
        "Email",
        "WhatsApp",
        "Portal Invoices",
        "Matched",
        "Missing In Books",
        "Missing In 2B",
        "Pending",
        "Rejected",
        "No Action",
        "ITC At Risk",
        "Risk Level",
        "Recommended Action",
        "Last Invoice Date",
    ])
    for row in rows:
        writer.writerow([
            row["supplier_name"],
            row["gstin"],
            row["email"],
            row["whatsapp_number"],
            row["portal_count"],
            row["matched_count"],
            row["missing_books_count"],
            row["missing_portal_count"],
            row["pending_count"],
            row["rejected_count"],
            row["no_action_count"],
            f"{row['itc_at_risk']:.2f}",
            row["risk_level"],
            row["recommended_action"],
            row["last_invoice_date"].isoformat() if row["last_invoice_date"] else "",
        ])
    return response


def _vendor_row_or_none(company, period_start, period_end, vendor_key):
    for row in _build_vendor_register(company, period_start, period_end):
        if row["key"] == vendor_key:
            return row
    return None


def _vendor_followup_from_email(company):
    if company.invoice_email_from_address:
        sender_name = company.invoice_email_from_name or company.name
        return formataddr((sender_name, company.invoice_email_from_address))
    return settings.DEFAULT_FROM_EMAIL


def _vendor_followup_reply_to(company):
    if company.invoice_email_reply_to:
        return [company.invoice_email_reply_to]
    if company.invoice_email_from_address:
        return [company.invoice_email_from_address]
    return None


def _vendor_followup_subject(company, row, period_start, period_end):
    return (
        f"GST ITC follow-up: {company.name} "
        f"({period_start:%b %Y} to {period_end:%b %Y})"
    )


def _vendor_followup_body(company, row, period_start, period_end):
    lines = [
        f"Dear {row['supplier_name']},",
        "",
        f"We are reconciling GST ITC for {company.name} for {period_start:%b %Y} to {period_end:%b %Y}.",
        f"GSTIN: {row['gstin'] or '-'}",
        "",
        "The following items need your support:",
    ]
    if row["missing_portal_count"]:
        lines.append(
            f"- {row['missing_portal_count']} purchase invoice(s) booked by us are not visible in GSTR-2B. "
            f"Tax impact: Rs. {row['missing_portal_tax']:.2f}."
        )
    if row["missing_books_count"]:
        lines.append(
            f"- {row['missing_books_count']} invoice(s) are visible in GSTR-2B and need confirmation/supporting documents. "
            f"Tax impact: Rs. {row['missing_books_tax']:.2f}."
        )
    if row["pending_count"] or row["rejected_count"] or row["no_action_count"]:
        lines.append(
            f"- IMS action review: {row['pending_count']} pending, "
            f"{row['rejected_count']} rejected, {row['no_action_count']} not actioned."
        )
    if not any([row["missing_portal_count"], row["missing_books_count"], row["pending_count"], row["rejected_count"], row["no_action_count"]]):
        lines.append("- No current exception is open; this is a confirmation request for our GST records.")

    lines.extend([
        "",
        f"Total ITC currently at risk: Rs. {row['itc_at_risk']:.2f}.",
        "",
        "Please file/amend GSTR-1/IFF where required, share the invoice/supporting details, or confirm the correct treatment.",
        "",
        "Regards,",
        company.name,
    ])
    return "\n".join(lines)


def _vendor_whatsapp_url(number, message):
    if not number:
        return ""
    normalized = normalize_phone_number(number)
    return f"https://wa.me/{normalized.lstrip('+')}?text={quote(message)}"


def _update_vendor_contact(row, email, whatsapp_number):
    if not row.get("ledger_id"):
        return None, {}
    ledger = Ledger.objects.filter(pk=row["ledger_id"]).first()
    if not ledger:
        return None, {}

    changed = {}
    if email and ledger.email != email:
        changed["email"] = {"old": ledger.email, "new": email}
        ledger.email = email
    if whatsapp_number and ledger.whatsapp_number != whatsapp_number:
        changed["whatsapp_number"] = {"old": ledger.whatsapp_number, "new": whatsapp_number}
        ledger.whatsapp_number = whatsapp_number
    if changed:
        ledger.save(update_fields=[*changed.keys(), "updated_at"])
    return ledger, changed


def _ensure_vendor_contact_task(company, user, row, period_start, period_end):
    priority = (
        PracticeTask.PRIORITY_CRITICAL
        if row["risk_level"] == "High"
        else PracticeTask.PRIORITY_HIGH
        if row["risk_level"] == "Medium"
        else PracticeTask.PRIORITY_NORMAL
    )
    task, created = PracticeTask.objects.get_or_create(
        company=company,
        reference=row["task_reference"],
        defaults={
            "title": f"GST vendor follow-up: {row['supplier_name'][:100]}",
            "task_type": PracticeTask.TYPE_GST,
            "priority": priority,
            "status": PracticeTask.STATUS_IN_PROGRESS,
            "due_date": timezone.localdate() + timezone.timedelta(days=3),
            "period_start": period_start,
            "period_end": period_end,
            "created_by": user,
            "description": (
                f"Supplier: {row['supplier_name']}\n"
                f"GSTIN: {row['gstin'] or '-'}\n"
                f"ITC at risk: Rs. {row['itc_at_risk']:.2f}\n"
                f"Recommended action: {row['recommended_action']}"
            ),
        },
    )
    if not created and task.status == PracticeTask.STATUS_OPEN:
        task.status = PracticeTask.STATUS_IN_PROGRESS
        task.save(update_fields=["status", "updated_at"])
    return task, created


def _audit_vendor_followup(company, user, row, *, channel, subject, recipient_email="", whatsapp_number="", task=None):
    AuditLog.objects.create(
        company=company,
        user=user,
        action=AuditLog.ACTION_UPDATE,
        model_name="Ledger" if row.get("ledger_id") else "VendorGSTFollowUp",
        record_id=row.get("ledger_id") or 0,
        object_repr=row["supplier_name"][:200],
        old_data={},
        new_data={
            "source": "vendor_gst_register",
            "channel": channel,
            "recipient_email": recipient_email,
            "whatsapp_number": whatsapp_number,
            "subject": subject,
            "vendor_key": row["key"],
            "gstin": row["gstin"],
            "itc_at_risk": f"{row['itc_at_risk']:.2f}",
            "task_reference": task.reference if task else "",
        },
    )

@login_required
@write_required
def upload_gstr2b(request):
    if request.method == 'POST' and request.FILES.get('json_file'):
        file = request.FILES['json_file']
        try:
            validate_uploaded_file(
                file,
                allowed_extensions=JSON_EXTENSIONS,
                max_mb=10,
                require_signature=False,
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect('gstr2b:upload')
        
        try:
            content = file.read().decode('utf-8')
            entries_data = GSTR2BParser.parse_json(content)
            
            created_count = 0
            updated_count = 0
            with transaction.atomic():
                for entry in entries_data:
                    # Skip entries with unparseable dates
                    if not entry['invoice_date']:
                        continue
                        
                    _, created = PortalGSTR2BEntry.objects.update_or_create(
                        company=request.current_company,
                        gstin=entry['gstin'],
                        invoice_number=entry['invoice_number'],
                        invoice_date=entry['invoice_date'],
                        defaults={
                            'supplier_name': entry['supplier_name'],
                            'taxable_value': entry['taxable_value'],
                            'tax_amount': entry['tax_amount'],
                            'is_matched': False,
                            'match_status': 'missing_in_books',
                            'matched_voucher': None,
                            'match_score': 0,
                        },
                    )
                    if created:
                        created_count += 1
                    else:
                        updated_count += 1
            run_matching(request.current_company)
            messages.success(
                request,
                f"Parsed {len(entries_data)} entries. Added {created_count}, refreshed {updated_count}."
            )
            period = entries_data[0]['invoice_date'].strftime("%Y-%m") if entries_data and entries_data[0]['invoice_date'] else _period_value(timezone.localdate())
            return redirect(f"{reverse('gstr2b:results')}?period={period}")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")
            return redirect('gstr2b:upload')
    return render(request, 'gstr2b/upload.html')

def run_matching(company):
    portal_entries = PortalGSTR2BEntry.objects.filter(company=company)
    books_vouchers = Voucher.objects.filter(company=company, voucher_type='Purchase').prefetch_related('items__ledger')
    
    for entry in portal_entries:
        # Match by:
        # 1. Supplier GSTIN
        # 2. Tax Amount (+/- 2.00 INR for rounding)
        # 3. Invoice Number (Regex word boundary match in narration or number)
        # 4. Date (within +/- 30 days window)
        from datetime import timedelta
        date_min = entry.invoice_date - timedelta(days=30)
        date_max = entry.invoice_date + timedelta(days=30)
        
        matches = books_vouchers.filter(
            items__ledger__gstin=entry.gstin,
            total_tax__gte=entry.tax_amount - Decimal('2.00'),
            total_tax__lte=entry.tax_amount + Decimal('2.00'),
            date__range=(date_min, date_max)
        )
        
        # Refine by invoice number search
        found_match = None
        for vch in matches:
            import re
            pattern = rf'\b{re.escape(entry.invoice_number)}\b'
            if re.search(pattern, vch.number) or re.search(pattern, vch.narration):
                found_match = vch
                break
        
        if found_match:
            entry.is_matched = True
            entry.match_status = 'matched'
            entry.matched_voucher = found_match
            entry.match_score = 90
            if entry.action_status == "new":
                entry.action_status = 'accepted'
            entry.save(update_fields=[
                'is_matched',
                'match_status',
                'matched_voucher',
                'match_score',
                'action_status',
                'updated_at',
            ])
            found_match.is_itc_claimed = True
            found_match.save(update_fields=['is_itc_claimed'])
        else:
            entry.is_matched = False
            entry.match_status = 'missing_in_books'
            entry.matched_voucher = None
            entry.match_score = 0
            entry.save(update_fields=['is_matched', 'match_status', 'matched_voucher', 'match_score', 'updated_at'])

@login_required
@write_required
def reconciliation_results(request):
    company = request.current_company
    period_start, period_end = _month_bounds(request.GET.get("period"))
    filters = _parse_filters(request)
    portal_base = PortalGSTR2BEntry.objects.filter(
        company=company,
        invoice_date__gte=period_start,
        invoice_date__lte=period_end,
    ).select_related("matched_voucher").order_by('-invoice_date', 'supplier_name', 'invoice_number')
    filtered_entries = _apply_portal_filters(portal_base, filters)
    matched = filtered_entries.filter(match_status='matched')
    missing_in_books = filtered_entries.filter(match_status='missing_in_books')
    matched_voucher_ids = portal_base.filter(
        matched_voucher_id__isnull=False,
    ).values_list("matched_voucher_id", flat=True)
    missing_portal_base = Voucher.objects.filter(
        company=company,
        voucher_type='Purchase',
        status='APPROVED',
        is_itc_claimed=False,
        date__gte=period_start,
        date__lte=period_end,
    ).exclude(pk__in=matched_voucher_ids).prefetch_related("items__ledger__account_group").order_by('-date')
    missing_in_portal = missing_portal_base
    if filters["status"] not in {"all", "missing_in_portal"} or filters["action"] != "all":
        missing_in_portal = Voucher.objects.none()
    elif filters["q"]:
        missing_in_portal = missing_in_portal.filter(
            models.Q(number__icontains=filters["q"])
            | models.Q(narration__icontains=filters["q"])
            | models.Q(items__ledger__name__icontains=filters["q"])
            | models.Q(items__ledger__gstin__icontains=filters["q"])
        ).distinct()

    missing_portal_rows = [
        {
            "voucher": voucher,
            "party": _purchase_party_ledger(voucher),
            "task_exists": PracticeTask.objects.filter(company=company, reference=f"IMSBOOK:{voucher.pk}").exists(),
        }
        for voucher in missing_in_portal
    ]
    task_refs = set(
        PracticeTask.objects.filter(
            company=company,
            reference__in=[f"IMS2B:{entry.pk}" for entry in filtered_entries],
        ).values_list("reference", flat=True)
    )
    accepted_itc = portal_base.filter(action_status="accepted").aggregate(total=models.Sum("tax_amount"))["total"] or Decimal("0.00")
    pending_itc = portal_base.filter(action_status="pending").aggregate(total=models.Sum("tax_amount"))["total"] or Decimal("0.00")
    rejected_itc = portal_base.filter(action_status="rejected").aggregate(total=models.Sum("tax_amount"))["total"] or Decimal("0.00")
    no_action_itc = portal_base.filter(action_status="new").aggregate(total=models.Sum("tax_amount"))["total"] or Decimal("0.00")
    missing_portal_itc = sum((voucher.total_tax or Decimal("0.00") for voucher in missing_portal_base), Decimal("0.00"))

    summary = {
        'matched': portal_base.filter(match_status='matched').count(),
        'missing_in_books': portal_base.filter(match_status='missing_in_books').count(),
        'missing_in_portal': missing_portal_base.count(),
        'pending_actions': portal_base.filter(action_status='pending').count(),
        'rejected': portal_base.filter(action_status='rejected').count(),
        'no_action': portal_base.filter(action_status='new').count(),
        'accepted_itc': accepted_itc,
        'pending_itc': pending_itc,
        'rejected_itc': rejected_itc,
        'pending_rejected_itc': pending_itc + rejected_itc,
        'no_action_itc': no_action_itc,
        'missing_books_itc': portal_base.filter(match_status='missing_in_books').aggregate(total=models.Sum("tax_amount"))["total"] or Decimal("0.00"),
        'missing_portal_itc': missing_portal_itc,
        'itc_at_risk': pending_itc + rejected_itc + no_action_itc + missing_portal_itc,
    }

    if request.GET.get("export") == "csv":
        return _reconciliation_csv(filtered_entries, missing_portal_rows, period_start)

    return render(request, 'gstr2b/results.html', {
        'matched': matched,
        'missing_in_books': missing_in_books,
        'missing_in_portal': missing_portal_rows,
        'result_counts': {
            'matched': matched.count(),
            'missing_in_books': missing_in_books.count(),
            'missing_in_portal': len(missing_portal_rows),
        },
        'summary': summary,
        'period_value': _period_value(period_start),
        'period_start': period_start,
        'period_end': period_end,
        'filters': filters,
        'filter_query': urlencode({**filters, "period": _period_value(period_start)}),
        'status_choices': [("all", "All match statuses"), *PortalGSTR2BEntry.MATCH_STATUS_CHOICES],
        'action_choices': [("all", "All IMS actions"), *PortalGSTR2BEntry.ACTION_STATUS_CHOICES],
        'task_refs': task_refs,
        'action_badge_class': _action_badge_class,
    })


def _reconciliation_csv(entries, missing_portal_rows, period_start):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="ims_2b_review_{period_start:%Y_%m}.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Section",
        "Supplier",
        "GSTIN",
        "Invoice",
        "Invoice Date",
        "Taxable",
        "Tax",
        "Match Status",
        "IMS Action",
        "Action Note",
        "Voucher",
    ])
    for entry in entries:
        writer.writerow([
            "Portal 2B",
            entry.supplier_name,
            entry.gstin,
            entry.invoice_number,
            entry.invoice_date.isoformat(),
            f"{entry.taxable_value:.2f}",
            f"{entry.tax_amount:.2f}",
            entry.get_match_status_display(),
            entry.get_action_status_display(),
            entry.action_note,
            entry.matched_voucher.number if entry.matched_voucher else "",
        ])
    for row in missing_portal_rows:
        voucher = row["voucher"]
        party = row["party"]
        writer.writerow([
            "Books Missing In 2B",
            party.name if party else "",
            party.gstin if party else "",
            voucher.number,
            voucher.date.isoformat(),
            "",
            f"{voucher.total_tax:.2f}",
            "Missing in Portal",
            "",
            "",
            voucher.number,
        ])
    return response


@login_required
@write_required
def vendor_compliance_register(request):
    period_start, period_end = _range_bounds(
        request.GET.get("from_period"),
        request.GET.get("to_period"),
    )
    q = request.GET.get("q", "").strip()
    rows = _build_vendor_register(request.current_company, period_start, period_end, q=q)
    summary = {
        "vendor_count": len(rows),
        "high_risk_count": sum(1 for row in rows if row["risk_level"] == "High"),
        "task_count": sum(1 for row in rows if row["task_exists"]),
        "itc_at_risk": sum((row["itc_at_risk"] for row in rows), Decimal("0.00")),
        "missing_portal_count": sum(row["missing_portal_count"] for row in rows),
        "missing_books_count": sum(row["missing_books_count"] for row in rows),
    }

    if request.GET.get("export") == "csv":
        return _vendor_register_csv(rows, period_start, period_end)

    query = urlencode({
        "from_period": _period_value(period_start),
        "to_period": _period_value(period_end.replace(day=1)),
        "q": q,
    })
    for row in rows:
        row["followup_query"] = urlencode({
            "vendor_key": row["key"],
            "from_period": _period_value(period_start),
            "to_period": _period_value(period_end.replace(day=1)),
        })
    return render(request, "gstr2b/vendor_register.html", {
        "rows": rows,
        "summary": summary,
        "from_period": _period_value(period_start),
        "to_period": _period_value(period_end.replace(day=1)),
        "period_start": period_start,
        "period_end": period_end,
        "q": q,
        "query": query,
    })


@login_required
@write_required
def vendor_followup(request):
    company = request.current_company
    period_start, period_end = _range_bounds(
        request.POST.get("from_period") or request.GET.get("from_period"),
        request.POST.get("to_period") or request.GET.get("to_period"),
    )
    vendor_key = (request.POST.get("vendor_key") or request.GET.get("vendor_key") or "").strip()
    row = _vendor_row_or_none(company, period_start, period_end, vendor_key)
    if not row:
        messages.error(request, "Vendor GST risk row was not found for this period.")
        return redirect(
            f"{reverse('gstr2b:vendor_register')}?{urlencode({'from_period': _period_value(period_start), 'to_period': _period_value(period_end.replace(day=1))})}"
        )

    default_subject = _vendor_followup_subject(company, row, period_start, period_end)
    default_body = _vendor_followup_body(company, row, period_start, period_end)
    recipient_email = (request.POST.get("recipient_email") or row["email"] or "").strip()
    raw_whatsapp = (request.POST.get("whatsapp_number") or row["whatsapp_number"] or "").strip()
    subject = (request.POST.get("subject") or default_subject).strip()
    body = request.POST.get("message") or default_body
    whatsapp_url = ""

    if request.method == "POST":
        channel = request.POST.get("channel", "email")
        normalized_whatsapp = ""
        if raw_whatsapp:
            try:
                normalized_whatsapp = normalize_phone_number(raw_whatsapp)
            except ValueError as exc:
                messages.error(request, str(exc))
                normalized_whatsapp = ""

        if channel == "email":
            if not recipient_email:
                messages.error(request, "Add the vendor email address before sending.")
            else:
                try:
                    validate_email(recipient_email)
                except ValidationError:
                    messages.error(request, "Enter a valid vendor email address.")
                else:
                    ledger, changes = _update_vendor_contact(row, recipient_email, normalized_whatsapp)
                    task, _ = _ensure_vendor_contact_task(company, request.user, row, period_start, period_end)
                    email = EmailMessage(
                        subject=subject,
                        body=body,
                        from_email=_vendor_followup_from_email(company),
                        to=[recipient_email],
                        reply_to=_vendor_followup_reply_to(company),
                    )
                    try:
                        email.send(fail_silently=False)
                    except Exception as exc:
                        messages.error(request, f"Vendor follow-up email could not be sent: {exc}")
                    else:
                        _audit_vendor_followup(
                            company,
                            request.user,
                            row,
                            channel="email",
                            subject=subject,
                            recipient_email=recipient_email,
                            whatsapp_number=normalized_whatsapp,
                            task=task,
                        )
                        if changes and ledger:
                            messages.info(request, f"Updated contact details for {ledger.name}.")
                        messages.success(request, f"GST follow-up emailed to {recipient_email}.")
                        return redirect(
                            f"{reverse('gstr2b:vendor_register')}?{urlencode({'from_period': _period_value(period_start), 'to_period': _period_value(period_end.replace(day=1))})}"
                        )
        elif channel == "whatsapp":
            if not normalized_whatsapp:
                messages.error(request, "Add a valid vendor WhatsApp number first.")
            else:
                ledger, changes = _update_vendor_contact(row, recipient_email, normalized_whatsapp)
                task, _ = _ensure_vendor_contact_task(company, request.user, row, period_start, period_end)
                whatsapp_url = _vendor_whatsapp_url(normalized_whatsapp, body)
                _audit_vendor_followup(
                    company,
                    request.user,
                    row,
                    channel="whatsapp_link",
                    subject=subject,
                    recipient_email=recipient_email,
                    whatsapp_number=normalized_whatsapp,
                    task=task,
                )
                if changes and ledger:
                    messages.info(request, f"Updated contact details for {ledger.name}.")
                messages.success(request, "WhatsApp follow-up link is ready.")
        else:
            messages.error(request, "Unknown follow-up channel.")

    if not whatsapp_url and raw_whatsapp:
        try:
            whatsapp_url = _vendor_whatsapp_url(raw_whatsapp, body)
        except ValueError:
            whatsapp_url = ""

    return render(request, "gstr2b/vendor_followup.html", {
        "row": row,
        "from_period": _period_value(period_start),
        "to_period": _period_value(period_end.replace(day=1)),
        "period_start": period_start,
        "period_end": period_end,
        "vendor_key": vendor_key,
        "recipient_email": recipient_email,
        "whatsapp_number": raw_whatsapp,
        "subject": subject,
        "message": body,
        "whatsapp_url": whatsapp_url,
    })


@login_required
@write_required
@require_POST
def bulk_create_vendor_tasks(request):
    period_start, period_end = _range_bounds(
        request.POST.get("from_period"),
        request.POST.get("to_period"),
    )
    selected_keys = set(request.POST.getlist("vendor_keys"))
    if not selected_keys:
        messages.warning(request, "Select at least one vendor.")
        return redirect(
            f"{reverse('gstr2b:vendor_register')}?{urlencode({'from_period': _period_value(period_start), 'to_period': _period_value(period_end.replace(day=1))})}"
        )

    rows = [
        row for row in _build_vendor_register(request.current_company, period_start, period_end)
        if row["key"] in selected_keys
    ]
    created = 0
    existing = 0
    for row in rows:
        priority = (
            PracticeTask.PRIORITY_CRITICAL
            if row["risk_level"] == "High"
            else PracticeTask.PRIORITY_HIGH
            if row["risk_level"] == "Medium"
            else PracticeTask.PRIORITY_NORMAL
        )
        task, was_created = PracticeTask.objects.get_or_create(
            company=request.current_company,
            reference=row["task_reference"],
            defaults={
                "title": f"GST vendor follow-up: {row['supplier_name'][:100]}",
                "task_type": PracticeTask.TYPE_GST,
                "priority": priority,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": timezone.localdate() + timezone.timedelta(days=3),
                "period_start": period_start,
                "period_end": period_end,
                "created_by": request.user,
                "description": (
                    f"Supplier: {row['supplier_name']}\n"
                    f"GSTIN: {row['gstin'] or '-'}\n"
                    f"Email: {row['email'] or '-'}\n"
                    f"ITC at risk: Rs. {row['itc_at_risk']:.2f}\n"
                    f"Missing in books: {row['missing_books_count']} invoice(s), Rs. {row['missing_books_tax']:.2f}\n"
                    f"Missing in 2B: {row['missing_portal_count']} invoice(s), Rs. {row['missing_portal_tax']:.2f}\n"
                    f"Pending: {row['pending_count']} | Rejected: {row['rejected_count']} | No action: {row['no_action_count']}\n"
                    f"Recommended action: {row['recommended_action']}"
                ),
            },
        )
        if was_created:
            created += 1
            AuditLog.objects.create(
                company=request.current_company,
                user=request.user,
                action=AuditLog.ACTION_CREATE,
                model_name="PracticeTask",
                record_id=task.pk,
                object_repr=task.title[:200],
                old_data={},
                new_data={"reference": task.reference, "source": "vendor_gst_register"},
            )
        else:
            existing += 1

    if created:
        messages.success(request, f"Created {created} vendor GST follow-up task(s). {existing} already existed.")
    else:
        messages.info(request, "No new vendor GST tasks were needed.")
    return redirect(
        f"{reverse('gstr2b:vendor_register')}?{urlencode({'from_period': _period_value(period_start), 'to_period': _period_value(period_end.replace(day=1))})}"
    )


def _create_draft_purchase_voucher(company, entry):
    """Helper to create a single draft purchase voucher from a 2B entry."""
    # 1. Safety Check: Duplicate Detection (Exact word boundary match)
    import re
    from django.db.models import Q
    
    # Check if any existing purchase voucher for this GSTIN has this invoice number in narration or number
    existing_vouchers = Voucher.objects.filter(
        company=company,
        voucher_type='Purchase',
        items__ledger__gstin=entry.gstin
    )
    
    pattern = rf'\b{re.escape(entry.invoice_number)}\b'
    is_duplicate = False
    for v in existing_vouchers:
        if re.search(pattern, v.number) or re.search(pattern, v.narration):
            is_duplicate = True
            break

    if is_duplicate:
        return None, "Duplicate"

    try:
        with transaction.atomic():
            # Get or create ledgers with more robust logic
            creditors_group, _ = AccountGroup.objects.get_or_create(
                company=company, name="Sundry Creditors", 
                defaults={'nature': 'Liability'}
            )
            
            supplier_ledger = Ledger.objects.filter(company=company, gstin=entry.gstin).first()
            if not supplier_ledger:
                supplier_ledger = Ledger.objects.create(
                    company=company, 
                    name=entry.supplier_name or f"Supplier {entry.gstin}",
                    gstin=entry.gstin,
                    account_group=creditors_group
                )
            
            purchase_group, _ = AccountGroup.objects.get_or_create(
                company=company, name="Purchase Accounts", 
                defaults={'nature': 'Expense'}
            )
            purchase_ledger, _ = Ledger.objects.get_or_create(
                company=company, name="Purchase GST", 
                defaults={'account_group': purchase_group}
            )
            
            tax_group, _ = AccountGroup.objects.get_or_create(
                company=company, name="Tax", 
                defaults={'nature': 'Tax'}
            )

            # Determine Tax Split (Intra-state vs Inter-state)
            company_state = company.gstin[:2] if (company.gstin and len(company.gstin) >= 2) else "27"
            party_state = entry.gstin[:2]
            is_interstate = company_state != party_state

            vch = Voucher.objects.create(
                company=company, 
                voucher_type='Purchase', 
                date=entry.invoice_date,
                narration=f"GSTR-2B Auto-Import | Inv: {entry.invoice_number}", 
                status='DRAFT',
                is_itc_claimed=True,
            )
            
            # Purchase Line
            VoucherItem.objects.create(voucher=vch, ledger=purchase_ledger, entry_type='DR', amount=entry.taxable_value)
            
            # Tax Lines
            if entry.tax_amount > 0:
                if is_interstate:
                    ledger_igst, _ = Ledger.objects.get_or_create(
                        company=company, name="IGST Input",
                        defaults={'account_group': tax_group}
                    )
                    VoucherItem.objects.create(voucher=vch, ledger=ledger_igst, entry_type='DR', amount=entry.tax_amount)
                    vch.igst_amount = entry.tax_amount
                else:
                    cgst = (entry.tax_amount / 2).quantize(Decimal("0.01"))
                    sgst = entry.tax_amount - cgst
                    ledger_cgst, _ = Ledger.objects.get_or_create(
                        company=company, name="CGST Input",
                        defaults={'account_group': tax_group}
                    )
                    ledger_sgst, _ = Ledger.objects.get_or_create(
                        company=company, name="SGST Input",
                        defaults={'account_group': tax_group}
                    )
                    VoucherItem.objects.create(voucher=vch, ledger=ledger_cgst, entry_type='DR', amount=cgst)
                    VoucherItem.objects.create(voucher=vch, ledger=ledger_sgst, entry_type='DR', amount=sgst)
                    vch.cgst_amount = cgst
                    vch.sgst_amount = sgst
                
                vch.total_tax = entry.tax_amount
                vch.save(update_fields=['cgst_amount', 'sgst_amount', 'igst_amount', 'total_tax'])
            
            # Party Line
            VoucherItem.objects.create(voucher=vch, ledger=supplier_ledger, entry_type='CR', amount=entry.taxable_value + entry.tax_amount)

            entry.is_matched = True
            entry.match_status = 'matched'
            entry.matched_voucher = vch
            entry.match_score = 100
            entry.action_status = "accepted"
            entry.save(update_fields=[
                'is_matched',
                'match_status',
                'matched_voucher',
                'match_score',
                'action_status',
                'updated_at',
            ])
            return vch, "Success"
    except Exception as e:
        return None, str(e)

@login_required
@write_required
def create_voucher_from_2b(request, pk):
    entry = get_object_or_404(PortalGSTR2BEntry, pk=pk, company=request.current_company)
    vch, status = _create_draft_purchase_voucher(request.current_company, entry)
    
    if status == "Success":
        messages.success(request, f"Draft Purchase Voucher created for {entry.invoice_number}.")
        return redirect('vouchers:edit', pk=vch.pk)
    elif status == "Duplicate":
        messages.warning(request, f"A voucher for Invoice {entry.invoice_number} already exists.")
    else:
        messages.error(request, f"Failed: {status}")
    return redirect('gstr2b:results')


@login_required
@write_required
@require_POST
def mark_2b_action(request, pk):
    entry = get_object_or_404(PortalGSTR2BEntry, pk=pk, company=request.current_company)
    action = request.POST.get("action_status", "new")
    note = request.POST.get("action_note", "").strip()
    allowed = {choice[0] for choice in PortalGSTR2BEntry.ACTION_STATUS_CHOICES}
    if action not in allowed:
        messages.error(request, "Invalid GSTR-2B action.")
        return redirect('gstr2b:results')

    _sync_entry_action(entry, request.user, action, note)
    messages.success(request, f"Marked invoice {entry.invoice_number} as {entry.get_action_status_display()}.")
    period_start, _ = _month_bounds(request.POST.get("period") or entry.invoice_date.strftime("%Y-%m"))
    return redirect(_results_url(period_start))


@login_required
@write_required
@require_POST
def bulk_mark_2b_action(request):
    action = request.POST.get("action_status", "")
    note = request.POST.get("action_note", "").strip()
    entry_ids = request.POST.getlist("entry_ids")
    period_start, _ = _month_bounds(request.POST.get("period"))
    allowed = {choice[0] for choice in PortalGSTR2BEntry.ACTION_STATUS_CHOICES}
    if action not in allowed:
        messages.error(request, "Invalid IMS action.")
        return redirect(_results_url(period_start))
    if not entry_ids:
        messages.warning(request, "Select at least one 2B invoice.")
        return redirect(_results_url(period_start))

    entries = PortalGSTR2BEntry.objects.filter(pk__in=entry_ids, company=request.current_company).select_related("matched_voucher")
    updated = 0
    with transaction.atomic():
        for entry in entries:
            _sync_entry_action(entry, request.user, action, note)
            updated += 1
    messages.success(request, f"Marked {updated} invoice(s) as {dict(PortalGSTR2BEntry.ACTION_STATUS_CHOICES)[action]}.")
    return redirect(_results_url(period_start))


@login_required
@write_required
@require_POST
def bulk_create_ims_tasks(request):
    period_start, period_end = _month_bounds(request.POST.get("period"))
    entry_ids = request.POST.getlist("entry_ids")
    voucher_ids = request.POST.getlist("voucher_ids")
    created = 0
    existing = 0

    for entry in PortalGSTR2BEntry.objects.filter(pk__in=entry_ids, company=request.current_company):
        _, was_created = _create_ims_task(
            request.current_company,
            request.user,
            entry=entry,
            period_start=period_start,
            period_end=period_end,
        )
        if was_created:
            created += 1
        else:
            existing += 1

    for voucher in Voucher.objects.filter(pk__in=voucher_ids, company=request.current_company, voucher_type="Purchase"):
        _, was_created = _create_ims_task(
            request.current_company,
            request.user,
            voucher=voucher,
            period_start=period_start,
            period_end=period_end,
        )
        if was_created:
            created += 1
        else:
            existing += 1

    if created:
        messages.success(request, f"Created {created} IMS follow-up task(s). {existing} already existed.")
    else:
        messages.info(request, "No new IMS follow-up tasks were needed.")
    return redirect(_results_url(period_start))

@login_required
@write_required
def bulk_create_vouchers_from_2b(request):
    if request.method != 'POST':
        return redirect('gstr2b:results')
    
    period_start, _ = _month_bounds(request.POST.get("period"))
    entry_ids = request.POST.getlist('entry_ids')
    if not entry_ids:
        messages.warning(request, "No entries selected.")
        return redirect(_results_url(period_start))

    created_count = 0
    duplicate_count = 0
    failed_count = 0
    
    for eid in entry_ids:
        try:
            entry = PortalGSTR2BEntry.objects.get(pk=eid, company=request.current_company)
            vch, status = _create_draft_purchase_voucher(request.current_company, entry)
            if status == "Success":
                created_count += 1
            elif status == "Duplicate":
                duplicate_count += 1
            else:
                failed_count += 1
        except PortalGSTR2BEntry.DoesNotExist:
            failed_count += 1

    msg = f"Bulk processing complete: {created_count} vouchers created."
    if duplicate_count > 0:
        msg += f" {duplicate_count} skipped as duplicates."
    if failed_count > 0:
        msg += f" {failed_count} entries failed."
    
    if failed_count > 0:
        messages.warning(request, msg)
    else:
        messages.success(request, msg)
        
    return redirect(_results_url(period_start))
