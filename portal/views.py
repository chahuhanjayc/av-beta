import csv
import hashlib
from urllib.parse import quote

from django.conf import settings
from django.core.mail import EmailMessage
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from .models import PortalUser, BalanceConfirmation, ClientDocumentRequest
from vouchers.models import VoucherItem
from decimal import Decimal
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.http import require_POST
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
import weasyprint
import datetime
from datetime import timedelta
from .utils import send_ledger_email
from core.decorators import write_required
from core.upload_validation import DOCUMENT_EXTENSIONS, validate_uploaded_file
from core.models import AuditLog, Company, PracticeTask, UserCompanyAccess
from core.phone import normalize_phone_number
from ocr.models import OCRSubmission
from .forms import (
    CLIENT_REQUEST_TEMPLATES,
    ClientDocumentRequestForm,
    ClientRequestCampaignForm,
    initial_for_template,
)


PORTAL_LOGIN_FAILURES_KEY = "portal_login_failures"
PORTAL_LOGIN_LOCKED_UNTIL_KEY = "portal_login_locked_until"
PORTAL_LOGIN_MAX_FAILURES = 5
PORTAL_LOGIN_LOCKOUT_MINUTES = 15


def _companies_for_user(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return (
        Company.objects.filter(user_access__user=user)
        .distinct()
        .order_by("name")
    )


def _can_manage_company(user, company):
    if user.is_superuser:
        return True
    return UserCompanyAccess.objects.filter(
        user=user,
        company=company,
        role__in=["Admin", "Accountant"],
    ).exists()


def _manageable_companies_for_user(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return (
        Company.objects.filter(
            user_access__user=user,
            user_access__role__in=["Admin", "Accountant"],
        )
        .distinct()
        .order_by("name")
    )


def _export_query(request):
    query = request.GET.copy()
    query["export"] = "csv"
    return query.urlencode()


def _portal_login_is_locked(request):
    locked_until_raw = request.session.get(PORTAL_LOGIN_LOCKED_UNTIL_KEY)
    if not locked_until_raw:
        return False

    locked_until = parse_datetime(locked_until_raw)
    if not locked_until:
        request.session.pop(PORTAL_LOGIN_LOCKED_UNTIL_KEY, None)
        return False

    if timezone.is_naive(locked_until):
        locked_until = timezone.make_aware(locked_until)

    if locked_until > timezone.now():
        messages.error(request, "Too many failed login attempts. Try again later.")
        return True

    request.session.pop(PORTAL_LOGIN_LOCKED_UNTIL_KEY, None)
    request.session.pop(PORTAL_LOGIN_FAILURES_KEY, None)
    return False


def _record_portal_login_failure(request):
    failures = int(request.session.get(PORTAL_LOGIN_FAILURES_KEY, 0)) + 1
    request.session[PORTAL_LOGIN_FAILURES_KEY] = failures

    if failures >= PORTAL_LOGIN_MAX_FAILURES:
        locked_until = timezone.now() + timedelta(minutes=PORTAL_LOGIN_LOCKOUT_MINUTES)
        request.session[PORTAL_LOGIN_LOCKED_UNTIL_KEY] = locked_until.isoformat()


def _clear_portal_login_failures(request):
    request.session.pop(PORTAL_LOGIN_FAILURES_KEY, None)
    request.session.pop(PORTAL_LOGIN_LOCKED_UNTIL_KEY, None)


def portal_login(request):
    if request.method == "POST":
        if _portal_login_is_locked(request):
            return render(request, "portal/login.html")

        email = request.POST.get("email")
        password = request.POST.get("password")
        try:
            user = PortalUser.objects.get(email=email, is_active=True)
            if user.check_password(password):
                request.session.cycle_key()
                _clear_portal_login_failures(request)
                request.session['portal_user_id'] = user.id
                return redirect("portal:dashboard")
            else:
                _record_portal_login_failure(request)
                messages.error(request, "Invalid email or password.")
        except PortalUser.DoesNotExist:
            _record_portal_login_failure(request)
            messages.error(request, "Invalid email or password.")
    
    return render(request, "portal/login.html")

@require_POST
def portal_logout(request):
    if 'portal_user_id' in request.session:
        del request.session['portal_user_id']
    return redirect("portal:login")

def _get_ledger_data(ledger):
    """Helper to fetch transactions and calculate balance for a ledger."""
    items = (
        VoucherItem.objects.filter(
            ledger=ledger,
            voucher__company=ledger.company,
            voucher__status='APPROVED',
        )
        .select_related('voucher')
        .order_by('voucher__date', 'id')
    )
    transactions = []
    running_balance = Decimal("0.00")
    for item in items:
        if item.entry_type == 'DR':
            running_balance += item.amount
        else:
            running_balance -= item.amount
        transactions.append({
            'date': item.voucher.date,
            'number': item.voucher.number,
            'type': item.entry_type,
            'amount': item.amount,
            'balance': running_balance
        })
    return transactions, running_balance


def _build_portal_dashboard_context(user, *, is_staff_view=False):
    ledger = user.linked_ledger
    transactions, running_balance = _get_ledger_data(ledger)
    today = timezone.localdate()

    requests = ClientDocumentRequest.objects.filter(portal_user=user).select_related(
        "company",
        "uploaded_submission",
        "related_task",
    )
    open_requests_qs = requests.filter(status=ClientDocumentRequest.STATUS_OPEN)
    uploaded_requests_qs = requests.filter(status=ClientDocumentRequest.STATUS_UPLOADED)
    closed_requests_qs = requests.filter(status=ClientDocumentRequest.STATUS_CLOSED)

    open_count = open_requests_qs.count()
    uploaded_count = uploaded_requests_qs.count()
    closed_count = closed_requests_qs.count()
    overdue_count = open_requests_qs.filter(due_date__lt=today).count()
    due_soon_count = open_requests_qs.filter(
        due_date__gte=today,
        due_date__lte=today + timedelta(days=7),
    ).count()
    total_requests = open_count + uploaded_count + closed_count
    delivered_requests = uploaded_count + closed_count
    completion_score = 100 if total_requests == 0 else round((delivered_requests / total_requests) * 100)
    completion_score = max(0, completion_score - min(40, overdue_count * 15))

    next_due_request = (
        open_requests_qs.filter(due_date__isnull=False).order_by("due_date", "-created_at").first()
        or open_requests_qs.order_by("-created_at").first()
    )
    last_confirmation = BalanceConfirmation.objects.filter(portal_user=user).first()

    portal_actions = []
    if overdue_count:
        portal_actions.append({
            "tone": "danger",
            "icon": "bi-exclamation-octagon",
            "title": "Overdue documents",
            "detail": f"{overdue_count} request(s) need upload now.",
        })
    elif open_count:
        portal_actions.append({
            "tone": "warning",
            "icon": "bi-cloud-upload",
            "title": "Documents to upload",
            "detail": f"{open_count} open request(s) are waiting for you.",
        })
    if uploaded_count:
        portal_actions.append({
            "tone": "success",
            "icon": "bi-hourglass-split",
            "title": "Under CA review",
            "detail": f"{uploaded_count} uploaded document(s) are with the CA team.",
        })
    if not last_confirmation:
        portal_actions.append({
            "tone": "primary",
            "icon": "bi-patch-check",
            "title": "Confirm ledger balance",
            "detail": "Review the statement and confirm or dispute the balance.",
        })
    elif last_confirmation.response_status == BalanceConfirmation.STATUS_DISPUTED:
        portal_actions.append({
            "tone": "danger",
            "icon": "bi-chat-left-text",
            "title": "Balance dispute recorded",
            "detail": "Your CA team has your dispute note and statement copy.",
        })

    active_document_requests = (
        requests.filter(
            status__in=[
                ClientDocumentRequest.STATUS_OPEN,
                ClientDocumentRequest.STATUS_UPLOADED,
            ],
        )
        .order_by("status", "due_date", "-created_at")
    )
    open_document_requests = list(
        open_requests_qs.filter(due_date__isnull=False).order_by("due_date", "-created_at")[:50]
    )
    remaining_open_slots = 50 - len(open_document_requests)
    if remaining_open_slots:
        open_document_requests.extend(
            open_requests_qs.filter(due_date__isnull=True).order_by("-created_at")[:remaining_open_slots]
        )

    return {
        "user": user,
        "ledger": ledger,
        "company": ledger.company,
        "transactions": list(reversed(transactions)),
        "total_outstanding": running_balance,
        "last_confirmation": last_confirmation,
        "document_requests": active_document_requests,
        "open_document_requests": open_document_requests,
        "uploaded_document_requests": uploaded_requests_qs.order_by("-uploaded_at", "-created_at")[:25],
        "closed_document_requests": closed_requests_qs.order_by("-closed_at", "-updated_at")[:5],
        "next_due_request": next_due_request,
        "portal_actions": portal_actions,
        "request_summary": {
            "open": open_count,
            "uploaded": uploaded_count,
            "closed": closed_count,
            "overdue": overdue_count,
            "due_soon": due_soon_count,
            "total": total_requests,
            "completion_score": completion_score,
        },
        "download_url": (
            reverse("portal:ca_download_pdf", args=[user.pk])
            if is_staff_view else reverse("portal:download_pdf")
        ),
        "is_staff_view": is_staff_view,
        "today": today,
    }


def portal_dashboard(request):
    user_id = request.session.get('portal_user_id')
    if not user_id:
        return redirect("portal:login")
    
    try:
        user = PortalUser.objects.select_related('linked_ledger__company').get(id=user_id)
        return render(request, "portal/dashboard.html", _build_portal_dashboard_context(user))
    except PortalUser.DoesNotExist:
        return redirect("portal:login")


def _public_request_pack(doc_request):
    today = timezone.localdate()
    base = ClientDocumentRequest.objects.filter(company=doc_request.company).select_related(
        "company",
        "portal_user",
        "uploaded_submission",
    )
    if doc_request.portal_user_id:
        base = base.filter(portal_user=doc_request.portal_user)
    elif doc_request.recipient_email:
        base = base.filter(recipient_email__iexact=doc_request.recipient_email)
    elif doc_request.recipient_whatsapp_number:
        base = base.filter(recipient_whatsapp_number=doc_request.recipient_whatsapp_number)
    else:
        base = base.filter(pk=doc_request.pk)

    open_qs = base.filter(status=ClientDocumentRequest.STATUS_OPEN)
    uploaded_qs = base.filter(status=ClientDocumentRequest.STATUS_UPLOADED)
    closed_qs = base.filter(status=ClientDocumentRequest.STATUS_CLOSED)
    open_requests = list(
        open_qs.filter(due_date__isnull=False).order_by("due_date", "-created_at")[:25]
    )
    remaining = 25 - len(open_requests)
    if remaining:
        open_requests.extend(open_qs.filter(due_date__isnull=True).order_by("-created_at")[:remaining])

    total = base.exclude(status=ClientDocumentRequest.STATUS_CANCELLED).count()
    uploaded_count = uploaded_qs.count()
    closed_count = closed_qs.count()
    delivered = uploaded_count + closed_count
    overdue_count = open_qs.filter(due_date__lt=today).count()
    completion_score = 100 if total == 0 else round((delivered / total) * 100)
    completion_score = max(0, completion_score - min(35, overdue_count * 12))

    next_request = (
        open_qs.exclude(pk=doc_request.pk).filter(due_date__isnull=False).order_by("due_date", "-created_at").first()
        or open_qs.exclude(pk=doc_request.pk).order_by("-created_at").first()
    )
    current_position = next(
        (index for index, item in enumerate(open_requests, 1) if item.pk == doc_request.pk),
        None,
    )

    return {
        "open_requests": open_requests,
        "uploaded_requests": uploaded_qs.order_by("-uploaded_at", "-created_at")[:10],
        "next_request": next_request,
        "current_position": current_position,
        "today": today,
        "summary": {
            "total": total,
            "open": open_qs.count(),
            "uploaded": uploaded_count,
            "closed": closed_count,
            "overdue": overdue_count,
            "completion_score": completion_score,
        },
    }


def _document_request_public_context(doc_request, **extra):
    context = {
        "doc_request": doc_request,
        "request_pack": _public_request_pack(doc_request),
    }
    context.update(extra)
    return context


def client_document_request_upload(request, token):
    doc_request = get_object_or_404(
        ClientDocumentRequest.objects.select_related("company", "portal_user"),
        token=token,
    )
    if doc_request.status in {ClientDocumentRequest.STATUS_CLOSED, ClientDocumentRequest.STATUS_CANCELLED}:
        return render(request, "portal/document_request_closed.html", _document_request_public_context(doc_request))

    if request.method == "POST":
        file_obj = request.FILES.get("file")
        response_note = request.POST.get("response_note", "").strip()
        try:
            validate_uploaded_file(
                file_obj,
                allowed_extensions=DOCUMENT_EXTENSIONS,
                max_mb=20,
            )
        except Exception as exc:
            messages.error(request, str(exc))
            return render(request, "portal/document_request_upload.html", _document_request_public_context(doc_request))

        hasher = hashlib.sha256()
        for chunk in file_obj.chunks():
            hasher.update(chunk)
        file_obj.seek(0)

        submission, created = OCRSubmission.objects.get_or_create(
            company=doc_request.company,
            file_hash=hasher.hexdigest(),
            defaults={
                "source": OCRSubmission.SOURCE_WEB,
                "file": file_obj,
                "status": OCRSubmission.STATUS_PENDING,
                "parsed_json": {
                    "client_document_request_id": doc_request.pk,
                    "source_reference": doc_request.source_reference,
                    "document_type": doc_request.document_type,
                },
            },
        )
        if not created and not submission.file:
            submission.file = file_obj
            submission.save(update_fields=["file", "updated_at"])

        doc_request.uploaded_submission = submission
        doc_request.uploaded_at = timezone.now()
        doc_request.status = ClientDocumentRequest.STATUS_UPLOADED
        doc_request.response_note = response_note[:1000]
        doc_request.save(update_fields=[
            "uploaded_submission",
            "uploaded_at",
            "status",
            "response_note",
            "updated_at",
        ])

        if doc_request.related_task_id:
            task = doc_request.related_task
            task.status = "in_progress"
            task.description = (task.description + "\n\nClient uploaded document.").strip()
            task.save(update_fields=["status", "description", "updated_at"])

        messages.success(request, "Document uploaded successfully.")
        return render(request, "portal/document_request_success.html", _document_request_public_context(doc_request))

    return render(request, "portal/document_request_upload.html", _document_request_public_context(doc_request))

@require_POST
def portal_confirm_balance(request):
    user_id = request.session.get('portal_user_id')
    if not user_id:
        return redirect("portal:login")
    
    try:
        user = PortalUser.objects.select_related('linked_ledger__company').get(id=user_id)
        ledger = user.linked_ledger
        _, balance = _get_ledger_data(ledger)
        response_status = request.POST.get("response_status", BalanceConfirmation.STATUS_CONFIRMED)
        if response_status not in {BalanceConfirmation.STATUS_CONFIRMED, BalanceConfirmation.STATUS_DISPUTED}:
            response_status = BalanceConfirmation.STATUS_CONFIRMED
        remarks = request.POST.get("remarks", "").strip()
        
        BalanceConfirmation.objects.create(
            portal_user=user,
            ledger=ledger,
            confirmed_balance=balance,
            response_status=response_status,
            remarks=remarks[:1000],
        )

        try:
            pdf_bytes, filename = _generate_ledger_pdf_bytes(user)
            send_ledger_email(user, pdf_bytes, filename)
            if response_status == BalanceConfirmation.STATUS_DISPUTED:
                messages.warning(request, f"Dispute submitted for balance of {balance:,.2f}. Statement has been emailed.")
            else:
                messages.success(request, f"Balance of {balance:,.2f} confirmed. Statement has been emailed.")
        except Exception as e:
            messages.success(request, f"Response saved for balance of {balance:,.2f}. (Note: Email delivery failed: {str(e)})")

        return redirect("portal:dashboard")
    except PortalUser.DoesNotExist:
        return redirect("portal:login")

def download_ledger_pdf(request):
    user_id = request.session.get('portal_user_id')
    if not user_id:
        return redirect("portal:login")

    try:
        user = PortalUser.objects.select_related('linked_ledger__company').get(id=user_id)
        pdf_bytes, filename = _generate_ledger_pdf_bytes(user)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
    except PortalUser.DoesNotExist:
        return redirect("portal:login")

def _generate_ledger_pdf_bytes(user):
    ledger = user.linked_ledger
    company = ledger.company
    transactions, running_balance = _get_ledger_data(ledger)

    html_str = render_to_string("portal/ledger_pdf.html", {
        "user": user,
        "ledger": ledger,
        "company": company,
        "transactions": transactions,
        "total_outstanding": running_balance,
        "today": datetime.date.today(),
    })

    pdf_bytes = weasyprint.HTML(string=html_str).write_pdf()
    filename = f"Ledger_{ledger.name.replace(' ', '_')}_{datetime.date.today()}.pdf"
    return pdf_bytes, filename


@login_required
def client_request_room(request):
    companies = _companies_for_user(request.user)
    selected_company_id = request.GET.get("company", "").strip()
    status_filter = request.GET.get("status", "active").strip() or "active"
    type_filter = request.GET.get("type", "").strip()
    due_filter = request.GET.get("due", "").strip()
    q = request.GET.get("q", "").strip()
    today = timezone.localdate()

    if selected_company_id and companies.filter(pk=selected_company_id).exists():
        scope_companies = companies.filter(pk=selected_company_id)
    else:
        scope_companies = companies
        selected_company_id = ""

    requests = ClientDocumentRequest.objects.filter(company__in=scope_companies).select_related(
        "company",
        "portal_user",
        "requested_by",
        "uploaded_submission",
        "related_task",
    )

    if status_filter == "active":
        requests = requests.filter(
            status__in=[
                ClientDocumentRequest.STATUS_OPEN,
                ClientDocumentRequest.STATUS_UPLOADED,
            ]
        )
    elif status_filter == "overdue":
        requests = requests.filter(
            status=ClientDocumentRequest.STATUS_OPEN,
            due_date__lt=today,
        )
    elif status_filter:
        requests = requests.filter(status=status_filter)

    if type_filter:
        requests = requests.filter(document_type=type_filter)
    if due_filter == "today":
        requests = requests.filter(due_date=today)
    elif due_filter == "next_7":
        requests = requests.filter(due_date__gte=today, due_date__lte=today + timedelta(days=7))
    elif due_filter == "no_due":
        requests = requests.filter(due_date__isnull=True)

    if q:
        requests = requests.filter(
            Q(title__icontains=q)
            | Q(notes__icontains=q)
            | Q(response_note__icontains=q)
            | Q(source_reference__icontains=q)
            | Q(company__name__icontains=q)
            | Q(portal_user__name__icontains=q)
            | Q(portal_user__email__icontains=q)
        )

    requests = requests.order_by("status", "due_date", "company__name", "-created_at")

    if request.method == "GET" and request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="client_document_requests.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "Company",
            "Client",
            "Client Email",
            "Client WhatsApp",
            "Request",
            "Document Type",
            "Status",
            "Due Date",
            "Days Overdue",
            "Source Reference",
            "Upload Link",
            "Last Reminded At",
            "Reminder Count",
            "Related Task",
            "Notes",
            "Response Note",
        ])
        for doc in requests:
            days_overdue = ""
            if doc.due_date and doc.is_open and doc.due_date < today:
                days_overdue = (today - doc.due_date).days
            writer.writerow([
                doc.company.name,
                doc.portal_user.name if doc.portal_user else "",
                _client_request_recipient_email(doc),
                _client_request_recipient_whatsapp_number(doc),
                doc.title,
                doc.get_document_type_display(),
                doc.get_status_display(),
                doc.due_date.isoformat() if doc.due_date else "",
                days_overdue,
                doc.source_reference,
                _document_request_upload_url(doc, request),
                doc.last_reminded_at.isoformat() if doc.last_reminded_at else "",
                doc.reminder_count,
                doc.related_task.get_status_display() if doc.related_task else "",
                doc.notes,
                doc.response_note,
            ])
        return response

    if request.method == "POST":
        selected_ids = request.POST.getlist("request_ids")
        action = request.POST.get("action", "")
        selected = ClientDocumentRequest.objects.filter(
            pk__in=selected_ids,
            company__in=companies,
        ).select_related("company", "related_task")
        manageable = [doc for doc in selected if _can_manage_company(request.user, doc.company)]
        if not selected_ids:
            messages.error(request, "Select at least one client request.")
        elif len(manageable) != selected.count():
            messages.error(request, "One or more selected requests are outside your write access.")
        elif action == "create_tasks":
            created, existing = _ensure_document_request_tasks(manageable, request.user)
            messages.success(request, f"Request tasks ready: {created} created, {existing} already existed.")
        elif action == "close":
            updated = _mark_document_requests_closed(manageable, request.user)
            messages.success(request, f"Closed {updated} client request(s).")
        elif action == "reopen":
            updated = _reopen_document_requests(manageable)
            messages.success(request, f"Reopened {updated} client request(s).")
        elif action == "cancel":
            updated = _cancel_document_requests(manageable, request.user)
            messages.success(request, f"Cancelled {updated} client request(s).")
        else:
            messages.error(request, "Invalid client request action.")
        return redirect(f"{reverse('portal:client_requests')}?{request.GET.urlencode()}")

    base = ClientDocumentRequest.objects.filter(company__in=scope_companies)
    open_base = base.filter(status=ClientDocumentRequest.STATUS_OPEN)
    uploaded_base = base.filter(status=ClientDocumentRequest.STATUS_UPLOADED)
    summary = {
        "active": open_base.count() + uploaded_base.count(),
        "open": open_base.count(),
        "uploaded": uploaded_base.count(),
        "overdue": open_base.filter(due_date__lt=today).count(),
        "closed": base.filter(status=ClientDocumentRequest.STATUS_CLOSED).count(),
    }

    return render(request, "portal/client_request_room.html", {
        "requests": requests[:500],
        "companies": companies,
        "selected_company_id": selected_company_id,
        "status_filter": status_filter,
        "type_filter": type_filter,
        "due_filter": due_filter,
        "q": q,
        "summary": summary,
        "document_types": ClientDocumentRequest.DOCUMENT_TYPE_CHOICES,
        "status_choices": ClientDocumentRequest.STATUS_CHOICES,
        "today": today,
        "export_query": _export_query(request),
        "title": "Client Requests",
    })


