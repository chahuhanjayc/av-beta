import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import Company


class IntegrationRequestLog(models.Model):
    SERVICE_GSTIN = "gstin_lookup"
    SERVICE_E_INVOICE = "e_invoice"
    SERVICE_E_WAY_BILL = "e_way_bill"
    SERVICE_GST_RETURN = "gst_return"
    SERVICE_TRACES = "traces"
    SERVICE_TALLY_SYNC = "tally_sync"
    SERVICE_BANK_FEED = "bank_feed"
    SERVICE_CHOICES = [
        (SERVICE_GSTIN, "GSTIN Lookup"),
        (SERVICE_E_INVOICE, "E-Invoice"),
        (SERVICE_E_WAY_BILL, "E-Way Bill"),
        (SERVICE_GST_RETURN, "GST Return"),
        (SERVICE_TRACES, "TRACES"),
        (SERVICE_TALLY_SYNC, "Tally Sync"),
        (SERVICE_BANK_FEED, "Bank Feed"),
    ]

    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CONFIG_ERROR = "config_error"
    STATUS_CHOICES = [
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CONFIG_ERROR, "Configuration Error"),
    ]

    request_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="integration_logs")
    voucher = models.ForeignKey(
        "vouchers.Voucher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="integration_logs",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="integration_requests",
    )
    provider = models.CharField(max_length=50)
    service = models.CharField(max_length=30, choices=SERVICE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    request_digest = models.CharField(max_length=64, blank=True)
    response_code = models.CharField(max_length=30, blank=True)
    response_payload = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "service", "status"], name="integration_company_0df26f_idx"),
            models.Index(fields=["voucher", "service"], name="integration_voucher_b06ed7_idx"),
        ]

    def __str__(self):
        return f"{self.company} {self.service} {self.status} ({self.request_id})"


class IntegrationConnector(models.Model):
    TYPE_GST = "gst"
    TYPE_IRP = "irp"
    TYPE_EWAY = "eway"
    TYPE_TRACES = "traces"
    TYPE_TALLY = "tally"
    TYPE_BANK = "bank_feed"
    CONNECTOR_CHOICES = [
        (TYPE_GST, "GST Portal"),
        (TYPE_IRP, "IRP / E-Invoice"),
        (TYPE_EWAY, "E-Way Bill"),
        (TYPE_TRACES, "TRACES"),
        (TYPE_TALLY, "Tally Sync"),
        (TYPE_BANK, "Connected Banking"),
    ]

    MODE_SANDBOX = "sandbox"
    MODE_PRODUCTION = "production"
    MODE_MANUAL = "manual"
    MODE_CHOICES = [
        (MODE_SANDBOX, "Sandbox"),
        (MODE_PRODUCTION, "Production"),
        (MODE_MANUAL, "Manual / Portal Upload"),
    ]

    STATUS_DISABLED = "disabled"
    STATUS_NEEDS_SETUP = "needs_setup"
    STATUS_READY = "ready"
    STATUS_LIVE = "live"
    STATUS_BLOCKED = "blocked"
    STATUS_CHOICES = [
        (STATUS_DISABLED, "Disabled"),
        (STATUS_NEEDS_SETUP, "Needs Setup"),
        (STATUS_READY, "Ready"),
        (STATUS_LIVE, "Live"),
        (STATUS_BLOCKED, "Blocked"),
    ]

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="integration_connectors",
    )
    connector_type = models.CharField(max_length=20, choices=CONNECTOR_CHOICES)
    display_name = models.CharField(max_length=120, blank=True)
    provider_name = models.CharField(max_length=80, blank=True)
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default=MODE_MANUAL)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEEDS_SETUP)
    gstin = models.CharField(max_length=15, blank=True)
    tan = models.CharField(max_length=10, blank=True)
    username = models.CharField(max_length=120, blank=True)
    base_url = models.URLField(blank=True)
    credential_reference = models.CharField(
        max_length=160,
        blank=True,
        help_text="Reference to a vault/env secret only. Do not store raw API passwords here.",
    )
    credential_last_rotated_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["company__name", "connector_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "connector_type"],
                name="unique_company_integration_connector",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "connector_type", "status"], name="connector_company_status_idx"),
        ]

    def __str__(self):
        return f"{self.company} - {self.label}"

    @property
    def label(self):
        return self.display_name or self.get_connector_type_display()

    @property
    def is_ready(self):
        return self.status in {self.STATUS_READY, self.STATUS_LIVE}

    @property
    def masked_username(self):
        if not self.username:
            return ""
        if len(self.username) <= 6:
            return "configured"
        return f"{self.username[:3]}...{self.username[-3:]}"

    @property
    def credential_age_days(self):
        if not self.credential_last_rotated_at:
            return None
        return (timezone.now() - self.credential_last_rotated_at).days

    @property
    def status_badge_class(self):
        return {
            self.STATUS_LIVE: "bg-success",
            self.STATUS_READY: "bg-primary",
            self.STATUS_NEEDS_SETUP: "bg-warning text-dark",
            self.STATUS_BLOCKED: "bg-danger",
            self.STATUS_DISABLED: "bg-secondary",
        }.get(self.status, "bg-secondary")

    @property
    def mode_badge_class(self):
        return {
            self.MODE_PRODUCTION: "bg-success-subtle text-success",
            self.MODE_SANDBOX: "bg-primary-subtle text-primary",
            self.MODE_MANUAL: "bg-secondary-subtle text-secondary",
        }.get(self.mode, "bg-secondary-subtle text-secondary")


