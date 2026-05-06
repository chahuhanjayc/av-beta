import secrets

from django.conf import settings
from django.db import models
from django.contrib.auth.hashers import make_password, check_password
from django.utils import timezone
from core.models import Company, PracticeTask
from ledger.models import Ledger

class PortalUser(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=255)  # Hashed
    linked_ledger = models.ForeignKey(Ledger, on_delete=models.CASCADE, related_name="portal_users")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def set_password(self, raw_password):
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        return check_password(raw_password, self.password)

    def __str__(self):
        return f"{self.name} ({self.email})"

class BalanceConfirmation(models.Model):
    STATUS_CONFIRMED = "confirmed"
    STATUS_DISPUTED = "disputed"
    STATUS_CHOICES = [
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_DISPUTED, "Disputed"),
    ]

    portal_user = models.ForeignKey(PortalUser, on_delete=models.CASCADE, related_name="confirmations")
    ledger = models.ForeignKey(Ledger, on_delete=models.CASCADE)
    confirmed_balance = models.DecimalField(max_digits=15, decimal_places=2)
    response_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_CONFIRMED)
    remarks = models.TextField(blank=True)
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-confirmed_at"]

    def __str__(self):
        return f"{self.get_response_status_display()} {self.confirmed_balance} on {self.confirmed_at.date()}"


class ClientDocumentRequest(models.Model):
    TYPE_GST_INVOICE = "gst_invoice"
    TYPE_GST_NOTICE = "gst_notice"
    TYPE_TDS = "tds"
    TYPE_BANK = "bank"
    TYPE_LEDGER_CONFIRMATION = "ledger_confirmation"
    TYPE_OTHER = "other"
    DOCUMENT_TYPE_CHOICES = [
        (TYPE_GST_INVOICE, "GST Invoice / Bill"),
        (TYPE_GST_NOTICE, "GST Notice Evidence"),
        (TYPE_TDS, "TDS Document"),
        (TYPE_BANK, "Bank Statement"),
        (TYPE_LEDGER_CONFIRMATION, "Ledger Confirmation"),
        (TYPE_OTHER, "Other Document"),
    ]

    STATUS_OPEN = "open"
    STATUS_UPLOADED = "uploaded"
    STATUS_CLOSED = "closed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_UPLOADED, "Uploaded"),
        (STATUS_CLOSED, "Closed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="client_document_requests")
    portal_user = models.ForeignKey(
        PortalUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="document_requests",
    )
    recipient_email = models.EmailField(
        blank=True,
        null=True,
        help_text="Email address used for document request reminders.",
    )
    recipient_whatsapp_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Client WhatsApp number used for direct reminder links.",
    )
    title = models.CharField(max_length=180)
    document_type = models.CharField(max_length=40, choices=DOCUMENT_TYPE_CHOICES, default=TYPE_OTHER)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    due_date = models.DateField(null=True, blank=True)
    token = models.CharField(max_length=64, unique=True, blank=True)
    source_reference = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    response_note = models.TextField(blank=True)
    related_task = models.ForeignKey(
        PracticeTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="client_document_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_client_document_requests",
    )
    uploaded_submission = models.ForeignKey(
        "ocr.OCRSubmission",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="client_document_requests",
    )
    uploaded_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    last_reminded_at = models.DateTimeField(null=True, blank=True)
    reminder_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "due_date", "-created_at"]
        indexes = [
            models.Index(fields=["company", "status", "due_date"], name="portal_docreq_cmp_stat_idx"),
            models.Index(fields=["source_reference"], name="portal_docreq_source_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = secrets.token_urlsafe(32)
        return super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.status == self.STATUS_OPEN

    @property
    def is_overdue(self):
        return bool(self.due_date and self.is_open and self.due_date < timezone.localdate())

    def __str__(self):
        return f"{self.company.name} - {self.title}"