@login_required
def client_request_create(request):
    companies = _manageable_companies_for_user(request.user)
    if not companies.exists():
        messages.error(request, "You do not have permission to create client requests.")
        return redirect("portal:client_requests")

    initial = {}
    selected_company_id = request.GET.get("company", "").strip()
    if selected_company_id and companies.filter(pk=selected_company_id).exists():
        initial["company"] = selected_company_id
    initial.update(initial_for_template(request.GET.get("template", "").strip()))

    created_request = None
    created_id = request.GET.get("created", "").strip()
    if created_id:
        created_request = ClientDocumentRequest.objects.filter(
            pk=created_id,
            company__in=companies,
        ).select_related("company", "portal_user", "related_task").first()

    if request.method == "POST":
        form = ClientDocumentRequestForm(request.POST, companies=companies)
        if form.is_valid():
            doc_request = form.save(commit=False)
            doc_request.status = ClientDocumentRequest.STATUS_OPEN
            doc_request.requested_by = request.user
            doc_request.save()
            if form.cleaned_data.get("create_task"):
                _ensure_document_request_tasks([doc_request], request.user)
            messages.success(request, "Client request created. Upload link is ready.")
            return redirect(f"{reverse('portal:client_request_create')}?created={doc_request.pk}")
    else:
        form = ClientDocumentRequestForm(initial=initial, companies=companies)

    upload_url = ""
    if created_request:
        upload_url = request.build_absolute_uri(
            reverse("portal:document_request_upload", args=[created_request.token])
        )

    return render(request, "portal/client_request_form.html", {
        "form": form,
        "created_request": created_request,
        "upload_url": upload_url,
        "request_templates": CLIENT_REQUEST_TEMPLATES,
        "title": "New Client Request",
    })


