"""
vouchers/views.py

Voucher CRUD + bulk actions + advanced filtering + pagination + simulate payment.

Phase 4.1 addition:
  - voucher_create and voucher_edit now also accept an optional
    VoucherStockItemFormSet.  When Sales or Purchase vouchers are saved:
      • VoucherStockItem rows are saved (inline to the Voucher).
      • Old StockLedger entries for this voucher are deleted and recreated so
        edits are idempotent.
    All of this happens inside the same transaction.atomic() block that saves
    the Voucher and VoucherItems — stock and accounting stay in sync.

Access control:
  list / detail         → all authenticated users with company access
  create / edit         → Admin, Accountant
  delete                → Admin only
  bulk_delete           → Admin only
  bulk_export_pdf       → all roles (read-only export)
  simulate_payment      → Admin, Accountant
"""

import csv
from datetime import date as _date, timedelta
from decimal import Decimal
from email.utils import formataddr
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.utils.crypto import constant_time_compare
from django.utils.http import url_has_allowed_host_and_scheme
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST

from core.decorators import admin_required, write_required
from core.models import AuditLog, PracticeTask, UserCompanyAccess
from integrations.gst import build_gst_voucher_execution_context
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.utils import timezone
from .models import Voucher, VoucherItem
from .forms import VoucherForm, VoucherItemFormSet
from . import tally_exporter
from .quality import build_voucher_quality_report

from django.views.decorators.csrf import csrf_exempt
import json

@csrf_exempt
@require_POST
def whatsapp_webhook(request):
    """
    Webhook for receiving WhatsApp replies (e.g. from Twilio/Meta).
    Expected JSON: {"from": "phone_no", "body": "YES", "voucher_id": 123}
    """
    webhook_token = getattr(settings, "WHATSAPP_WEBHOOK_TOKEN", "")
    supplied_token = request.headers.get("X-Webhook-Token", "") or request.GET.get("token", "")
    if not webhook_token:
        return JsonResponse({"status": "error", "message": "Webhook approval is not configured."}, status=503)
    if not supplied_token or not constant_time_compare(supplied_token, webhook_token):
        return JsonResponse({"status": "error", "message": "Unauthorized webhook request."}, status=403)

    try:
        data = json.loads(request.body)
        body = data.get("body", "").strip().upper()
        voucher_id = data.get("voucher_id")

        if not voucher_id:
            return JsonResponse({"status": "error", "message": "Missing voucher_id"}, status=400)

        if body == "YES":
            with transaction.atomic():
                voucher = Voucher.objects.select_for_update().get(pk=voucher_id)
                if voucher.status == 'PENDING':
                    voucher.approve(None)
                    return JsonResponse({"status": "approved", "voucher": voucher.number})

        return JsonResponse({"status": "received", "body": body})
    except (Voucher.DoesNotExist, ValueError):
        return JsonResponse({"status": "error", "message": "Invalid data"}, status=400)
    except ValidationError as exc:
        return JsonResponse({"status": "error", "message": "; ".join(exc.messages)}, status=400)

@login_required
def export_to_tally(request):
    """
    Exports filtered vouchers (or all) to Tally-compatible XML.
    """
    company = request.current_company
    vouchers = Voucher.objects.filter(company=company).prefetch_related("items__ledger")
    
    # Filter by date range if provided
    start_date = request.GET.get("start_date")
    end_date = request.GET.get("end_date")
    if start_date:
        vouchers = vouchers.filter(date__gte=start_date)
    if end_date:
        vouchers = vouchers.filter(date__lte=end_date)

    xml_data = tally_exporter.vouchers_to_tally_xml(vouchers)
    
    response = HttpResponse(xml_data, content_type="application/xml")
    filename = f"TallyExport_{company.short_code}_{timezone.now().strftime('%Y%m%d')}.xml"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
from .utils import generate_upi_qr

PAGE_SIZE = 25


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _make_formset(company, *args, **kwargs):
    """Return a VoucherItemFormSet with ledger choices scoped to the company."""
    # Ensure form_kwargs contains the company so VoucherItemForm.__init__ can filter querysets
    form_kwargs = kwargs.get('form_kwargs', {})
    form_kwargs['company'] = company
    kwargs['form_kwargs'] = form_kwargs
    
    fs = VoucherItemFormSet(*args, **kwargs)
    return fs


def _make_stock_formset(company, *args, **kwargs):
    """
    Return a VoucherStockItemFormSet with stock item choices scoped to the company.
    Imported lazily to avoid circular-import issues at module load time.
    """
    from inventory.forms import VoucherStockItemFormSet
    
    # Ensure form_kwargs contains the company so VoucherStockItemForm.__init__ can filter querysets
    form_kwargs = kwargs.get('form_kwargs', {})
    form_kwargs['company'] = company
    kwargs['form_kwargs'] = form_kwargs

    fs = VoucherStockItemFormSet(*args, **kwargs)
    return fs


def _parse_filters(request):
    """Extract and sanitise GET filter params. Returns (filters_dict, filter_qs_str)."""
    filters = {
        "q":            request.GET.get("q", "").strip(),
        "start_date":   request.GET.get("start_date", "").strip(),
        "end_date":     request.GET.get("end_date", "").strip(),
        "voucher_type": request.GET.get("voucher_type", "").strip(),
        "ledger":       request.GET.get("ledger", "").strip(),
    }
    # Build a query-string fragment for pagination links (?page=N&q=...&...)
    active = {k: v for k, v in filters.items() if v}
    filter_qs = ("&" + urlencode(active)) if active else ""
    return filters, filter_qs