class IntegrationRetryJob(models.Model):
    STATUS_PENDING = "pending"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RESOLVED = "resolved"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    PRIORITY_NORMAL = "normal"
    PRIORITY_HIGH = "high"
    PRIORITY_CRITICAL = "critical"
    PRIORITY_CHOICES = [
        (PRIORITY_NORMAL, "Normal"),
        (PRIORITY_HIGH, "High"),
        (PRIORITY_CRITICAL, "Critical"),
    ]

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="integration_retry_jobs",
    )
    connector = models.ForeignKey(
        IntegrationConnector,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retry_jobs",
    )
    request_log = models.OneToOneField(
        IntegrationRequestLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retry_job",
    )
    voucher = models.ForeignKey(
        "vouchers.Voucher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="integration_retry_jobs",
    )
    service = models.CharField(max_length=30, choices=IntegrationRequestLog.SERVICE_CHOICES)
    provider = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default=PRIORITY_HIGH)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_integration_retry_jobs",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_integration_retry_jobs",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "next_attempt_at", "-priority", "-created_at"]
        indexes = [
            models.Index(fields=["company", "status", "next_attempt_at"], name="retry_company_status_next_idx"),
            models.Index(fields=["service", "status"], name="retry_service_status_idx"),
            models.Index(fields=["connector", "status"], name="retry_connector_status_idx"),
        ]

    def __str__(self):
        return f"{self.company} {self.service} retry #{self.pk or 'new'}"

    @property
    def is_open(self):
        return self.status in {self.STATUS_PENDING, self.STATUS_IN_PROGRESS, self.STATUS_FAILED}

    @property
    def is_due(self):
        return self.is_open and self.next_attempt_at <= timezone.now()

    @property
    def status_badge_class(self):
        return {
            self.STATUS_PENDING: "bg-warning text-dark",
            self.STATUS_IN_PROGRESS: "bg-primary",
            self.STATUS_RESOLVED: "bg-success",
            self.STATUS_FAILED: "bg-danger",
            self.STATUS_CANCELLED: "bg-secondary",
        }.get(self.status, "bg-secondary")


class StatutoryExportLog(models.Model):
    TYPE_GSTR1_JSON = "gstr1_json"
    TYPE_GSTR3B_JSON = "gstr3b_json"
    TYPE_E_INVOICE_PAYLOAD = "e_invoice_payload"
    TYPE_EWAY_PAYLOAD = "eway_payload"
    TYPE_TDS_FVU = "tds_fvu"
    TYPE_TALLY_SYNC = "tally_sync"
    EXPORT_TYPE_CHOICES = [
        (TYPE_GSTR1_JSON, "GSTR-1 JSON"),
        (TYPE_GSTR3B_JSON, "GSTR-3B JSON"),
        (TYPE_E_INVOICE_PAYLOAD, "E-Invoice Payload"),
        (TYPE_EWAY_PAYLOAD, "E-Way Bill Payload"),
        (TYPE_TDS_FVU, "TDS FVU"),
        (TYPE_TALLY_SYNC, "Tally Sync"),
    ]

    STATUS_GENERATED = "generated"
    STATUS_VALIDATED = "validated"
    STATUS_SUBMITTED = "submitted"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_GENERATED, "Generated"),
        (STATUS_VALIDATED, "Validated"),
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_REJECTED, "Rejected"),
    ]

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="statutory_export_logs",
    )
    connector = models.ForeignKey(
        IntegrationConnector,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="export_logs",
    )
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="statutory_export_logs",
    )
    export_type = models.CharField(max_length=30, choices=EXPORT_TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_GENERATED)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    file_name = models.CharField(max_length=180)
    file_sha256 = models.CharField(max_length=64)
    row_count = models.PositiveIntegerField(default=0)
    amount_total = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    validation_summary = models.JSONField(default=dict, blank=True)
    portal_reference = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "export_type", "created_at"], name="stat_export_company_type_idx"),
            models.Index(fields=["period_start", "period_end"], name="stat_export_period_idx"),
            models.Index(fields=["file_sha256"], name="stat_export_sha_idx"),
        ]

    def __str__(self):
        return f"{self.company} {self.get_export_type_display()} {self.file_name}"