def _campaign_initial(request, companies):
    initial = {}
    selected_company_id = request.GET.get("company", "").strip()
    current_company = getattr(request, "current_company", None)
    if selected_company_id and companies.filter(pk=selected_company_id).exists():
        initial["company"] = selected_company_id
    elif current_company and companies.filter(pk=current_company.pk).exists():
        initial["company"] = current_company.pk

    template_code = request.GET.get("template", "").strip()
    initial.update(initial_for_template(template_code))
    if not initial.get("due_date"):
        initial["due_date"] = timezone.localdate() + timedelta(days=3)
    if template_code:
        initial["template"] = template_code
    return initial


def _campaign_source_reference(prefix, template, due_date, portal_user):
    base = (prefix or "").strip()
    if not base:
        base = f"CAMPAIGN:{template or 'document'}:{due_date:%Y%m%d}"
    return f"{base[:140]}:{portal_user.pk}"


def _campaign_contact_for_user(portal_user):
    ledger = portal_user.linked_ledger
    return {
        "recipient_email": portal_user.email or ledger.email or "",
        "recipient_whatsapp_number": ledger.whatsapp_number or "",
    }


@login_required
@write_required
def client_request_campaign(request):
    companies = _manageable_companies_for_user(request.user)
    if not companies.exists():
        messages.error(request, "You do not have permission to create client request campaigns.")
        return redirect("portal:client_requests")

    if request.method == "POST":
        form = ClientRequestCampaignForm(request.POST, companies=companies)
        if form.is_valid():
            company = form.cleaned_data["company"]
            portal_users = form.cleaned_data["portal_users"]
            create_task = form.cleaned_data.get("create_task")
            send_email = form.cleaned_data.get("send_email")
            created_count = 0
            existing_count = 0
            task_created = 0
            task_existing = 0
            email_candidates = []

            with transaction.atomic():
                for portal_user in portal_users:
                    source_reference = _campaign_source_reference(
                        form.cleaned_data.get("source_reference_prefix"),
                        form.cleaned_data.get("template"),
                        form.cleaned_data["due_date"],
                        portal_user,
                    )
                    contact = _campaign_contact_for_user(portal_user)
                    doc_request, created = ClientDocumentRequest.objects.get_or_create(
                        company=company,
                        portal_user=portal_user,
                        source_reference=source_reference,
                        defaults={
                            "recipient_email": contact["recipient_email"],
                            "recipient_whatsapp_number": contact["recipient_whatsapp_number"],
                            "document_type": form.cleaned_data["document_type"],
                            "title": form.cleaned_data["title"],
                            "status": ClientDocumentRequest.STATUS_OPEN,
                            "due_date": form.cleaned_data["due_date"],
                            "notes": form.cleaned_data.get("notes", ""),
                            "requested_by": request.user,
                        },
                    )
                    if created:
                        created_count += 1
                        AuditLog.objects.create(
                            company=company,
                            user=request.user,
                            action=AuditLog.ACTION_CREATE,
                            model_name="ClientDocumentRequest",
                            record_id=doc_request.pk,
                            object_repr=doc_request.title[:200],
                            old_data={},
                            new_data={
                                "source": "client_request_campaign",
                                "source_reference": source_reference,
                                "portal_user": portal_user.email,
                                "document_type": doc_request.document_type,
                                "due_date": doc_request.due_date.isoformat() if doc_request.due_date else "",
                            },
                        )
                        if send_email:
                            email_candidates.append(doc_request)
                    else:
                        existing_count += 1
                        update_fields = []
                        if contact["recipient_email"] and not doc_request.recipient_email:
                            doc_request.recipient_email = contact["recipient_email"]
                            update_fields.append("recipient_email")
                        if contact["recipient_whatsapp_number"] and not doc_request.recipient_whatsapp_number:
                            doc_request.recipient_whatsapp_number = contact["recipient_whatsapp_number"]
                            update_fields.append("recipient_whatsapp_number")
                        if update_fields:
                            doc_request.save(update_fields=[*update_fields, "updated_at"])

                    if create_task and not doc_request.related_task_id:
                        created_tasks, existing_tasks = _ensure_document_request_tasks([doc_request], request.user)
                        task_created += created_tasks
                        task_existing += existing_tasks
                    elif create_task:
                        task_existing += 1

            if send_email and email_candidates:
                sent, skipped, failed = _send_client_request_email_reminders(email_candidates, request.user, request)
                if sent:
                    messages.success(request, f"Sent {sent} campaign email reminder(s).")
                if skipped:
                    messages.warning(request, f"Skipped {skipped} request(s) without an email target.")
                if failed:
                    messages.error(request, f"{failed} campaign email reminder(s) failed.")

            messages.success(
                request,
                (
                    f"Campaign ready: {created_count} request(s) created, "
                    f"{existing_count} duplicate(s) reused, {task_created} task(s) created."
                ),
            )
            return redirect(f"{reverse('portal:client_requests')}?company={company.pk}&status=active")
    else:
        form = ClientRequestCampaignForm(
            initial=_campaign_initial(request, companies),
            companies=companies,
        )

    return render(request, "portal/client_request_campaign.html", {
        "form": form,
        "request_templates": CLIENT_REQUEST_TEMPLATES,
        "title": "Client Request Campaign",
    })