def _apply_filters(qs, filters):
    """Apply the filter dict to a Voucher queryset."""
    q            = filters["q"]
    start_date   = filters["start_date"]
    end_date     = filters["end_date"]
    voucher_type = filters["voucher_type"]
    ledger       = filters["ledger"]

    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(narration__icontains=q))
    if start_date:
        try:
            qs = qs.filter(date__gte=_date.fromisoformat(start_date))
        except ValueError:
            pass
    if end_date:
        try:
            qs = qs.filter(date__lte=_date.fromisoformat(end_date))
        except ValueError:
            pass
    if voucher_type:
        qs = qs.filter(voucher_type=voucher_type)
    if ledger:
        qs = qs.filter(items__ledger__name__icontains=ledger).distinct()

    return qs


def _parse_iso_date(value):
    if not value:
        return None
    try:
        return _date.fromisoformat(value)
    except ValueError:
        return None


def _current_fy_start(today):
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    return _date(fy_start_year, 4, 1)


def _parse_quality_filters(request):
    today = timezone.localdate()
    filters = {
        "start_date": request.GET.get("start_date", "").strip() or _current_fy_start(today).isoformat(),
        "end_date": request.GET.get("end_date", "").strip() or today.isoformat(),
        "status": request.GET.get("status", "all").strip() or "all",
        "voucher_type": request.GET.get("voucher_type", "").strip(),
        "q": request.GET.get("q", "").strip(),
    }
    return filters, _parse_iso_date(filters["start_date"]), _parse_iso_date(filters["end_date"])


def _can_write_current_company(request):
    company = getattr(request, "current_company", None)
    if not company:
        return False
    return UserCompanyAccess.objects.filter(
        user=request.user,
        company=company,
        role__in=["Admin", "Accountant"],
    ).exists()


def _party_ledger_for_entry(voucher, *, entry_type, preferred_nature):
    party_items = [
        item for item in voucher.items.select_related("ledger", "ledger__account_group").all()
        if item.entry_type == entry_type
    ]
    if not party_items:
        return None
    for item in party_items:
        if item.ledger.account_group.nature == preferred_nature:
            return item.ledger
    return party_items[0].ledger


def _sales_party_ledger(voucher):
    if voucher.voucher_type != "Sales":
        return None
    return _party_ledger_for_entry(voucher, entry_type="DR", preferred_nature="Asset")


def _purchase_party_ledger(voucher):
    if voucher.voucher_type != "Purchase":
        return None
    return _party_ledger_for_entry(voucher, entry_type="CR", preferred_nature="Liability")


def _outstanding_party_ledger(voucher):
    if voucher.voucher_type == "Sales":
        return _sales_party_ledger(voucher)
    if voucher.voucher_type == "Purchase":
        return _purchase_party_ledger(voucher)
    return None


def _invoice_email_context(company, voucher, client_name):
    return {
        "voucher_number": voucher.number,
        "company_name": company.name,
        "client_name": client_name or "Customer",
        "amount": f"Rs. {voucher.total_debit():.2f}",
    }


def _format_invoice_email_template(template, context):
    try:
        return (template or "").format(**context)
    except (KeyError, ValueError):
        return template or ""


def _invoice_from_email(company):
    if company.invoice_email_from_address:
        sender_name = company.invoice_email_from_name or company.name
        return formataddr((sender_name, company.invoice_email_from_address))
    return settings.DEFAULT_FROM_EMAIL


def _invoice_reply_to(company):
    if company.invoice_email_reply_to:
        return [company.invoice_email_reply_to]
    if company.invoice_email_from_address:
        return [company.invoice_email_from_address]
    return None


def _render_invoice_pdf_bytes(request, voucher, company):
    import weasyprint

    qr_code = generate_upi_qr(voucher) if voucher.voucher_type == "Sales" else None
    html_str = render_to_string("vouchers/invoice_pdf.html", {
        "voucher": voucher,
        "company": company,
        "qr_code": qr_code,
        "today": _date.today(),
    }, request=request)
    return weasyprint.HTML(string=html_str).write_pdf()


def _send_invoice_email(request, voucher, recipient_email, party_ledger):
    company = request.current_company
    context = _invoice_email_context(company, voucher, party_ledger.name if party_ledger else "")
    subject_template = company.invoice_email_subject or "Invoice {voucher_number} from {company_name}"
    body_template = company.invoice_email_body or (
        "Dear {client_name},\n\n"
        "Please find attached invoice {voucher_number} from {company_name} for {amount}.\n\n"
        "Regards,\n{company_name}"
    )
    subject = _format_invoice_email_template(subject_template, context)
    body = _format_invoice_email_template(body_template, context)
    pdf_bytes = _render_invoice_pdf_bytes(request, voucher, company)
    filename = f"Invoice_{voucher.number}.pdf"

    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=_invoice_from_email(company),
        to=[recipient_email],
        reply_to=_invoice_reply_to(company),
    )
    email.attach(filename, pdf_bytes, "application/pdf")
    email.send(fail_silently=False)


