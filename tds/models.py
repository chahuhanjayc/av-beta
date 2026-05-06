"""
tds/models.py — Phase 7B: TDS / TCS Management

Models:
  TDSSection  — Section codes (194A, 194C, 194J etc.) with threshold & rate
  TDSEntry    — TDS deducted on a specific voucher/payment
"""

from decimal import Decimal
from django.conf import settings
from django.db import models
from core.models import Company
from ledger.models import Ledger


class TDSSection(models.Model):
    """TDS section configuration (company-scoped for custom overrides)."""
    NATURE_CHOICES = [
        ("TDS", "TDS — Tax Deducted at Source"),
        ("TCS", "TCS — Tax Collected at Source"),
    ]

    company         = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="tds_sections")
    nature          = models.CharField(max_length=3, choices=NATURE_CHOICES, default="TDS")
    section_code    = models.CharField(max_length=20, help_text="e.g. 194C, 194J, 206C")
    description     = models.CharField(max_length=200, help_text="Nature of payment/collection")
    threshold       = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"),
                                          help_text="Annual threshold limit (₹)")
    rate_individual = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("1.00"),
                                          help_text="Rate % for Individuals / HUF")
    rate_company    = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("2.00"),
                                          help_text="Rate % for Companies / Firms")
    surcharge_rate  = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    is_active       = models.BooleanField(default=True)

    class Meta:
        verbose_name        = "TDS/TCS Section"
        verbose_name_plural = "TDS/TCS Sections"
        ordering            = ["section_code"]
        unique_together     = ("company", "section_code")

    def __str__(self):
        return f"Sec {self.section_code} — {self.description[:40]}"


class TDSEntry(models.Model):
    """Records TDS deducted / TCS collected on a voucher."""
    DEDUCTEE_TYPE_CHOICES = [
        ("Individual", "Individual / HUF"),
        ("Company",    "Company / Firm"),
    ]

    company         = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="tds_entries")
    voucher         = models.ForeignKey(
        "vouchers.Voucher", on_delete=models.CASCADE, related_name="tds_entries",
        null=True, blank=True
    )
    section         = models.ForeignKey(TDSSection, on_delete=models.PROTECT, related_name="entries")
    deductee_ledger = models.ForeignKey(
        Ledger, on_delete=models.PROTECT, related_name="tds_entries_as_deductee",
        help_text="Party from whom TDS is deducted / on whom TCS is collected"
    )
    tds_ledger      = models.ForeignKey(
        Ledger, on_delete=models.PROTECT, related_name="tds_entries_as_tds_payable",
        null=True, blank=True,
        help_text="TDS Payable ledger (Liability)"
    )
    transaction_date    = models.DateField()
    deductee_type       = models.CharField(max_length=12, choices=DEDUCTEE_TYPE_CHOICES, default="Company")
    deductible_amount   = models.DecimalField(max_digits=15, decimal_places=2,
                                               help_text="Base amount on which TDS is computed")
    rate_applied        = models.DecimalField(max_digits=5, decimal_places=2, help_text="Actual rate % used")
    tds_amount          = models.DecimalField(max_digits=15, decimal_places=2)
    pan_number          = models.CharField(max_length=10, blank=True, verbose_name="Deductee PAN")
    # Deposit tracking
    is_deposited        = models.BooleanField(default=False)
    deposit_date        = models.DateField(null=True, blank=True)
    challan_number      = models.CharField(max_length=50, blank=True)
    bsr_code            = models.CharField(max_length=7, blank=True, verbose_name="BSR Code")
    notes               = models.CharField(max_length=300, blank=True)
    created_at          = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "TDS/TCS Entry"
        verbose_name_plural = "TDS/TCS Entries"
        ordering            = ["-transaction_date"]
        indexes             = [models.Index(fields=["company", "is_deposited", "transaction_date"])]

    def __str__(self):
        return (f"TDS Sec {self.section.section_code} | "
                f"{self.deductee_ledger.name} | ₹{self.tds_amount}")

    @classmethod
    def total_payable(cls, company):
        """Sum of all undeposited TDS for a company."""
        result = cls.objects.filter(company=company, is_deposited=False)\
                            .aggregate(total=models.Sum("tds_amount"))["total"]
        return result or Decimal("0.00")