@login_required
def client_request_reminders(request):
    companies = _companies_for_user(request.user)
    selected_company_id = request.GET.get("company", "").strip()
    kind = request.GET.get("kind", "all").strip() or "all"
    today = timezone.localdate()
    due_soon = today + timedelta(days=2)

    if selected_company_id and companies.filter(pk=selected_company_id).exists():
        scope_companies = companies.filter(pk=selected_company_id)
    else:
        scope_companies = companies
        selected_company_id = ""

    base = ClientDocumentRequest.objects.filter(company__in=scope_companies).select_related(
        "company",
        "portal_user",
        "related_task",
        "uploaded_submission",
    )
    reminder_q = (
        Q(status=ClientDocumentRequest.STATUS_OPEN, due_date__lt=today)
        | Q(status=ClientDocumentRequest.STATUS_OPEN, due_date__gte=today, due_date__lte=due_soon)
        | Q(status=ClientDocumentRequest.STATUS_UPLOADED)
    )
    requests = base.filter(reminder_q)

    if kind == "overdue":
        requests = base.filter(status=ClientDocumentRequest.STATUS_OPEN, due_date__lt=today)
    elif kind == "due_soon":
        requests = base.filter(status=ClientDocumentRequest.STATUS_OPEN, due_date__gte=today, due_date__lte=due_soon)
    elif kind == "uploaded":
        requests = base.filter(status=ClientDocumentRequest.STATUS_UPLOADED)

    if request.method == "POST":
        action = request.POST.get("action", "")
        selected_ids = request.POST.getlist("request_ids")
        selected = ClientDocumentRequest.objects.filter(
            pk__in=selected_ids,
            company__in=companies,
        ).select_related("company", "related_task")
        manageable = [doc for doc in selected if _can_manage_company(request.user, doc.company)]

        if not selected_ids:
            messages.error(request, "Select at least one client request.")
        elif len(manageable) != selected.count():
            messages.error(request, "One or more selected requests are outside your write access.")
        elif action == "mark_reminded":
            updated = _mark_document_requests_reminded(manageable, request.user)
            messages.success(request, f"Marked reminder sent for {updated} client request(s).")
        elif action == "send_email":
            sent, skipped, failed = _send_client_request_email_reminders(manageable, request.user, request)
            if sent:
                messages.success(request, f"Sent {sent} email reminder(s).")
            if skipped:
                messages.warning(request, f"Skipped {skipped} request(s) without an email target or already uploaded/closed.")
            if failed:
                messages.error(request, f"{failed} email reminder(s) failed.")
        elif action == "close_uploaded":
            uploaded = [doc for doc in manageable if doc.status == ClientDocumentRequest.STATUS_UPLOADED]
            updated = _mark_document_requests_closed(uploaded, request.user)
            messages.success(request, f"Closed {updated} uploaded request(s).")
        elif action == "create_tasks":
            created, existing = _ensure_document_request_tasks(manageable, request.user)
            messages.success(request, f"Reminder tasks ready: {created} created, {existing} already existed.")
        else:
            messages.error(request, "Invalid reminder action.")
        return redirect(f"{reverse('portal:client_request_reminders')}?{request.GET.urlencode()}")

    ordered_requests = requests.order_by("status", "due_date", "company__name", "-created_at")

    if request.method == "GET" and request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="client_request_reminders.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "Company",
            "Kind",
            "Request",
            "Document Type",
            "Status",
            "Due Date",
            "Email To",
            "WhatsApp URL",
            "Upload Link",
            "Reminder Message",
        ])
        for doc in ordered_requests:
            writer.writerow([
                doc.company.name,
                _reminder_kind(doc, today, due_soon),
                doc.title,
                doc.get_document_type_display(),
                doc.get_status_display(),
                doc.due_date.isoformat() if doc.due_date else "",
                _client_request_recipient_email(doc),
                _client_request_whatsapp_url(doc, request),
                _document_request_upload_url(doc, request),
                _client_reminder_message(doc, request),
            ])
        return response

    rows = [
        {
            "request": doc,
            "kind": _reminder_kind(doc, today, due_soon),
            "message": _client_reminder_message(doc, request),
            "email_to": _client_request_recipient_email(doc),
            "whatsapp_url": _client_request_whatsapp_url(doc, request),
            "upload_url": _document_request_upload_url(doc, request),
        }
        for doc in ordered_requests[:500]
    ]
    summary = {
        "overdue": base.filter(status=ClientDocumentRequest.STATUS_OPEN, due_date__lt=today).count(),
        "due_soon": base.filter(status=ClientDocumentRequest.STATUS_OPEN, due_date__gte=today, due_date__lte=due_soon).count(),
        "uploaded": base.filter(status=ClientDocumentRequest.STATUS_UPLOADED).count(),
        "total": len(rows),
    }

    return render(request, "portal/client_request_reminders.html", {
        "rows": rows,
        "companies": companies,
        "selected_company_id": selected_company_id,
        "kind": kind,
        "summary": summary,
        "today": today,
        "due_soon": due_soon,
        "export_query": _export_query(request),
        "title": "Client Request Reminders",
    })