def _send_payment_reminder_email(request, voucher, recipient_email, party_ledger):
    company = request.current_company
    outstanding = voucher.outstanding_amount or Decimal("0.00")
    due_date = voucher.due_date.strftime("%d %b %Y") if voucher.due_date else "as per agreed payment terms"
    days_overdue = (timezone.localdate() - voucher.due_date).days if voucher.due_date else None
    if days_overdue is None:
        aging_line = "The invoice is currently unpaid."
    elif days_overdue > 0:
        aging_line = f"The invoice is {days_overdue} day(s) overdue."
    elif days_overdue == 0:
        aging_line = "The invoice is due today."
    else:
        aging_line = f"The invoice is due in {abs(days_overdue)} day(s)."

    context = _invoice_email_context(company, voucher, party_ledger.name if party_ledger else "")
    context.update({
        "outstanding": f"Rs. {outstanding:.2f}",
        "due_date": due_date,
        "aging_line": aging_line,
    })
    subject_template = company.payment_reminder_email_subject or (
        "Payment reminder: Invoice {voucher_number} from {company_name}"
    )
    body_template = company.payment_reminder_email_body or (
        "Dear {client_name},\n\n"
        "This is a payment reminder for invoice {voucher_number} from {company_name}.\n"
        "Outstanding amount: {outstanding}\n"
        "Due date: {due_date}\n"
        "{aging_line}\n\n"
        "Please ignore this message if payment has already been made.\n\n"
        "Regards,\n{company_name}"
    )
    subject = _format_invoice_email_template(subject_template, context)
    body = _format_invoice_email_template(body_template, context)
    pdf_bytes = _render_invoice_pdf_bytes(request, voucher, company)
    filename = f"Invoice_{voucher.number}.pdf"

    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=_invoice_from_email(company),
        to=[recipient_email],
        reply_to=_invoice_reply_to(company),
    )
    email.attach(filename, pdf_bytes, "application/pdf")
    email.send(fail_silently=False)


def _quality_task_reference(company, issue, start_date, end_date):
    period = f"{start_date or 'all'}:{end_date or 'all'}"
    return f"VQ:{company.pk}:{period}:{issue.code}:{issue.voucher_id}"


def _create_quality_tasks(company, user, report, start_date, end_date):
    created_count = 0
    existing_count = 0
    today = timezone.localdate()

    for issue in report["issues"]:
        due_days = 2 if issue.severity == "critical" else 7
        reference = _quality_task_reference(company, issue, start_date, end_date)
        _, created = PracticeTask.objects.get_or_create(
            company=company,
            reference=reference,
            defaults={
                "title": f"{issue.title}: {issue.voucher.number}",
                "task_type": issue.task_type,
                "priority": issue.priority,
                "due_date": today + timedelta(days=due_days),
                "period_start": start_date,
                "period_end": end_date,
                "created_by": user,
                "description": (
                    f"{issue.message}\n"
                    f"Voucher: {issue.voucher.number}\n"
                    f"Party: {issue.party_name or 'Not identified'}\n"
                    f"Amount: {issue.amount:.2f}"
                ),
            },
        )
        if created:
            created_count += 1
        else:
            existing_count += 1

    return created_count, existing_count


from .suggestion_engine import get_suggestions

@login_required
def voucher_suggestion_api(request):
    """
    API endpoint for smart voucher suggestions.
    Query param: 'q' (e.g. 'rent 5000')
    """
    query = request.GET.get('q', '').strip()
    company = request.current_company
    
    suggestions = get_suggestions(company, query)
    return JsonResponse({'suggestions': suggestions})

@login_required
def voucher_list(request):
    company  = request.current_company
    filters, filter_qs = _parse_filters(request)

    qs = (
        Voucher.objects.filter(company=company)
        .prefetch_related("items__ledger")
        .order_by("-date", "-created_at")
    )
    qs = _apply_filters(qs, filters)

    paginator = Paginator(qs, PAGE_SIZE)
    page_obj  = paginator.get_page(request.GET.get("page"))

    # Check if any filter is actually active (for the "clear filters" link)
    has_filters = any(filters.values())

    return render(request, "vouchers/voucher_list.html", {
        "page_obj":      page_obj,
        "filters":       filters,
        "filter_qs":     filter_qs,
        "has_filters":   has_filters,
        "total_count":   qs.count(),
        "voucher_types": Voucher.VOUCHER_TYPE_CHOICES,
    })