class TDSReturnWorkpaper(models.Model):
    """Quarterly TDS return control sheet for TRACES/FVU filing readiness."""

    FORM_24Q = "24Q"
    FORM_26Q = "26Q"
    FORM_27Q = "27Q"
    FORM_TYPE_CHOICES = [
        (FORM_24Q, "Form 24Q - Salary"),
        (FORM_26Q, "Form 26Q - Domestic non-salary"),
        (FORM_27Q, "Form 27Q - Non-resident"),
    ]

    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"
    QUARTER_CHOICES = [
        (Q1, "Q1"),
        (Q2, "Q2"),
        (Q3, "Q3"),
        (Q4, "Q4"),
    ]

    STATUS_DRAFT = "draft"
    STATUS_READY_FOR_REVIEW = "ready_for_review"
    STATUS_FILED = "filed"
    STATUS_REOPENED = "reopened"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_READY_FOR_REVIEW, "Ready for Review"),
        (STATUS_FILED, "Filed"),
        (STATUS_REOPENED, "Reopened"),
    ]

    TRACES_NOT_CHECKED = "not_checked"
    TRACES_ACCEPTED = "accepted"
    TRACES_PROCESSED_DEFAULT = "processed_with_default"
    TRACES_REJECTED = "rejected"
    TRACES_STATUS_CHOICES = [
        (TRACES_NOT_CHECKED, "Not Checked"),
        (TRACES_ACCEPTED, "Accepted / Processed"),
        (TRACES_PROCESSED_DEFAULT, "Processed With Default"),
        (TRACES_REJECTED, "Rejected"),
    ]

    CHALLAN_NOT_CHECKED = "not_checked"
    CHALLAN_MATCHED = "matched"
    CHALLAN_UNMATCHED = "unmatched"
    CHALLAN_OVERBOOKED = "overbooked"
    CHALLAN_STATUS_CHOICES = [
        (CHALLAN_NOT_CHECKED, "Not Checked"),
        (CHALLAN_MATCHED, "Matched"),
        (CHALLAN_UNMATCHED, "Unmatched"),
        (CHALLAN_OVERBOOKED, "Overbooked"),
    ]

    FORM16_NOT_APPLICABLE = "not_applicable"
    FORM16_NOT_REQUESTED = "not_requested"
    FORM16_REQUESTED = "requested"
    FORM16_DOWNLOADED = "downloaded"
    FORM16_ISSUED = "issued"
    FORM16_STATUS_CHOICES = [
        (FORM16_NOT_APPLICABLE, "Not Applicable"),
        (FORM16_NOT_REQUESTED, "Not Requested"),
        (FORM16_REQUESTED, "Requested"),
        (FORM16_DOWNLOADED, "Downloaded"),
        (FORM16_ISSUED, "Issued"),
    ]

    FVU_NOT_RUN = "not_run"
    FVU_WARNINGS = "warnings"
    FVU_VALIDATED = "validated"
    FVU_FAILED = "failed"
    FVU_STATUS_CHOICES = [
        (FVU_NOT_RUN, "Not Run"),
        (FVU_WARNINGS, "Warnings"),
        (FVU_VALIDATED, "Validated"),
        (FVU_FAILED, "Failed"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="tds_return_workpapers")
    form_type = models.CharField(max_length=3, choices=FORM_TYPE_CHOICES, default=FORM_26Q)
    financial_year_start = models.PositiveIntegerField(help_text="Financial year start year, e.g. 2025 for FY 2025-26.")
    quarter = models.CharField(max_length=2, choices=QUARTER_CHOICES)
    period_start = models.DateField()
    period_end = models.DateField()
    due_date = models.DateField()
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    traces_token = models.CharField(max_length=120, blank=True)
    traces_statement_status = models.CharField(
        max_length=30,
        choices=TRACES_STATUS_CHOICES,
        default=TRACES_NOT_CHECKED,
    )
    challan_status = models.CharField(
        max_length=30,
        choices=CHALLAN_STATUS_CHOICES,
        default=CHALLAN_NOT_CHECKED,
    )
    form16_status = models.CharField(
        max_length=30,
        choices=FORM16_STATUS_CHOICES,
        default=FORM16_NOT_APPLICABLE,
    )
    fvu_status = models.CharField(max_length=30, choices=FVU_STATUS_CHOICES, default=FVU_NOT_RUN)
    ack_number = models.CharField(max_length=160, blank=True, verbose_name="Acknowledgement Number")
    summary_snapshot = models.JSONField(default=dict, blank=True)
    validation_snapshot = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prepared_tds_return_workpapers",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_tds_return_workpapers",
    )
    filed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="filed_tds_return_workpapers",
    )
    filed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TDS Return Workpaper"
        verbose_name_plural = "TDS Return Workpapers"
        ordering = ["-financial_year_start", "quarter", "form_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "form_type", "financial_year_start", "quarter"],
                name="tds_ret_unique_period",
            )
        ]
        indexes = [
            models.Index(fields=["company", "form_type", "financial_year_start", "quarter"], name="tds_ret_lookup_idx"),
            models.Index(fields=["status", "due_date"], name="tds_ret_status_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} {self.form_type} {self.quarter} FY {self.financial_year_label}"

    @property
    def financial_year_label(self):
        return f"{self.financial_year_start}-{str(self.financial_year_start + 1)[-2:]}"

    @property
    def is_filed(self):
        return self.status == self.STATUS_FILED


class TDSFilingPack(models.Model):
    """Saved filing export pack for a quarterly TDS return."""

    STATUS_READY = "ready"
    STATUS_FILED = "filed"
    STATUS_REOPENED = "reopened"
    STATUS_CHOICES = [
        (STATUS_READY, "Ready"),
        (STATUS_FILED, "Filed"),
        (STATUS_REOPENED, "Reopened"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="tds_filing_packs")
    workpaper = models.ForeignKey(
        TDSReturnWorkpaper,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="filing_packs",
    )
    form_type = models.CharField(max_length=3, choices=TDSReturnWorkpaper.FORM_TYPE_CHOICES, default=TDSReturnWorkpaper.FORM_26Q)
    financial_year_start = models.PositiveIntegerField(help_text="Financial year start year, e.g. 2025 for FY 2025-26.")
    quarter = models.CharField(max_length=2, choices=TDSReturnWorkpaper.QUARTER_CHOICES)
    period_start = models.DateField()
    period_end = models.DateField()
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_READY)
    summary_snapshot = models.JSONField(default=dict, blank=True)
    validation_snapshot = models.JSONField(default=dict, blank=True)
    export_snapshot = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    ack_number = models.CharField(max_length=160, blank=True, verbose_name="Acknowledgement Number")
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_tds_filing_packs",
    )
    filed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="filed_tds_filing_packs",
    )
    filed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TDS Filing Pack"
        verbose_name_plural = "TDS Filing Packs"
        ordering = ["-financial_year_start", "quarter", "form_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "form_type", "financial_year_start", "quarter"],
                name="tds_pack_unique_period",
            )
        ]
        indexes = [
            models.Index(fields=["company", "form_type", "financial_year_start", "quarter"], name="tds_pack_lookup_idx"),
            models.Index(fields=["status", "due_date"], name="tds_pack_status_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} {self.form_type} {self.quarter} FY {self.financial_year_label}"

    @property
    def financial_year_label(self):
        return f"{self.financial_year_start}-{str(self.financial_year_start + 1)[-2:]}"

    @property
    def is_filed(self):
        return self.status == self.STATUS_FILED


class TDSPostFilingTracker(models.Model):
    """TRACES lifecycle status after a TDS filing pack has been filed."""

    STATEMENT_NOT_CHECKED = "not_checked"
    STATEMENT_PENDING = "pending"
    STATEMENT_PROCESSED = "processed"
    STATEMENT_PROCESSED_DEFAULT = "processed_with_default"
    STATEMENT_REJECTED = "rejected"
    STATEMENT_STATUS_CHOICES = [
        (STATEMENT_NOT_CHECKED, "Not Checked"),
        (STATEMENT_PENDING, "Pending / In Process"),
        (STATEMENT_PROCESSED, "Processed Without Default"),
        (STATEMENT_PROCESSED_DEFAULT, "Processed With Default"),
        (STATEMENT_REJECTED, "Rejected"),
    ]

    REPORT_NOT_REQUIRED = "not_required"
    REPORT_NOT_REQUESTED = "not_requested"
    REPORT_REQUESTED = "requested"
    REPORT_DOWNLOADED = "downloaded"
    REPORT_REVIEWED = "reviewed"
    REPORT_STATUS_CHOICES = [
        (REPORT_NOT_REQUIRED, "Not Required"),
        (REPORT_NOT_REQUESTED, "Not Requested"),
        (REPORT_REQUESTED, "Requested"),
        (REPORT_DOWNLOADED, "Downloaded"),
        (REPORT_REVIEWED, "Reviewed"),
    ]

    CORRECTION_NOT_REQUIRED = "not_required"
    CORRECTION_OPEN = "open"
    CORRECTION_PREPARED = "prepared"
    CORRECTION_FILED = "filed"
    CORRECTION_ACCEPTED = "accepted"
    CORRECTION_STATUS_CHOICES = [
        (CORRECTION_NOT_REQUIRED, "Not Required"),
        (CORRECTION_OPEN, "Open"),
        (CORRECTION_PREPARED, "Prepared"),
        (CORRECTION_FILED, "Filed"),
        (CORRECTION_ACCEPTED, "Accepted"),
    ]

    pack = models.OneToOneField(TDSFilingPack, on_delete=models.CASCADE, related_name="post_filing_tracker")
    statement_status = models.CharField(
        max_length=30,
        choices=STATEMENT_STATUS_CHOICES,
        default=STATEMENT_NOT_CHECKED,
    )
    status_checked_at = models.DateTimeField(null=True, blank=True)
    traces_request_number = models.CharField(max_length=120, blank=True)
    justification_report_status = models.CharField(
        max_length=30,
        choices=REPORT_STATUS_CHOICES,
        default=REPORT_NOT_REQUIRED,
    )
    justification_request_number = models.CharField(max_length=120, blank=True)
    justification_downloaded_at = models.DateTimeField(null=True, blank=True)
    conso_file_status = models.CharField(
        max_length=30,
        choices=REPORT_STATUS_CHOICES,
        default=REPORT_NOT_REQUIRED,
    )
    conso_request_number = models.CharField(max_length=120, blank=True)
    conso_downloaded_at = models.DateTimeField(null=True, blank=True)
    correction_required = models.BooleanField(default=False)
    correction_reason = models.CharField(max_length=240, blank=True)
    correction_status = models.CharField(
        max_length=30,
        choices=CORRECTION_STATUS_CHOICES,
        default=CORRECTION_NOT_REQUIRED,
    )
    notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_tds_post_filing_trackers",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TDS Post-Filing Tracker"
        verbose_name_plural = "TDS Post-Filing Trackers"
        ordering = ["-pack__financial_year_start", "pack__quarter", "pack__form_type"]
        indexes = [
            models.Index(fields=["statement_status"], name="tds_post_statement_idx"),
            models.Index(fields=["correction_required", "correction_status"], name="tds_post_correction_idx"),
        ]

    def __str__(self):
        return f"{self.pack} - {self.get_statement_status_display()}"

    @property
    def is_processed(self):
        return self.statement_status in {self.STATEMENT_PROCESSED, self.STATEMENT_PROCESSED_DEFAULT}


class TDSCertificateIssue(models.Model):
    """Deductee-wise Form 16 / 16A issuance tracker generated from a filing pack."""

    CERT_FORM16 = "16"
    CERT_FORM16A = "16A"
    CERTIFICATE_TYPE_CHOICES = [
        (CERT_FORM16, "Form 16"),
        (CERT_FORM16A, "Form 16A"),
    ]

    STATUS_PENDING = "pending"
    STATUS_REQUESTED = "requested"
    STATUS_DOWNLOADED = "downloaded"
    STATUS_PDF_GENERATED = "pdf_generated"
    STATUS_SIGNED = "signed"
    STATUS_ISSUED = "issued"
    STATUS_BLOCKED = "blocked"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_REQUESTED, "Requested"),
        (STATUS_DOWNLOADED, "Downloaded"),
        (STATUS_PDF_GENERATED, "PDF Generated"),
        (STATUS_SIGNED, "Signed"),
        (STATUS_ISSUED, "Issued"),
        (STATUS_BLOCKED, "Blocked"),
    ]

    CHANNEL_NONE = ""
    CHANNEL_EMAIL = "email"
    CHANNEL_PORTAL = "portal"
    CHANNEL_MANUAL = "manual"
    CHANNEL_CHOICES = [
        (CHANNEL_NONE, "Not Issued"),
        (CHANNEL_EMAIL, "Email"),
        (CHANNEL_PORTAL, "Portal"),
        (CHANNEL_MANUAL, "Manual"),
    ]

    pack = models.ForeignKey(TDSFilingPack, on_delete=models.CASCADE, related_name="certificates")
    entry_serial = models.PositiveIntegerField()
    certificate_type = models.CharField(max_length=4, choices=CERTIFICATE_TYPE_CHOICES, default=CERT_FORM16A)
    deductee_name = models.CharField(max_length=200)
    deductee_pan = models.CharField(max_length=10, blank=True)
    deductee_ledger = models.ForeignKey(
        Ledger,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tds_certificate_issues",
    )
    section_code = models.CharField(max_length=20, blank=True)
    amount_paid = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0.00"))
    tds_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0.00"))
    request_number = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_PENDING)
    issue_channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default=CHANNEL_NONE, blank=True)
    evidence_reference = models.CharField(max_length=240, blank=True)
    downloaded_at = models.DateTimeField(null=True, blank=True)
    pdf_generated_at = models.DateTimeField(null=True, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issued_tds_certificates",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TDS Certificate Issue"
        verbose_name_plural = "TDS Certificate Issues"
        ordering = ["pack", "entry_serial"]
        constraints = [
            models.UniqueConstraint(fields=["pack", "entry_serial"], name="tds_cert_unique_pack_serial")
        ]
        indexes = [
            models.Index(fields=["pack", "status"], name="tds_cert_pack_status_idx"),
            models.Index(fields=["deductee_pan"], name="tds_cert_pan_idx"),
        ]

    def __str__(self):
        return f"{self.get_certificate_type_display()} {self.deductee_name} {self.pack}"

    @property
    def is_issued(self):
        return self.status == self.STATUS_ISSUED