def _reminder_kind(doc_request, today, due_soon):
    if doc_request.status == ClientDocumentRequest.STATUS_UPLOADED:
        return "Uploaded - review pending"
    if doc_request.due_date and doc_request.due_date < today:
        return "Overdue"
    if doc_request.due_date and doc_request.due_date <= due_soon:
        return "Due soon"
    return "Follow-up"


def _client_reminder_message(doc_request, request):
    upload_url = _document_request_upload_url(doc_request, request)
    if doc_request.status == ClientDocumentRequest.STATUS_UPLOADED:
        return (
            f"Review pending: {doc_request.title} has been uploaded for "
            f"{doc_request.company.name}. Please verify and close the request."
        )
    due_text = doc_request.due_date.strftime("%d %b %Y") if doc_request.due_date else "at the earliest"
    greeting = f"Dear {doc_request.portal_user.name}," if doc_request.portal_user else "Dear Client,"
    whatsapp_line = ""
    if doc_request.company.whatsapp_intake_number:
        whatsapp_line = (
            f"\nYou can also send the document on WhatsApp to {doc_request.company.whatsapp_intake_number} "
            f"with reference {doc_request.source_reference or doc_request.pk}."
        )
    return (
        f"{greeting}\n\n"
        f"Please upload the requested document for {doc_request.company.name}: {doc_request.title}.\n"
        f"Due date: {due_text}.\n"
        f"Upload link: {upload_url}"
        f"{whatsapp_line}\n\n"
        "Regards,\n"
        "CA Team"
    )