@login_required
def voucher_quality(request):
    company = request.current_company
    filters, start_date, end_date = _parse_quality_filters(request)
    can_create_tasks = _can_write_current_company(request)

    report = build_voucher_quality_report(
        company,
        start_date=start_date,
        end_date=end_date,
        status=filters["status"],
        voucher_type=filters["voucher_type"],
        q=filters["q"],
    )

    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="voucher_quality_exceptions.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "Voucher",
            "Date",
            "Type",
            "Severity",
            "Issue Code",
            "Issue",
            "Message",
            "Party",
            "Amount",
            "Task Type",
            "Priority",
        ])
        for issue in report["issues"]:
            writer.writerow([
                issue.voucher.number,
                issue.voucher.date.isoformat(),
                issue.voucher.voucher_type,
                issue.severity,
                issue.code,
                issue.title,
                issue.message,
                issue.party_name,
                f"{issue.amount:.2f}",
                issue.task_type,
                issue.priority,
            ])
        return response

    if request.method == "POST" and request.POST.get("action") == "create_tasks":
        if not can_create_tasks:
            messages.error(request, "Permission denied. Your role cannot create practice tasks.")
            return redirect("vouchers:quality")

        created_count, existing_count = _create_quality_tasks(
            company,
            request.user,
            report,
            start_date,
            end_date,
        )
        messages.success(
            request,
            f"Created {created_count} voucher quality task(s). {existing_count} already existed.",
        )
        redirect_filters = {key: value for key, value in filters.items() if value}
        return redirect(f"{request.path}?{urlencode(redirect_filters)}")

    return render(
        request,
        "vouchers/voucher_quality.html",
        {
            "report": report,
            "filters": filters,
            "voucher_types": Voucher.VOUCHER_TYPE_CHOICES,
            "status_choices": [("all", "All statuses"), ("open", "Not approved")] + Voucher.STATUS_CHOICES,
            "can_create_tasks": can_create_tasks,
            "export_query": urlencode({**filters, "export": "csv"}),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@write_required
def create_voucher(request):
    """
    Phase 2+4 Integration: 
    Simplified Voucher Creation with Atomic GST and Balance Validation.
    """
    company = request.current_company
    if request.method == "POST":
        form = VoucherForm(request.POST)
        formset = _make_formset(company, request.POST)
        
        if form.is_valid() and formset.is_valid():
            try:
                with transaction.atomic():
                    # 1. Save Voucher
                    voucher = form.save(commit=False)
                    voucher.company = company
                    voucher.save()

                    # 2. Save Items
                    formset.instance = voucher
                    formset.save()

                    # 3. GST Logic (Phase 4)
                    voucher.create_tax_lines()
                    voucher.check_tds_deduction()

                    # 4. Inventory Logic (Step 3)
                    voucher.sync_inventory()

                    # 5. Balance Validation (Phase 2)
                    voucher.clean()
                    if voucher.voucher_type in ["Sales", "Purchase"]:
                        voucher.sync_outstanding()

                messages.success(request, f"Voucher {voucher.number} created successfully.")
                return redirect("vouchers:list")
            except Exception as e:
                messages.error(request, f"Database Error: {str(e)}")
        else:
            messages.error(request, "Voucher could not be saved. Please check for errors in the lines below.")
    else:
        form = VoucherForm()
        formset = _make_formset(company)
    
    return render(request, "vouchers/create_voucher.html", {
        "form": form,
        "formset": formset,
        "title": "New Voucher (Quick)"
    })


@login_required
@write_required
def voucher_create(request):
    """
    Unified Voucher Creation:
    Handles both standard accounting lines and separate inventory lines.
    Calculates GST and Syncs Inventory via model methods.
    """
    company = request.current_company

    if request.method == "POST":
        form          = VoucherForm(request.POST)
        formset       = _make_formset(company, request.POST)
        stock_formset = _make_stock_formset(company, request.POST)

        if form.is_valid() and formset.is_valid() and stock_formset.is_valid():
            try:
                with transaction.atomic():
                    # 1. Save Header
                    voucher = form.save(commit=False)
                    voucher.company = company
                    voucher.save()

                    # 2. Save Child Lines
                    formset.instance = voucher
                    formset.save()
                    stock_formset.instance = voucher
                    stock_formset.save()

                    # 3. Automated Logic (Model Level)
                    voucher.create_tax_lines()
                    voucher.check_tds_deduction()
                    voucher.sync_inventory()
                    voucher.clean()
                    if voucher.voucher_type in ["Sales", "Purchase"]:
                        voucher.sync_outstanding()

                    # 4. Session Defaults for UX
                    request.session['last_narration'] = voucher.narration
                    first_item = voucher.items.first()
                    if first_item:
                        request.session['last_ledger_id'] = first_item.ledger_id

                messages.success(request, f"Voucher {voucher.number} created successfully.")
                return redirect("vouchers:list")
            except Exception as e:
                messages.error(request, f"Database Error: {str(e)}")
        else:
            # Explicitly notify about validation failures
            messages.error(request, "Voucher could not be saved. Please check the red remarks in the form below.")
    else:
        # Pre-fill from session
        initial_voucher = {}
        last_narration = request.session.get('last_narration')
        if last_narration:
            initial_voucher['narration'] = last_narration

        requested_type = request.GET.get("voucher_type", "").strip()
        allowed_types = {code for code, _label in Voucher.VOUCHER_TYPE_CHOICES}
        if requested_type in allowed_types:
            initial_voucher["voucher_type"] = requested_type

        form = VoucherForm(initial=initial_voucher)

        last_ledger_id = request.session.get('last_ledger_id')
        formset_kwargs = {}
        if last_ledger_id:
            formset_kwargs['initial'] = [{'ledger': last_ledger_id}]

        formset = _make_formset(company, **formset_kwargs)
        stock_formset = _make_stock_formset(company)

    return render(request, "vouchers/voucher_form.html", {
        "form":          form,
        "formset":       formset,
        "stock_formset": stock_formset,
        "title":         "New Voucher",
    })


@login_required
@write_required
def voucher_edit(request, pk):
    """
    Unified Voucher Edit:
    Ensures consistency between accounting and inventory on updates.
    """
    company = request.current_company
    voucher = get_object_or_404(Voucher, pk=pk, company=company)

    if voucher.status == "APPROVED":
        messages.error(
            request,
            "Approved vouchers are hard locked. Unapprove this voucher before editing it."
        )
        return redirect("vouchers:detail", pk=voucher.pk)

    if request.method == "POST":
        form          = VoucherForm(request.POST, instance=voucher)
        formset       = _make_formset(company, request.POST, instance=voucher)
        stock_formset = _make_stock_formset(company, request.POST, instance=voucher)

        if form.is_valid() and formset.is_valid() and stock_formset.is_valid():
            try:
                with transaction.atomic():
                    # 1. Update Lines
                    form.save()
                    formset.save()
                    stock_formset.save()

                    # 2. Re-trigger calculations
                    voucher.create_tax_lines()
                    voucher.check_tds_deduction()
                    voucher.sync_inventory()
                    voucher.clean()
                    if voucher.voucher_type in ["Sales", "Purchase"]:
                        voucher.sync_outstanding()

                messages.success(request, f"Voucher {voucher.number} updated successfully.")
                return redirect("vouchers:list")
            except Exception as e:
                messages.error(request, f"Update failed: {str(e)}")
        else:
            messages.error(request, "Voucher could not be updated. Please check the errors below.")
    else:
        form          = VoucherForm(instance=voucher)
        formset       = _make_formset(company, instance=voucher)
        stock_formset = _make_stock_formset(company, instance=voucher)

    return render(request, "vouchers/voucher_form.html", {
        "form":          form,
        "formset":       formset,
        "stock_formset": stock_formset,
        "title":         "Edit Voucher",
        "voucher":       voucher,
    })

@login_required
def voucher_detail(request, pk):
    company = request.current_company
    # Strict isolation: filter by company in get_object_or_404
    voucher = get_object_or_404(
        Voucher.objects.filter(company=company).prefetch_related(
            "items__ledger",
            "voucher_stock_items__stock_item",
        ),
        pk=pk
    )

    # Track as recent item
    from core.utils.search_utils import add_recent_item
    from django.urls import reverse
    add_recent_item(request, 'vouchers', voucher.id, voucher.number, reverse('vouchers:detail', args=[voucher.id]))

    audit_logs = AuditLog.objects.filter(
        company=company, model_name="Voucher", record_id=pk
    ).select_related("user")[:20]

    # Generate UPI QR only for Sales vouchers where a UPI ID is configured
    qr_code = None
    invoice_party = None
    if voucher.voucher_type == "Sales":
        qr_code = generate_upi_qr(voucher)
        invoice_party = _sales_party_ledger(voucher)
    gst_execution = build_gst_voucher_execution_context(voucher) if voucher.voucher_type == "Sales" else None
    gst_integration_logs = voucher.integration_logs.select_related("requested_by")[:5]

    ctx = {
        "voucher":    voucher,
        "audit_logs": audit_logs,
        "qr_code":    qr_code,
        "today":      _date.today(),
        "invoice_party": invoice_party,
        "invoice_recipient_email": (invoice_party.email or "") if invoice_party else "",
        "gst_execution": gst_execution,
        "gst_integration_logs": gst_integration_logs,
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return render(request, "vouchers/partials/voucher_detail_content.html", ctx)

    return render(request, "vouchers/voucher_detail.html", ctx)


@login_required
@write_required
@require_POST
def send_invoice_email(request, pk):
    company = request.current_company
    voucher = get_object_or_404(
        Voucher.objects.filter(company=company).prefetch_related("items__ledger", "items__ledger__account_group"),
        pk=pk,
    )

    if voucher.voucher_type != "Sales":
        messages.error(request, "Invoice email is available only for Sales vouchers.")
        return redirect("vouchers:detail", pk=voucher.pk)

    party_ledger = _sales_party_ledger(voucher)
    if not party_ledger:
        messages.error(request, "Could not identify the client ledger for this Sales voucher.")
        return redirect("vouchers:detail", pk=voucher.pk)

    recipient_email = (request.POST.get("recipient_email") or party_ledger.email or "").strip()
    if not recipient_email:
        messages.error(request, "Add the client's email address before sending the invoice.")
        return redirect("vouchers:detail", pk=voucher.pk)

    try:
        validate_email(recipient_email)
    except ValidationError:
        messages.error(request, "Enter a valid client email address.")
        return redirect("vouchers:detail", pk=voucher.pk)

    if party_ledger.email != recipient_email:
        party_ledger.email = recipient_email
        party_ledger.save(update_fields=["email", "updated_at"])

    try:
        _send_invoice_email(request, voucher, recipient_email, party_ledger)
    except Exception as exc:
        messages.error(request, f"Invoice email could not be sent: {exc}")
        return redirect("vouchers:detail", pk=voucher.pk)

    AuditLog.objects.create(
        company=company,
        user=request.user,
        action=AuditLog.ACTION_UPDATE,
        model_name="Voucher",
        record_id=voucher.pk,
        object_repr=str(voucher),
        old_data={},
        new_data={
            "invoice_email_sent_to": recipient_email,
            "client_ledger": party_ledger.name,
        },
    )
    messages.success(request, f"Invoice {voucher.number} emailed to {recipient_email}.")
    return redirect("vouchers:detail", pk=voucher.pk)


@login_required
@write_required
@require_POST
def send_payment_reminder(request, pk):
    company = request.current_company
    voucher = get_object_or_404(
        Voucher.objects.filter(company=company).prefetch_related("items__ledger", "items__ledger__account_group"),
        pk=pk,
    )

    redirect_url = request.POST.get("next") or request.GET.get("next")
    if redirect_url and not url_has_allowed_host_and_scheme(redirect_url, allowed_hosts={request.get_host()}):
        redirect_url = None
    redirect_target = redirect_url or "vouchers:outstanding"

    if voucher.voucher_type != "Sales":
        messages.error(request, "Payment reminders are available only for Sales invoices.")
        return redirect(redirect_target)

    if (voucher.outstanding_amount or Decimal("0.00")) <= Decimal("0.00"):
        messages.info(request, f"Invoice {voucher.number} is already settled.")
        return redirect(redirect_target)

    party_ledger = _sales_party_ledger(voucher)
    if not party_ledger:
        messages.error(request, "Could not identify the client ledger for this Sales invoice.")
        return redirect(redirect_target)

    recipient_email = (request.POST.get("recipient_email") or party_ledger.email or "").strip()
    if not recipient_email:
        messages.error(request, "Add the client's email address before sending a payment reminder.")
        return redirect(redirect_target)

    try:
        validate_email(recipient_email)
    except ValidationError:
        messages.error(request, "Enter a valid client email address.")
        return redirect(redirect_target)

    if party_ledger.email != recipient_email:
        party_ledger.email = recipient_email
        party_ledger.save(update_fields=["email", "updated_at"])

    try:
        _send_payment_reminder_email(request, voucher, recipient_email, party_ledger)
    except Exception as exc:
        messages.error(request, f"Payment reminder could not be sent: {exc}")
        return redirect(redirect_target)

    AuditLog.objects.create(
        company=company,
        user=request.user,
        action=AuditLog.ACTION_UPDATE,
        model_name="Voucher",
        record_id=voucher.pk,
        object_repr=str(voucher),
        old_data={},
        new_data={
            "payment_reminder_sent_to": recipient_email,
            "client_ledger": party_ledger.name,
            "outstanding_amount": str(voucher.outstanding_amount or Decimal("0.00")),
            "due_date": voucher.due_date.isoformat() if voucher.due_date else None,
        },
    )
    messages.success(request, f"Payment reminder for invoice {voucher.number} sent to {recipient_email}.")
    return redirect(redirect_target)


@login_required
@write_required
@require_POST
def create_collection_tasks(request):
    company = request.current_company
    today = timezone.localdate()
    redirect_url = request.POST.get("next") or request.GET.get("next")
    if redirect_url and not url_has_allowed_host_and_scheme(redirect_url, allowed_hosts={request.get_host()}):
        redirect_url = None

    vouchers = (
        Voucher.objects.filter(
            company=company,
            voucher_type="Sales",
            status="APPROVED",
            outstanding_amount__gt=0,
            due_date__lt=today,
        )
        .prefetch_related("items__ledger__account_group")
        .order_by("due_date", "date", "id")
    )

    created_count = 0
    existing_count = 0
    for voucher in vouchers:
        party_ledger = _sales_party_ledger(voucher)
        days_overdue = (today - voucher.due_date).days if voucher.due_date else 0
        priority = PracticeTask.PRIORITY_CRITICAL if days_overdue > 90 else PracticeTask.PRIORITY_HIGH
        reference = f"COLLECT:{company.pk}:{voucher.pk}"
        _, created = PracticeTask.objects.get_or_create(
            company=company,
            reference=reference,
            defaults={
                "title": f"Collect overdue invoice {voucher.number or voucher.pk}",
                "task_type": PracticeTask.TYPE_OTHER,
                "priority": priority,
                "due_date": today + timedelta(days=1),
                "created_by": request.user,
                "description": (
                    f"Invoice {voucher.number or voucher.pk} is {days_overdue} day(s) overdue.\n"
                    f"Client: {party_ledger.name if party_ledger else 'Not identified'}\n"
                    f"Outstanding: Rs.{voucher.outstanding_amount:.2f}\n"
                    "Use the outstanding statement to send a reminder or record collection notes."
                ),
            },
        )
        if created:
            created_count += 1
        else:
            existing_count += 1

    if created_count:
        messages.success(request, f"Created {created_count} collection follow-up task(s). {existing_count} already existed.")
    else:
        messages.info(request, "No new collection tasks were needed for overdue sales invoices.")
    return redirect(redirect_url or "vouchers:outstanding")


@login_required
@admin_required
@require_POST
def voucher_unapprove(request, pk):
    company = request.current_company
    voucher = get_object_or_404(Voucher, pk=pk, company=company)

    if voucher.status != "APPROVED":
        messages.info(request, f"Voucher {voucher.number} is not approved.")
        return redirect("vouchers:detail", pk=voucher.pk)

    old_data = {
        "status": voucher.status,
        "verified_by": voucher.verified_by_id,
        "verified_at": voucher.verified_at.isoformat() if voucher.verified_at else None,
    }
    voucher.unapprove(request.user)
    AuditLog.objects.create(
        company=company,
        user=request.user,
        action=AuditLog.ACTION_UPDATE,
        model_name="Voucher",
        record_id=voucher.pk,
        object_repr=str(voucher),
        old_data=old_data,
        new_data={"status": voucher.status},
    )
    messages.success(request, f"Voucher {voucher.number} has been unapproved and can now be edited.")
    return redirect("vouchers:detail", pk=voucher.pk)


@login_required
@admin_required
def voucher_delete(request, pk):
    company = request.current_company
    voucher = get_object_or_404(Voucher, pk=pk, company=company)

    if voucher.status == "APPROVED":
        messages.error(request, "Approved vouchers are hard locked. Unapprove before deleting.")
        return redirect("vouchers:detail", pk=voucher.pk)

    if request.method == "POST":
        num = voucher.number
        affected_stock_items = set(voucher.stock_movements.values_list("stock_item_id", flat=True))
        with transaction.atomic():
            for movement in voucher.stock_movements.select_related("batch"):
                if movement.batch:
                    movement.batch.quantity -= movement.quantity
                    movement.batch.save(update_fields=["quantity"])
            voucher.delete()
            if affected_stock_items:
                from inventory.valuation_utils import rebuild_valuation_for_items
                rebuild_valuation_for_items(affected_stock_items)
        messages.success(request, f"Voucher {num} deleted.")
        return redirect("vouchers:list")

    return render(request, "vouchers/voucher_confirm_delete.html", {"voucher": voucher})


# ─────────────────────────────────────────────────────────────────────────────
# BULK ACTIONS
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def bulk_action(request):
    """
    Handles two bulk actions posted from the voucher list:
      action=delete      → Admin only, deletes the selected vouchers.
      action=export_pdf  → All roles, renders a print-optimised HTML page.
    """
    if request.method != "POST":
        return redirect("vouchers:list")

    company      = request.current_company
    action       = request.POST.get("action", "")
    selected_ids = request.POST.getlist("selected_ids")

    if not selected_ids:
        messages.warning(request, "No vouchers were selected.")
        return redirect("vouchers:list")

    # Scope to company for security — never trust client-supplied PKs alone
    vouchers = Voucher.objects.filter(pk__in=selected_ids, company=company)

    # ── Bulk Delete (Admin only) ──────────────────────────────────────────────
    if action == "delete":
        role = getattr(request, "current_company_role", None)
        if role != "Admin":
            messages.error(
                request,
                "Permission denied. Only Admins can bulk-delete vouchers."
            )
            return redirect("vouchers:list")

        count = vouchers.count()
        if count == 0:
            messages.warning(request, "No matching vouchers found.")
            return redirect("vouchers:list")
        if vouchers.filter(status="APPROVED").exists():
            messages.error(
                request,
                "Approved vouchers are hard locked. Unapprove selected vouchers before deleting."
            )
            return redirect("vouchers:list")

        with transaction.atomic():
            affected_stock_items = set()
            for voucher in vouchers.prefetch_related("stock_movements__batch"):
                for movement in voucher.stock_movements.all():
                    affected_stock_items.add(movement.stock_item_id)
                    if movement.batch:
                        movement.batch.quantity -= movement.quantity
                        movement.batch.save(update_fields=["quantity"])
            vouchers.delete()
            if affected_stock_items:
                from inventory.valuation_utils import rebuild_valuation_for_items
                rebuild_valuation_for_items(affected_stock_items)

        messages.success(request, f"{count} voucher(s) deleted successfully.")
        return redirect("vouchers:list")

    # ── Bulk Export to PDF (all roles) ────────────────────────────────────────
    if action == "export_pdf":
        vouchers = (
            vouchers
            .prefetch_related("items__ledger")
            .order_by("date", "number")
        )
        return render(request, "vouchers/bulk_print.html", {
            "vouchers":       vouchers,
            "exported_count": vouchers.count(),
        })

    messages.warning(request, "Unknown bulk action.")
    return redirect("vouchers:list")


# ─────────────────────────────────────────────────────────────────────────────
# SIMULATE PAYMENT
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@write_required
def simulate_payment(request, pk):
    """
    POST-only view that auto-creates a balanced Receipt voucher to simulate
    payment received against a Sales voucher.

    Logic:
      1. Load the Sales Voucher (must belong to this company).
      2. Collect all debit items (the Debtor / customer receivable accounts).
      3. Find a Bank or Cash ledger in the company (Asset group, name contains
         "bank" or "cash").
      4. Create a Receipt voucher inside an atomic transaction:
            Dr  Bank / Cash        ← total amount received
            Cr  Debtor ledger(s)   ← clearing the receivable
      5. Log the action in AuditLog.
      6. Redirect back to the Sales Voucher detail with a success message.
    """
    if request.method != "POST":
        return redirect("vouchers:detail", pk=pk)

    company = request.current_company

    # ── 1. Load the Sales Voucher ─────────────────────────────────────────────
    sales_voucher = get_object_or_404(
        Voucher.objects.prefetch_related("items__ledger"),
        pk=pk,
        company=company,
        voucher_type="Sales",
    )

    # ── 2. Collect debit items (Debtor / receivable side) ─────────────────────
    debit_items = [item for item in sales_voucher.items.all() if item.entry_type == 'DR']
    if not debit_items:
        messages.error(
            request,
            "Cannot simulate payment: the Sales Voucher has no debit (receivable) entries."
        )
        return redirect("vouchers:detail", pk=pk)

    total_amount = sum(item.amount for item in debit_items)

    # ── 3. Find Bank / Cash ledger ────────────────────────────────────────────
    from ledger.models import Ledger

    bank_ledger = (
        Ledger.objects.filter(company=company, account_group__nature="Asset", is_active=True)
        .filter(
            Q(name__icontains="bank") | Q(name__icontains="cash")
        )
        .first()
    )
    if not bank_ledger:
        messages.error(
            request,
            "No active Bank or Cash ledger found for this company. "
            "Please create one (nature = Asset, name containing 'Bank' or 'Cash') first."
        )
        return redirect("vouchers:detail", pk=pk)

    # ── 4. Create the Receipt Voucher ─────────────────────────────────────────
    with transaction.atomic():
        receipt = Voucher(
            company=company,
            date=_date.today(),
            voucher_type="Receipt",
            narration=f"Payment received against Sales Voucher {sales_voucher.number}",
        )
        receipt.save()  # triggers number generation

        # Dr: Bank / Cash — money received
        VoucherItem.objects.create(
            voucher=receipt,
            ledger=bank_ledger,
            entry_type='DR',
            amount=total_amount,
        )

        # Cr: each Debtor ledger from the original — clearing the receivable
        for item in debit_items:
            VoucherItem.objects.create(
                voucher=receipt,
                ledger=item.ledger,
                entry_type='CR',
                amount=item.amount,
                reference_voucher=sales_voucher,
            )

        receipt.validate_balance()
        sales_voucher.sync_outstanding()

    # ── 5. Redirect with success ──────────────────────────────────────────────
    messages.success(
        request,
        f"Payment Simulated! Receipt Voucher {receipt.number} created automatically."
    )
    return redirect("vouchers:detail", pk=pk)


# ─────────────────────────────────────────────────────────────────────────────
# OUTSTANDING STATEMENT (Bill-to-Bill)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def outstanding_statement(request):
    """
    Shows every Sales and Purchase invoice for the company with:
      - Total invoice amount
      - Amount settled so far (via reference_voucher links)
      - Outstanding balance remaining
      - Days overdue (if due_date is set)

    The user can filter by type (Sales / Purchase / All) and by status
    (Outstanding / Settled / All).
    """
    company   = request.current_company
    today     = _date.today()
    type_filter   = request.GET.get("type", "Sales")
    status_filter = request.GET.get("status", "outstanding")

    # Base queryset: Sales and/or Purchase vouchers
    allowed_types = ["Sales", "Purchase"]
    if type_filter in allowed_types:
        qs = Voucher.objects.filter(company=company, voucher_type=type_filter)
    else:
        qs = Voucher.objects.filter(company=company, voucher_type__in=allowed_types)

    qs = qs.prefetch_related("items__ledger__account_group", "settlements").order_by("date", "number")

    # Annotate with financial data
    rows = []

    for v in qs:
        invoice_total = v.total_amount()
        settled       = v.amount_settled()
        outstanding   = v.outstanding_amount
        fully_settled = outstanding == Decimal("0.00")
        party_ledger = _outstanding_party_ledger(v)

        # Days overdue: positive = overdue, negative = still within term
        days_overdue = None
        days_to_due = None
        if v.due_date:
            days_overdue = (today - v.due_date).days
            days_to_due = (v.due_date - today).days

        if fully_settled:
            collection_status = "settled"
            collection_label = "Settled"
        elif days_overdue is None:
            collection_status = "no_due_date"
            collection_label = "No due date"
        elif days_overdue > 30:
            collection_status = "critical"
            collection_label = "Critical"
        elif days_overdue > 0:
            collection_status = "overdue"
            collection_label = "Overdue"
        elif days_to_due is not None and days_to_due <= 7:
            collection_status = "due_soon"
            collection_label = "Due soon"
        else:
            collection_status = "current"
            collection_label = "Current"

        rows.append({
            "voucher":       v,
            "party_ledger":  party_ledger,
            "party_name":    party_ledger.name if party_ledger else "",
            "party_email":   (party_ledger.email or "") if party_ledger else "",
            "party_gstin":   (party_ledger.gstin or "") if party_ledger else "",
            "invoice_total": invoice_total,
            "settled":       settled,
            "outstanding":   outstanding,
            "fully_settled": fully_settled,
            "days_overdue":  days_overdue,
            "days_to_due":   days_to_due,
            "is_overdue":    (days_overdue is not None and days_overdue > 0 and not fully_settled),
            "collection_status": collection_status,
            "collection_label":  collection_label,
        })

    # Filter by status after annotation
    if status_filter == "outstanding":
        rows = [r for r in rows if not r["fully_settled"]]
    elif status_filter == "settled":
        rows = [r for r in rows if r["fully_settled"]]

    total_invoiced = sum((r["invoice_total"] for r in rows), Decimal("0.00"))
    total_settled = sum((r["settled"] for r in rows), Decimal("0.00"))
    total_outstanding = sum((r["outstanding"] for r in rows), Decimal("0.00"))
    overdue_rows = [r for r in rows if r["is_overdue"]]
    due_soon_rows = [r for r in rows if r["collection_status"] == "due_soon"]
    overdue_amount = sum((r["outstanding"] for r in overdue_rows), Decimal("0.00"))
    due_soon_amount = sum((r["outstanding"] for r in due_soon_rows), Decimal("0.00"))

    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="Outstanding_{company.name}_{type_filter}_{status_filter}_{today:%Y%m%d}.csv"'
            .replace(" ", "_")
        )
        writer = csv.writer(response)
        writer.writerow([
            "Invoice No.",
            "Type",
            "Party",
            "Email",
            "GSTIN",
            "Invoice Date",
            "Due Date",
            "Status",
            "Days Overdue",
            "Invoice Amount",
            "Settled",
            "Outstanding",
        ])
        for row in rows:
            writer.writerow([
                row["voucher"].number,
                row["voucher"].voucher_type,
                row["party_name"],
                row["party_email"],
                row["party_gstin"],
                row["voucher"].date.isoformat(),
                row["voucher"].due_date.isoformat() if row["voucher"].due_date else "",
                row["collection_label"],
                row["days_overdue"] if row["days_overdue"] is not None else "",
                f"{row['invoice_total']:.2f}",
                f"{row['settled']:.2f}",
                f"{row['outstanding']:.2f}",
            ])
        return response

    return render(request, "vouchers/outstanding_statement.html", {
        "rows":              rows,
        "total_invoiced":    total_invoiced,
        "total_settled":     total_settled,
        "total_outstanding": total_outstanding,
        "overdue_count":     len(overdue_rows),
        "overdue_amount":    overdue_amount,
        "due_soon_count":    len(due_soon_rows),
        "due_soon_amount":   due_soon_amount,
        "type_filter":       type_filter,
        "status_filter":     status_filter,
        "today":             today,
    })


# ─────────────────────────────────────────────────────────────────────────────
# SERVER-SIDE PDF INVOICE (WeasyPrint)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def invoice_pdf(request, pk):
    """
    Generate a professional, server-side PDF invoice for any voucher using
    WeasyPrint. Produces byte-identical output on every device — no browser
    print-to-PDF variance.

    Query parameter:
        ?download=1  → Content-Disposition: attachment (forces download)
        (default)    → inline (browser renders PDF viewer)
    """
    from django.http import HttpResponse

    company = request.current_company
    voucher = get_object_or_404(
        Voucher.objects.prefetch_related("items__ledger"), pk=pk, company=company
    )
    pdf_bytes = _render_invoice_pdf_bytes(request, voucher, company)

    disposition = "attachment" if request.GET.get("download") else "inline"
    filename    = f"Invoice_{voucher.number}.pdf"

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return response