def _document_request_upload_url(doc_request, request):
    return request.build_absolute_uri(
        reverse("portal:document_request_upload", args=[doc_request.token])
    )


def _client_request_recipient_email(doc_request):
    if doc_request.recipient_email:
        return doc_request.recipient_email
    if doc_request.portal_user and doc_request.portal_user.email:
        return doc_request.portal_user.email
    ledger = doc_request.portal_user.linked_ledger if doc_request.portal_user else None
    if ledger and ledger.email:
        return ledger.email
    return ""


def _client_request_recipient_whatsapp_number(doc_request):
    if not doc_request.recipient_whatsapp_number:
        return ""
    try:
        return normalize_phone_number(doc_request.recipient_whatsapp_number)
    except ValueError:
        return ""


def _client_request_whatsapp_url(doc_request, request):
    message = _client_reminder_message(doc_request, request)
    encoded_message = quote(message)
    whatsapp_number = _client_request_recipient_whatsapp_number(doc_request)
    if whatsapp_number:
        return f"https://wa.me/{whatsapp_number.lstrip('+')}?text={encoded_message}"
    return f"https://wa.me/?text={encoded_message}"


def _client_request_from_email(company):
    if company.invoice_email_from_address:
        sender_name = company.invoice_email_from_name or company.name
        return f"{sender_name} <{company.invoice_email_from_address}>"
    return settings.DEFAULT_FROM_EMAIL


def _send_client_request_email_reminders(document_requests, user, request):
    sent = 0
    skipped = 0
    failed = 0
    for doc_request in document_requests:
        recipient = _client_request_recipient_email(doc_request)
        if not recipient or doc_request.status != ClientDocumentRequest.STATUS_OPEN:
            skipped += 1
            continue

        subject = f"Document request: {doc_request.title} - {doc_request.company.name}"
        email = EmailMessage(
            subject=subject,
            body=_client_reminder_message(doc_request, request),
            from_email=_client_request_from_email(doc_request.company),
            to=[recipient],
            reply_to=[doc_request.company.invoice_email_reply_to] if doc_request.company.invoice_email_reply_to else None,
        )
        try:
            email.send(fail_silently=False)
        except Exception:
            failed += 1
            continue

        _mark_document_requests_reminded([doc_request], user, channel="Email reminder")
        sent += 1
    return sent, skipped, failed


def _ensure_document_request_tasks(document_requests, user):
    created = 0
    existing = 0
    today = timezone.localdate()
    for doc_request in document_requests:
        if doc_request.related_task_id:
            existing += 1
            continue
        task, was_created = PracticeTask.objects.get_or_create(
            company=doc_request.company,
            reference=f"DOCREQ:{doc_request.pk}",
            defaults={
                "title": f"Client request: {doc_request.title}",
                "task_type": PracticeTask.TYPE_DOCUMENT,
                "priority": (
                    PracticeTask.PRIORITY_CRITICAL
                    if doc_request.due_date and doc_request.due_date < today
                    else PracticeTask.PRIORITY_HIGH
                ),
                "status": PracticeTask.STATUS_OPEN,
                "due_date": doc_request.due_date,
                "created_by": user,
                "description": (
                    f"Collect/review client document request.\n"
                    f"Document type: {doc_request.get_document_type_display()}\n"
                    f"Source reference: {doc_request.source_reference or '-'}"
                ),
            },
        )
        doc_request.related_task = task
        doc_request.save(update_fields=["related_task", "updated_at"])
        if was_created:
            created += 1
        else:
            existing += 1
    return created, existing


def _mark_document_requests_reminded(document_requests, user, channel="Reminder"):
    updated = 0
    now = timezone.now()
    for doc_request in document_requests:
        if doc_request.status == ClientDocumentRequest.STATUS_CLOSED:
            continue
        doc_request.last_reminded_at = now
        doc_request.reminder_count = (doc_request.reminder_count or 0) + 1
        doc_request.save(update_fields=["last_reminded_at", "reminder_count", "updated_at"])
        if doc_request.related_task_id:
            task = doc_request.related_task
            stamp = timezone.localtime(now).strftime("%d %b %Y %H:%M")
            note = f"{channel} sent by {getattr(user, 'email', user)} at {stamp}."
            task.description = (task.description + "\n\n" + note).strip()
            task.save(update_fields=["description", "updated_at"])
        updated += 1
    return updated


def _mark_document_requests_closed(document_requests, user):
    updated = 0
    now = timezone.now()
    for doc_request in document_requests:
        doc_request.status = ClientDocumentRequest.STATUS_CLOSED
        doc_request.closed_at = now
        doc_request.save(update_fields=["status", "closed_at", "updated_at"])
        if doc_request.related_task_id:
            task = doc_request.related_task
            task.status = PracticeTask.STATUS_DONE
            task.completed_by = user
            task.completed_at = now
            task.save(update_fields=["status", "completed_by", "completed_at", "updated_at"])
        updated += 1
    return updated


def _reopen_document_requests(document_requests):
    updated = 0
    for doc_request in document_requests:
        doc_request.status = ClientDocumentRequest.STATUS_OPEN
        doc_request.closed_at = None
        doc_request.save(update_fields=["status", "closed_at", "updated_at"])
        updated += 1
    return updated


def _cancel_document_requests(document_requests, user):
    updated = 0
    now = timezone.now()
    for doc_request in document_requests:
        doc_request.status = ClientDocumentRequest.STATUS_CANCELLED
        doc_request.closed_at = now
        doc_request.save(update_fields=["status", "closed_at", "updated_at"])
        if doc_request.related_task_id:
            task = doc_request.related_task
            task.status = PracticeTask.STATUS_CANCELLED
            task.completed_by = user
            task.completed_at = now
            task.save(update_fields=["status", "completed_by", "completed_at", "updated_at"])
        updated += 1
    return updated


# --- CA DASHBOARD VIEWS ---

@staff_member_required
def ca_dashboard_view(request):
    """CA tracking dashboard for all portal user confirmations."""
    # Filter by current company to avoid cross-leakage
    company = request.current_company
    portal_users = PortalUser.objects.filter(linked_ledger__company=company).select_related('linked_ledger')
    
    reports = []
    for p_user in portal_users:
        _, balance = _get_ledger_data(p_user.linked_ledger)
        latest_conf = BalanceConfirmation.objects.filter(portal_user=p_user).first()
        status = 'Pending'
        if latest_conf:
            status = 'Disputed' if latest_conf.response_status == BalanceConfirmation.STATUS_DISPUTED else 'Confirmed'
        
        reports.append({
            'user': p_user,
            'ledger_name': p_user.linked_ledger.name,
            'outstanding': balance,
            'last_confirmed': latest_conf.confirmed_at if latest_conf else None,
            'status': status,
            'remarks': latest_conf.remarks if latest_conf else '',
        })
        
    document_requests = ClientDocumentRequest.objects.filter(company=company).select_related(
        "portal_user", "requested_by", "uploaded_submission"
    )[:50]

    return render(request, "portal/ca_dashboard.html", {
        "reports": reports,
        "company": company,
        "document_requests": document_requests,
    })

@staff_member_required
def ca_view_user_ledger(request, user_id):
    """Staff preview of a specific portal user's ledger."""
    company = request.current_company
    user = get_object_or_404(
        PortalUser.objects.select_related("linked_ledger__company"),
        id=user_id,
        linked_ledger__company=company,
    )
    
    return render(
        request,
        "portal/dashboard.html",
        _build_portal_dashboard_context(user, is_staff_view=True),
    )

@staff_member_required
def ca_download_user_pdf(request, user_id):
    """Staff download of a specific portal user's ledger PDF."""
    company = request.current_company
    user = get_object_or_404(PortalUser, id=user_id, linked_ledger__company=company)
    pdf_bytes, filename = _generate_ledger_pdf_bytes(user)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
