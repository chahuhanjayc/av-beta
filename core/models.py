"""
core/models.py
Company, UserCompanyAccess, and AuditLog models.
Every business model in the system has a FK to Company for strict multi-tenancy.
"""

import os
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone


class Company(models.Model):
    name = models.CharField(max_length=255)
    gstin = models.CharField(
        max_length=15, blank=True, null=True,
        verbose_name="GSTIN",
        help_text="15-character GST Identification Number (optional)",
    )
    tan = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        verbose_name="TAN",
        help_text="10-character Tax Deduction and Collection Account Number (optional)",
    )
    tds_responsible_person = models.CharField(
        max_length=120,
        blank=True,
        verbose_name="TDS Responsible Person",
        help_text="Person responsible for TDS/TRACES filings.",
    )
    tds_responsible_designation = models.CharField(
        max_length=80,
        blank=True,
        verbose_name="TDS Responsible Designation",
        help_text="Designation used on TDS return workpapers.",
    )
    address = models.TextField(blank=True, null=True)
    short_code = models.CharField(
        max_length=6, blank=True,
        help_text="Used in voucher number prefix, e.g. ABC for ABC Corp",
    )
    financial_year_start = models.DateField(
        null=True, blank=True, help_text="e.g. 2024-04-01"
    )

    # ── Banking & UPI payment details ────────────────────────────────────────
    upi_id = models.CharField(
        max_length=50, blank=True, null=True,
        verbose_name="UPI ID",
        help_text="e.g. business@ybl — used to generate Pay-Now QR on Sales invoices",
    )
    bank_name = models.CharField(
        max_length=100, blank=True, null=True,
        verbose_name="Bank Name",
        help_text="e.g. State Bank of India",
    )
    account_number = models.CharField(
        max_length=30, blank=True, null=True,
        verbose_name="Account Number",
    )
    ifsc_code = models.CharField(
        max_length=11, blank=True, null=True,
        verbose_name="IFSC Code",
        help_text="11-character IFSC code, e.g. SBIN0001234",
    )
    whatsapp_intake_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        unique=True,
        verbose_name="WhatsApp Intake Number",
        help_text="Business WhatsApp number clients send documents to, e.g. +919876543210.",
    )
    invoice_email_from_name = models.CharField(
        max_length=120,
        blank=True,
        verbose_name="Invoice Email Sender Name",
        help_text="Display name used when sending invoices by email.",
    )
    invoice_email_from_address = models.EmailField(
        blank=True,
        null=True,
        verbose_name="Invoice Email Sender Address",
        help_text="Sender email used for invoice emails. Leave blank to use the system default.",
    )
    invoice_email_reply_to = models.EmailField(
        blank=True,
        null=True,
        verbose_name="Invoice Email Reply-To",
        help_text="Replies to invoice emails will go to this address. Leave blank to use the sender address.",
    )
    invoice_email_subject = models.CharField(
        max_length=180,
        blank=True,
        default="Invoice {voucher_number} from {company_name}",
        verbose_name="Invoice Email Subject",
        help_text="Available placeholders: {voucher_number}, {company_name}, {client_name}, {amount}.",
    )
    invoice_email_body = models.TextField(
        blank=True,
        default=(
            "Dear {client_name},\n\n"
            "Please find attached invoice {voucher_number} from {company_name} "
            "for {amount}.\n\n"
            "Regards,\n{company_name}"
        ),
        verbose_name="Invoice Email Body",
        help_text="Available placeholders: {voucher_number}, {company_name}, {client_name}, {amount}.",
    )
    payment_reminder_email_subject = models.CharField(
        max_length=180,
        blank=True,
        default="Payment reminder: Invoice {voucher_number} from {company_name}",
        verbose_name="Payment Reminder Email Subject",
        help_text=(
            "Available placeholders: {voucher_number}, {company_name}, {client_name}, "
            "{amount}, {outstanding}, {due_date}, {aging_line}."
        ),
    )
    payment_reminder_email_body = models.TextField(
        blank=True,
        default=(
            "Dear {client_name},\n\n"
            "This is a payment reminder for invoice {voucher_number} from {company_name}.\n"
            "Outstanding amount: {outstanding}\n"
            "Due date: {due_date}\n"
            "{aging_line}\n\n"
            "Please ignore this message if payment has already been made.\n\n"
            "Regards,\n{company_name}"
        ),
        verbose_name="Payment Reminder Email Body",
        help_text=(
            "Available placeholders: {voucher_number}, {company_name}, {client_name}, "
            "{amount}, {outstanding}, {due_date}, {aging_line}."
        ),
    )
    e_invoice_enabled = models.BooleanField(
        default=False,
        verbose_name="E-Invoice Applicable",
        help_text="Enable IRN deadline watch for clients covered by GST e-invoicing.",
    )
    e_invoice_aato_crore = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name="AATO (Rs. crore)",
        help_text="Aggregate annual turnover used to decide e-invoice applicability.",
    )
    e_invoice_reporting_deadline_days = models.PositiveSmallIntegerField(
        default=30,
        verbose_name="IRP Reporting Deadline Days",
        help_text="Maximum days from invoice date to report eligible documents on IRP.",
    )
    e_invoice_warning_days = models.PositiveSmallIntegerField(
        default=25,
        verbose_name="IRP Warning Day",
        help_text="Flag invoices without IRN once they cross this age in days.",
    )

    # Secure token for public (unauthenticated) client upload portal
    portal_token = models.CharField(
        max_length=64, blank=True, null=True, unique=True,
        help_text="A secure token for the client upload portal."
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Company"
        verbose_name_plural = "Companies"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # ... existing save logic ...
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.pk and self.audit_logs.exists():
            raise ValidationError("Companies with audit logs cannot be deleted.")
        return super().delete(*args, **kwargs)


class ClientEngagement(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_ONBOARDING = "onboarding"
    STATUS_PAUSED = "paused"
    STATUS_EXITING = "exiting"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_ONBOARDING, "Onboarding"),
        (STATUS_PAUSED, "Paused"),
        (STATUS_EXITING, "Exiting"),
    ]

    PACKAGE_BASIC = "basic_compliance"
    PACKAGE_GST_TDS = "gst_tds"
    PACKAGE_FULL_ACCOUNTING = "full_accounting"
    PACKAGE_CFO = "virtual_cfo"
    PACKAGE_AUDIT = "audit_tax"
    PACKAGE_CUSTOM = "custom"
    SERVICE_PACKAGE_CHOICES = [
        (PACKAGE_BASIC, "Basic Compliance"),
        (PACKAGE_GST_TDS, "GST + TDS"),
        (PACKAGE_FULL_ACCOUNTING, "Full Accounting"),
        (PACKAGE_CFO, "Virtual CFO / Advisory"),
        (PACKAGE_AUDIT, "Audit + Tax"),
        (PACKAGE_CUSTOM, "Custom"),
    ]

    BILLING_MONTHLY = "monthly"
    BILLING_QUARTERLY = "quarterly"
    BILLING_ANNUAL = "annual"
    BILLING_PROJECT = "project"
    BILLING_CHOICES = [
        (BILLING_MONTHLY, "Monthly"),
        (BILLING_QUARTERLY, "Quarterly"),
        (BILLING_ANNUAL, "Annual"),
        (BILLING_PROJECT, "Project / One-time"),
    ]

    RISK_LOW = "low"
    RISK_MEDIUM = "medium"
    RISK_HIGH = "high"
    RISK_CRITICAL = "critical"
    RISK_CHOICES = [
        (RISK_LOW, "Low"),
        (RISK_MEDIUM, "Medium"),
        (RISK_HIGH, "High"),
        (RISK_CRITICAL, "Critical"),
    ]

    company = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        related_name="engagement",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    service_package = models.CharField(
        max_length=30,
        choices=SERVICE_PACKAGE_CHOICES,
        default=PACKAGE_GST_TDS,
    )
    monthly_retainer = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    billing_cycle = models.CharField(max_length=20, choices=BILLING_CHOICES, default=BILLING_MONTHLY)
    renewal_date = models.DateField(null=True, blank=True)
    partner_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="partner_engagements",
    )
    manager_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manager_engagements",
    )
    risk_rating = models.CharField(max_length=20, choices=RISK_CHOICES, default=RISK_MEDIUM)
    scope_summary = models.TextField(blank=True)
    out_of_scope = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)
    last_reviewed_at = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["company__name"]
        indexes = [
            models.Index(fields=["status", "risk_rating"], name="core_engage_status_risk_idx"),
            models.Index(fields=["renewal_date"], name="core_engage_renewal_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} engagement"

    @property
    def annualized_retainer(self):
        if self.billing_cycle == self.BILLING_ANNUAL:
            return self.monthly_retainer
        if self.billing_cycle == self.BILLING_QUARTERLY:
            return self.monthly_retainer * Decimal("4")
        if self.billing_cycle == self.BILLING_PROJECT:
            return self.monthly_retainer
        return self.monthly_retainer * Decimal("12")


class CompanyStatutoryProfile(models.Model):
    GST_FREQUENCY_MONTHLY = "monthly"
    GST_FREQUENCY_QRMP = "qrmp"
    GST_FREQUENCY_CHOICES = [
        (GST_FREQUENCY_MONTHLY, "Monthly"),
        (GST_FREQUENCY_QRMP, "QRMP / Quarterly"),
    ]

    GSTR1_MONTHLY = "monthly"
    GSTR1_QUARTERLY = "quarterly"
    GSTR1_FREQUENCY_CHOICES = [
        (GSTR1_MONTHLY, "Monthly"),
        (GSTR1_QUARTERLY, "Quarterly"),
    ]

    QRMP_GROUP_A = "A"
    QRMP_GROUP_B = "B"
    QRMP_GROUP_CUSTOM = "CUSTOM"
    QRMP_GROUP_CHOICES = [
        (QRMP_GROUP_A, "Group A / 22nd"),
        (QRMP_GROUP_B, "Group B / 24th"),
        (QRMP_GROUP_CUSTOM, "Custom"),
    ]

    company = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        related_name="statutory_profile",
    )
    gst_registered = models.BooleanField(
        default=True,
        verbose_name="GST Registered",
        help_text="Disable GST deadline generation for clients not registered under GST.",
    )
    gst_return_frequency = models.CharField(
        max_length=20,
        choices=GST_FREQUENCY_CHOICES,
        default=GST_FREQUENCY_MONTHLY,
        verbose_name="GSTR-3B Frequency",
    )
    gstr1_frequency = models.CharField(
        max_length=20,
        choices=GSTR1_FREQUENCY_CHOICES,
        default=GSTR1_MONTHLY,
        verbose_name="GSTR-1 Frequency",
    )
    qrmp_group = models.CharField(
        max_length=10,
        choices=QRMP_GROUP_CHOICES,
        default=QRMP_GROUP_A,
        verbose_name="QRMP State Group",
    )
    gstr1_monthly_due_day = models.PositiveSmallIntegerField(default=11)
    gstr1_quarterly_due_day = models.PositiveSmallIntegerField(default=13)
    gstr3b_monthly_due_day = models.PositiveSmallIntegerField(default=20)
    gstr3b_qrmp_due_day = models.PositiveSmallIntegerField(default=22)
    gst_late_fee_per_day = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("50.00"))
    gst_nil_late_fee_per_day = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("20.00"))
    gst_interest_rate_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("18.00"))

    tds_applicable = models.BooleanField(default=True, verbose_name="TDS Applicable")
    tds_24q_enabled = models.BooleanField(default=False, verbose_name="Form 24Q")
    tds_26q_enabled = models.BooleanField(default=True, verbose_name="Form 26Q")
    tds_27q_enabled = models.BooleanField(default=False, verbose_name="Form 27Q")
    tds_deposit_due_day = models.PositiveSmallIntegerField(default=7)
    tds_march_deposit_due_day = models.PositiveSmallIntegerField(default=30)
    tds_deposit_interest_rate_percent_per_month = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("1.50"),
    )
    tds_return_late_fee_per_day = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("200.00"))

    msme_watch_enabled = models.BooleanField(default=True, verbose_name="MSME Watch")
    msme_default_credit_days = models.PositiveSmallIntegerField(default=45)
    msme_interest_rate_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("18.00"))
    due_date_grace_days = models.PositiveSmallIntegerField(
        default=0,
        help_text="Internal grace window for client triage only. It does not change statutory due dates.",
    )
    rules_notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_statutory_profiles",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Company Statutory Profile"
        verbose_name_plural = "Company Statutory Profiles"
        ordering = ["company__name"]

    def __str__(self):
        return f"{self.company.name} statutory profile"

    def clean(self):
        super().clean()
        day_fields = [
            "gstr1_monthly_due_day",
            "gstr1_quarterly_due_day",
            "gstr3b_monthly_due_day",
            "gstr3b_qrmp_due_day",
            "tds_deposit_due_day",
            "tds_march_deposit_due_day",
        ]
        errors = {}
        for field_name in day_fields:
            value = getattr(self, field_name)
            if value < 1 or value > 31:
                errors[field_name] = "Due day must be between 1 and 31."
        if self.gstr3b_qrmp_due_day not in {22, 24} and self.qrmp_group != self.QRMP_GROUP_CUSTOM:
            errors["qrmp_group"] = "Use Custom when the QRMP due day is not 22 or 24."
        if errors:
            raise ValidationError(errors)


class StatutoryRuleOverride(models.Model):
    RULE_GSTR1 = "GSTR1"
    RULE_GSTR3B = "GSTR3B"
    RULE_TDS_DEPOSIT = "TDS_DEPOSIT"
    RULE_TDS_RETURN_24Q = "TDS_RETURN_24Q"
    RULE_TDS_RETURN_26Q = "TDS_RETURN_26Q"
    RULE_TDS_RETURN_27Q = "TDS_RETURN_27Q"
    RULE_MSME_PAYMENT = "MSME_PAYMENT"
    RULE_E_INVOICE = "E_INVOICE"
    RULE_OTHER = "OTHER"
    RULE_TYPE_CHOICES = [
        (RULE_GSTR1, "GSTR-1"),
        (RULE_GSTR3B, "GSTR-3B"),
        (RULE_TDS_DEPOSIT, "TDS Deposit"),
        (RULE_TDS_RETURN_24Q, "TDS Return 24Q"),
        (RULE_TDS_RETURN_26Q, "TDS Return 26Q"),
        (RULE_TDS_RETURN_27Q, "TDS Return 27Q"),
        (RULE_MSME_PAYMENT, "MSME Payment"),
        (RULE_E_INVOICE, "E-Invoice"),
        (RULE_OTHER, "Other"),
    ]

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="statutory_rule_overrides",
    )
    rule_type = models.CharField(max_length=30, choices=RULE_TYPE_CHOICES)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    original_due_date = models.DateField(null=True, blank=True)
    override_due_date = models.DateField(null=True, blank=True)
    late_fee_per_day = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    interest_rate_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    reason = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_statutory_rule_overrides",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_active", "rule_type", "-period_start", "-updated_at"]
        indexes = [
            models.Index(fields=["company", "rule_type", "is_active"], name="core_rule_company_type_idx"),
            models.Index(fields=["period_start", "period_end"], name="core_rule_period_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} {self.get_rule_type_display()} override"

    def clean(self):
        super().clean()
        if self.period_start and self.period_end and self.period_start > self.period_end:
            raise ValidationError({"period_end": "Period end cannot be before period start."})
        if not any([self.override_due_date, self.late_fee_per_day, self.interest_rate_percent]):
            raise ValidationError("Add a due date override, late-fee override, or interest-rate override.")


class CompanySettings(models.Model):
    """
    Global configuration for a company.
    """
    company = models.OneToOneField(
        Company, on_delete=models.CASCADE, related_name="settings"
    )
    # Inventory
    default_valuation_method = models.CharField(
        max_length=10, 
        choices=[("WAC", "Weighted Average Cost"), ("FIFO", "First-In, First-Out")],
        default="WAC"
    )
    track_inventory_by_default = models.BooleanField(default=True)
    
    # Accounting
    auto_create_tax_lines = models.BooleanField(
        default=True, 
        help_text="Automatically create GST lines based on StockItem tax rates."
    )
    books_closed_until = models.DateField(
        null=True, blank=True,
        help_text="Period Locking: No vouchers can be created or edited on or before this date."
    )
    inventory_locked_until = models.DateField(
        null=True, blank=True,
        help_text="Inventory Locking: No stock vouchers can be created or edited on or before this date."
    )
    bank_locked_until = models.DateField(
        null=True, blank=True,
        help_text="Bank Locking: No bank vouchers can be created or edited on or before this date."
    )

    def clean(self):
        super().clean()
        if self.books_closed_until:
            from datetime import date
            from django.core.exceptions import ValidationError
            
            # Check date is the first of the month being locked
            check_date = self.books_closed_until.replace(day=1)
            
            required_items = ["Bank Reconciliation", "GST Match", "Depreciation"]
            
            # For each month up to the lock date, ensure items exist and are completed
            # To keep it simple, we only check the specific month being locked 
            # and any other month that has checklist items recorded.
            
            pending_items = ChecklistItem.objects.filter(
                company=self.company,
                month__lte=check_date,
                is_completed=False
            )
            
            missing_or_pending = []
            
            if pending_items.exists():
                for item in pending_items:
                    missing_or_pending.append(f"{item.name} ({item.month.strftime('%b %Y')})")
            
            # Also check if any are missing for THIS month
            for name in required_items:
                if not ChecklistItem.objects.filter(company=self.company, name=name, month=check_date).exists():
                    # Create it if it's missing? No, clean() shouldn't have side effects.
                    # Just report it as missing.
                    missing_or_pending.append(f"{name} ({check_date.strftime('%b %Y')}) [Missing]")

            if missing_or_pending:
                raise ValidationError(
                    f"Cannot lock period! The following checklist items are incomplete or missing: {', '.join(missing_or_pending)}"
                )

    def __str__(self):
        return f"Settings for {self.company.name}"


class ChecklistItem(models.Model):
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="checklist_items"
    )
    name = models.CharField(max_length=100)
    month = models.DateField(help_text="The first day of the month this item is for.")
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        unique_together = ("company", "name", "month")
        ordering = ["-month", "name"]

    def __str__(self):
        return f"{self.name} - {self.month.strftime('%b %Y')} ({'Completed' if self.is_completed else 'Pending'})"


class PracticeTask(models.Model):
    TYPE_GST = "GST"
    TYPE_TDS = "TDS"
    TYPE_ITR = "ITR"
    TYPE_MCA = "MCA"
    TYPE_AUDIT = "AUDIT"
    TYPE_NOTICE = "NOTICE"
    TYPE_DOCUMENT = "DOCUMENT"
    TYPE_BANK = "BANK"
    TYPE_OTHER = "OTHER"
    TASK_TYPE_CHOICES = [
        (TYPE_GST, "GST"),
        (TYPE_TDS, "TDS"),
        (TYPE_ITR, "ITR"),
        (TYPE_MCA, "MCA"),
        (TYPE_AUDIT, "Audit"),
        (TYPE_NOTICE, "Notice"),
        (TYPE_DOCUMENT, "Document Chase"),
        (TYPE_BANK, "Banking"),
        (TYPE_OTHER, "Other"),
    ]

    PRIORITY_LOW = "low"
    PRIORITY_NORMAL = "normal"
    PRIORITY_HIGH = "high"
    PRIORITY_CRITICAL = "critical"
    PRIORITY_CHOICES = [
        (PRIORITY_LOW, "Low"),
        (PRIORITY_NORMAL, "Normal"),
        (PRIORITY_HIGH, "High"),
        (PRIORITY_CRITICAL, "Critical"),
    ]

    STATUS_OPEN = "open"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_BLOCKED = "blocked"
    STATUS_DONE = "done"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_BLOCKED, "Blocked"),
        (STATUS_DONE, "Done"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="practice_tasks")
    title = models.CharField(max_length=160)
    task_type = models.CharField(max_length=20, choices=TASK_TYPE_CHOICES, default=TYPE_OTHER)
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    due_date = models.DateField(null=True, blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_practice_tasks",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_practice_tasks",
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_practice_tasks",
    )
    reference = models.CharField(max_length=120, blank=True, help_text="Notice number, filing ref, or external task id.")
    description = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "due_date", "-priority", "company__name"]
        indexes = [
            models.Index(fields=["company", "status", "due_date"], name="core_task_cmp_stat_due_idx"),
            models.Index(fields=["assigned_to", "status", "due_date"], name="core_task_assignee_status_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} - {self.title}"

    @property
    def is_open(self):
        return self.status not in {self.STATUS_DONE, self.STATUS_CANCELLED}


class PilotFeedback(models.Model):
    TYPE_PILOT_CALL = "pilot_call"
    TYPE_OBJECTION = "objection"
    TYPE_FEATURE_REQUEST = "feature_request"
    TYPE_BUG = "bug"
    TYPE_WORKFLOW_GAP = "workflow_gap"
    TYPE_TRAINING = "training"
    TYPE_CONVERSION_SIGNAL = "conversion_signal"
    FEEDBACK_TYPE_CHOICES = [
        (TYPE_PILOT_CALL, "Pilot Call"),
        (TYPE_OBJECTION, "Objection"),
        (TYPE_FEATURE_REQUEST, "Feature Request"),
        (TYPE_BUG, "Bug"),
        (TYPE_WORKFLOW_GAP, "Workflow Gap"),
        (TYPE_TRAINING, "Training Need"),
        (TYPE_CONVERSION_SIGNAL, "Conversion Signal"),
    ]

    SENTIMENT_POSITIVE = "positive"
    SENTIMENT_NEUTRAL = "neutral"
    SENTIMENT_NEGATIVE = "negative"
    SENTIMENT_CHOICES = [
        (SENTIMENT_POSITIVE, "Positive"),
        (SENTIMENT_NEUTRAL, "Neutral"),
        (SENTIMENT_NEGATIVE, "Negative"),
    ]

    SEVERITY_LOW = "low"
    SEVERITY_MEDIUM = "medium"
    SEVERITY_HIGH = "high"
    SEVERITY_CRITICAL = "critical"
    SEVERITY_CHOICES = [
        (SEVERITY_LOW, "Low"),
        (SEVERITY_MEDIUM, "Medium"),
        (SEVERITY_HIGH, "High"),
        (SEVERITY_CRITICAL, "Critical"),
    ]

    STATUS_OPEN = "open"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RESOLVED = "resolved"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_DISMISSED, "Dismissed"),
    ]

    COMPETITOR_NONE = ""
    COMPETITOR_TALLY = "tally"
    COMPETITOR_ZOHO = "zoho"
    COMPETITOR_BUSY = "busy"
    COMPETITOR_MARG = "marg"
    COMPETITOR_CLEAR = "clear"
    COMPETITOR_LEDGER = "ledger"
    COMPETITOR_SPREADSHEET = "spreadsheet"
    COMPETITOR_OTHER = "other"
    COMPETITOR_CHOICES = [
        (COMPETITOR_NONE, "No direct competitor"),
        (COMPETITOR_TALLY, "Tally"),
        (COMPETITOR_ZOHO, "Zoho Books"),
        (COMPETITOR_BUSY, "Busy"),
        (COMPETITOR_MARG, "Marg"),
        (COMPETITOR_CLEAR, "Clear"),
        (COMPETITOR_LEDGER, "Ledger / Vyapar"),
        (COMPETITOR_SPREADSHEET, "Excel / Spreadsheet"),
        (COMPETITOR_OTHER, "Other"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="pilot_feedback")
    feedback_type = models.CharField(max_length=30, choices=FEEDBACK_TYPE_CHOICES, default=TYPE_PILOT_CALL)
    sentiment = models.CharField(max_length=20, choices=SENTIMENT_CHOICES, default=SENTIMENT_NEUTRAL)
    confidence_score = models.PositiveSmallIntegerField(
        default=7,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
        help_text="0 to 10 signal of client confidence in replacing the current workflow.",
    )
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default=SEVERITY_MEDIUM)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    summary = models.CharField(max_length=220)
    detail = models.TextField(blank=True)
    client_contact = models.CharField(max_length=160, blank=True)
    competitor_reference = models.CharField(max_length=30, choices=COMPETITOR_CHOICES, blank=True, default=COMPETITOR_NONE)
    evidence_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text="Call note, ticket id, client quote reference, screen recording, or uploaded evidence link.",
    )
    occurred_on = models.DateField(default=timezone.localdate)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_pilot_feedback",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_pilot_feedback",
    )
    follow_up_task = models.ForeignKey(
        PracticeTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pilot_feedback_items",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "-occurred_on", "company__name"]
        indexes = [
            models.Index(fields=["company", "status"], name="core_pfb_cmp_stat_idx"),
            models.Index(fields=["occurred_on"], name="core_pfb_occ_idx"),
            models.Index(fields=["feedback_type", "sentiment"], name="core_pfb_type_sent_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} - {self.summary}"

    @property
    def is_open(self):
        return self.status not in {self.STATUS_RESOLVED, self.STATUS_DISMISSED}

    @property
    def is_blocker(self):
        return self.is_open and self.severity in {self.SEVERITY_HIGH, self.SEVERITY_CRITICAL}

    @property
    def status_badge_class(self):
        return {
            self.STATUS_OPEN: "bg-danger-subtle text-danger",
            self.STATUS_IN_PROGRESS: "bg-warning-subtle text-warning",
            self.STATUS_RESOLVED: "bg-success-subtle text-success",
            self.STATUS_DISMISSED: "bg-secondary-subtle text-secondary",
        }.get(self.status, "bg-secondary")

    @property
    def severity_badge_class(self):
        return {
            self.SEVERITY_CRITICAL: "bg-danger",
            self.SEVERITY_HIGH: "bg-warning text-dark",
            self.SEVERITY_MEDIUM: "bg-info text-dark",
            self.SEVERITY_LOW: "bg-secondary",
        }.get(self.severity, "bg-secondary")

    @property
    def sentiment_badge_class(self):
        return {
            self.SENTIMENT_POSITIVE: "bg-success-subtle text-success",
            self.SENTIMENT_NEUTRAL: "bg-secondary-subtle text-secondary",
            self.SENTIMENT_NEGATIVE: "bg-danger-subtle text-danger",
        }.get(self.sentiment, "bg-secondary")


class MarketProofCaseStudy(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_READY = "ready"
    STATUS_APPROVED = "approved"
    STATUS_PUBLISHED = "published"
    STATUS_ON_HOLD = "on_hold"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_READY, "Ready for Review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_PUBLISHED, "Published"),
        (STATUS_ON_HOLD, "On Hold"),
    ]

    OUTCOME_EVALUATING = "evaluating"
    OUTCOME_CONVERTED = "converted"
    OUTCOME_PAID = "paid"
    OUTCOME_EXPANDED = "expanded"
    OUTCOME_PAUSED = "paused"
    OUTCOME_CHOICES = [
        (OUTCOME_EVALUATING, "Evaluating"),
        (OUTCOME_CONVERTED, "Converted"),
        (OUTCOME_PAID, "Paid"),
        (OUTCOME_EXPANDED, "Expanded"),
        (OUTCOME_PAUSED, "Paused"),
    ]

    SOURCE_TALLY = "tally"
    SOURCE_ZOHO = "zoho"
    SOURCE_BUSY = "busy"
    SOURCE_SPREADSHEET = "spreadsheet"
    SOURCE_MANUAL = "manual"
    SOURCE_OTHER = "other"
    SOURCE_CHOICES = [
        (SOURCE_TALLY, "Tally"),
        (SOURCE_ZOHO, "Zoho Books"),
        (SOURCE_BUSY, "Busy"),
        (SOURCE_SPREADSHEET, "Excel / Spreadsheet"),
        (SOURCE_MANUAL, "Manual / Paper"),
        (SOURCE_OTHER, "Other"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="market_case_studies")
    title = models.CharField(max_length=180)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, default=OUTCOME_EVALUATING)
    migration_source = models.CharField(max_length=30, choices=SOURCE_CHOICES, default=SOURCE_TALLY)
    client_contact = models.CharField(max_length=160, blank=True)
    client_role = models.CharField(max_length=120, blank=True)
    testimonial_quote = models.TextField(blank=True)
    publish_consent = models.BooleanField(default=False)
    anonymized = models.BooleanField(default=True)
    consent_reference = models.CharField(max_length=180, blank=True)
    evidence_reference = models.CharField(max_length=255, blank=True)
    before_process_hours = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    after_process_hours = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    monthly_documents = models.PositiveIntegerField(default=0)
    monthly_invoices = models.PositiveIntegerField(default=0)
    gst_periods_completed = models.PositiveIntegerField(default=0)
    tally_parallel_run_days = models.PositiveIntegerField(default=0)
    cutover_date = models.DateField(null=True, blank=True)
    commercial_value = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    value_summary = models.TextField(blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_market_case_studies",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_market_case_studies",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_market_case_studies",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "-updated_at", "company__name"]
        indexes = [
            models.Index(fields=["company", "status"], name="core_case_cmp_stat_idx"),
            models.Index(fields=["outcome", "migration_source"], name="core_case_out_src_idx"),
            models.Index(fields=["publish_consent", "status"], name="core_case_consent_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} - {self.title}"

    @property
    def is_approved(self):
        return self.status in {self.STATUS_APPROVED, self.STATUS_PUBLISHED}

    @property
    def is_publishable(self):
        return (
            self.is_approved
            and self.publish_consent
            and bool(self.consent_reference.strip())
            and bool(self.testimonial_quote.strip())
            and bool(self.evidence_reference.strip())
            and self.has_metric_proof
        )

    @property
    def has_metric_proof(self):
        return (
            self.gst_periods_completed > 0
            or self.monthly_documents > 0
            or self.monthly_invoices > 0
            or bool(self.before_process_hours and self.after_process_hours)
            or self.commercial_value > 0
        )

    @property
    def hours_saved(self):
        if self.before_process_hours is None or self.after_process_hours is None:
            return None
        return max(Decimal("0.00"), self.before_process_hours - self.after_process_hours)

    @property
    def status_badge_class(self):
        return {
            self.STATUS_DRAFT: "bg-secondary",
            self.STATUS_READY: "bg-primary",
            self.STATUS_APPROVED: "bg-success",
            self.STATUS_PUBLISHED: "bg-success",
            self.STATUS_ON_HOLD: "bg-warning text-dark",
        }.get(self.status, "bg-secondary")

    @property
    def outcome_badge_class(self):
        return {
            self.OUTCOME_EVALUATING: "bg-secondary-subtle text-secondary",
            self.OUTCOME_CONVERTED: "bg-primary-subtle text-primary",
            self.OUTCOME_PAID: "bg-success-subtle text-success",
            self.OUTCOME_EXPANDED: "bg-success-subtle text-success",
            self.OUTCOME_PAUSED: "bg-warning-subtle text-warning",
        }.get(self.outcome, "bg-secondary-subtle text-secondary")


class MarketProofExternalEvidence(models.Model):
    CATEGORY_PROVIDER = "provider_production"
    CATEGORY_PILOT = "live_pilot"
    CATEGORY_CASE_STUDY = "client_case_study"
    CATEGORY_STATUTORY = "statutory_filing"
    CATEGORY_BACKUP = "backup_restore"
    CATEGORY_SECURITY = "security_signoff"
    CATEGORY_COMMERCIAL = "commercial_commitment"
    CATEGORY_CHOICES = [
        (CATEGORY_PROVIDER, "Live Provider Credentials"),
        (CATEGORY_PILOT, "Live Pilot Usage"),
        (CATEGORY_CASE_STUDY, "Client Case Study Proof"),
        (CATEGORY_STATUTORY, "Statutory Filing Acknowledgement"),
        (CATEGORY_BACKUP, "Backup / Restore Evidence"),
        (CATEGORY_SECURITY, "Security / Access Signoff"),
        (CATEGORY_COMMERCIAL, "Commercial Commitment"),
    ]

    STATUS_REQUESTED = "requested"
    STATUS_RECEIVED = "received"
    STATUS_VERIFIED = "verified"
    STATUS_REJECTED = "rejected"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Requested"),
        (STATUS_RECEIVED, "Received"),
        (STATUS_VERIFIED, "Verified"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_EXPIRED, "Expired"),
    ]

    SOURCE_CLIENT = "client"
    SOURCE_CA = "ca_team"
    SOURCE_PROVIDER = "provider"
    SOURCE_SYSTEM = "system"
    SOURCE_OTHER = "other"
    SOURCE_CHOICES = [
        (SOURCE_CLIENT, "Client"),
        (SOURCE_CA, "CA Team"),
        (SOURCE_PROVIDER, "Provider"),
        (SOURCE_SYSTEM, "System"),
        (SOURCE_OTHER, "Other"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="market_external_evidence")
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_REQUESTED)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_CA)
    title = models.CharField(max_length=180)
    evidence_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text="Portal ARN, provider ticket, evidence pack id, signed email reference, or internal artifact id.",
    )
    artifact_sha256 = models.CharField(max_length=64, blank=True, verbose_name="Artifact SHA-256")
    evidence_url = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_market_external_evidence",
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_market_external_evidence",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_market_external_evidence",
    )
    follow_up_task = models.ForeignKey(
        PracticeTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="market_external_evidence_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "due_date", "company__name", "category"]
        indexes = [
            models.Index(fields=["company", "category", "status"], name="core_mpe_cmp_cat_stat_idx"),
            models.Index(fields=["status", "due_date"], name="core_mpe_stat_due_idx"),
            models.Index(fields=["expires_on"], name="core_mpe_exp_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} - {self.get_category_display()} - {self.title}"

    @property
    def is_expired(self):
        return bool(self.expires_on and self.expires_on < timezone.localdate())

    @property
    def is_verified(self):
        return self.status == self.STATUS_VERIFIED and not self.is_expired

    @property
    def needs_attention(self):
        return not self.is_verified

    @property
    def status_badge_class(self):
        if self.is_expired:
            return "bg-danger"
        return {
            self.STATUS_REQUESTED: "bg-secondary",
            self.STATUS_RECEIVED: "bg-primary",
            self.STATUS_VERIFIED: "bg-success",
            self.STATUS_REJECTED: "bg-danger",
            self.STATUS_EXPIRED: "bg-danger",
        }.get(self.status, "bg-secondary")


class ComplianceFiling(models.Model):
    TYPE_GSTR1 = "GSTR1"
    TYPE_GSTR3B = "GSTR3B"
    TYPE_GSTR9 = "GSTR9"
    TYPE_GSTR9C = "GSTR9C"
    TYPE_GST_IMS = "GST_IMS"
    TYPE_TDS_PAYMENT = "TDS_PAYMENT"
    TYPE_TDS_24Q = "TDS_24Q"
    TYPE_TDS_26Q = "TDS_26Q"
    TYPE_TDS_27Q = "TDS_27Q"
    TYPE_FORM16 = "FORM16"
    TYPE_ITR = "ITR"
    TYPE_MCA_AOC4 = "MCA_AOC4"
    TYPE_MCA_MGT7 = "MCA_MGT7"
    TYPE_TAX_AUDIT = "TAX_AUDIT"
    TYPE_OTHER = "OTHER"
    FILING_TYPE_CHOICES = [
        (TYPE_GSTR1, "GSTR-1"),
        (TYPE_GSTR3B, "GSTR-3B"),
        (TYPE_GSTR9, "GSTR-9"),
        (TYPE_GSTR9C, "GSTR-9C"),
        (TYPE_GST_IMS, "GST IMS Review"),
        (TYPE_TDS_PAYMENT, "TDS Payment"),
        (TYPE_TDS_24Q, "TDS 24Q"),
        (TYPE_TDS_26Q, "TDS 26Q"),
        (TYPE_TDS_27Q, "TDS 27Q"),
        (TYPE_FORM16, "Form 16"),
        (TYPE_ITR, "ITR"),
        (TYPE_MCA_AOC4, "MCA AOC-4"),
        (TYPE_MCA_MGT7, "MCA MGT-7"),
        (TYPE_TAX_AUDIT, "Tax Audit"),
        (TYPE_OTHER, "Other"),
    ]

    STATUS_NOT_STARTED = "not_started"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_CLIENT_PENDING = "client_pending"
    STATUS_READY_FOR_REVIEW = "ready_for_review"
    STATUS_BLOCKED = "blocked"
    STATUS_FILED = "filed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_NOT_STARTED, "Not Started"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_CLIENT_PENDING, "Client Pending"),
        (STATUS_READY_FOR_REVIEW, "Ready for Review"),
        (STATUS_BLOCKED, "Blocked"),
        (STATUS_FILED, "Filed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    SOURCE_MANUAL = "manual"
    SOURCE_CALENDAR = "calendar"
    SOURCE_PORTAL = "portal"
    SOURCE_IMPORT = "import"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_CALENDAR, "Calendar Template"),
        (SOURCE_PORTAL, "Portal"),
        (SOURCE_IMPORT, "Import"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="compliance_filings")
    filing_type = models.CharField(max_length=30, choices=FILING_TYPE_CHOICES)
    title = models.CharField(max_length=180)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_NOT_STARTED)
    priority = models.CharField(max_length=20, choices=PracticeTask.PRIORITY_CHOICES, default=PracticeTask.PRIORITY_NORMAL)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_compliance_filings",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="review_compliance_filings",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_compliance_filings",
    )
    filed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="filed_compliance_filings",
    )
    related_task = models.ForeignKey(
        PracticeTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compliance_filings",
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    source_reference = models.CharField(max_length=160, blank=True)
    arn_ack_number = models.CharField(max_length=160, blank=True, verbose_name="ARN / Ack Number")
    portal_status = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    review_notes = models.TextField(blank=True)
    filed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "due_date", "company__name", "filing_type"]
        indexes = [
            models.Index(fields=["company", "filing_type", "period_start"], name="core_fil_cmp_typ_per_idx"),
            models.Index(fields=["status", "due_date"], name="core_filing_status_due_idx"),
            models.Index(fields=["assigned_to", "status", "due_date"], name="core_filing_owner_stat_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} - {self.title}"

    @property
    def is_open(self):
        return self.status not in {self.STATUS_FILED, self.STATUS_CANCELLED}

    @property
    def task_type(self):
        if self.filing_type.startswith("GSTR") or self.filing_type == self.TYPE_GST_IMS:
            return PracticeTask.TYPE_GST
        if self.filing_type.startswith("TDS") or self.filing_type == self.TYPE_FORM16:
            return PracticeTask.TYPE_TDS
        if self.filing_type.startswith("MCA"):
            return PracticeTask.TYPE_MCA
        if self.filing_type in {self.TYPE_ITR, self.TYPE_TAX_AUDIT}:
            return PracticeTask.TYPE_ITR
        return PracticeTask.TYPE_OTHER


class ComplianceNotice(models.Model):
    TYPE_GST = "GST"
    TYPE_TDS = "TDS"
    TYPE_INCOME_TAX = "INCOME_TAX"
    TYPE_MCA = "MCA"
    TYPE_AUDIT = "AUDIT"
    TYPE_CLIENT = "CLIENT"
    TYPE_OTHER = "OTHER"
    NOTICE_TYPE_CHOICES = [
        (TYPE_GST, "GST"),
        (TYPE_TDS, "TDS"),
        (TYPE_INCOME_TAX, "Income Tax"),
        (TYPE_MCA, "MCA"),
        (TYPE_AUDIT, "Audit"),
        (TYPE_CLIENT, "Client Query"),
        (TYPE_OTHER, "Other"),
    ]

    STATUS_RECEIVED = "received"
    STATUS_IN_REVIEW = "in_review"
    STATUS_DATA_PENDING = "data_pending"
    STATUS_RESPONSE_READY = "response_ready"
    STATUS_RESPONDED = "responded"
    STATUS_CLOSED = "closed"
    STATUS_ESCALATED = "escalated"
    STATUS_CHOICES = [
        (STATUS_RECEIVED, "Received"),
        (STATUS_IN_REVIEW, "In Review"),
        (STATUS_DATA_PENDING, "Data Pending"),
        (STATUS_RESPONSE_READY, "Response Ready"),
        (STATUS_RESPONDED, "Responded"),
        (STATUS_CLOSED, "Closed"),
        (STATUS_ESCALATED, "Escalated"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="compliance_notices")
    notice_type = models.CharField(max_length=30, choices=NOTICE_TYPE_CHOICES, default=TYPE_OTHER)
    title = models.CharField(max_length=180)
    reference_number = models.CharField(max_length=160, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    response_due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_RECEIVED)
    priority = models.CharField(max_length=20, choices=PracticeTask.PRIORITY_CHOICES, default=PracticeTask.PRIORITY_HIGH)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_compliance_notices",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_compliance_notices",
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_compliance_notices",
    )
    related_task = models.ForeignKey(
        PracticeTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compliance_notices",
    )
    related_filing = models.ForeignKey(
        ComplianceFiling,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notices",
    )
    portal_status = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    response_summary = models.TextField(blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "response_due_date", "-priority", "company__name"]
        indexes = [
            models.Index(fields=["company", "notice_type", "response_due_date"], name="core_notice_cmp_type_due_idx"),
            models.Index(fields=["status", "response_due_date"], name="core_notice_status_due_idx"),
            models.Index(fields=["assigned_to", "status", "response_due_date"], name="core_notice_owner_stat_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} - {self.title}"

    @property
    def is_open(self):
        return self.status not in {self.STATUS_CLOSED}


class GSTPeriodReview(models.Model):
    STATUS_OPEN = "open"
    STATUS_IN_REVIEW = "in_review"
    STATUS_SIGNED_OFF = "signed_off"
    STATUS_REOPENED = "reopened"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_IN_REVIEW, "In Review"),
        (STATUS_SIGNED_OFF, "Signed Off"),
        (STATUS_REOPENED, "Reopened"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="gst_period_reviews")
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    risk_score = models.PositiveSmallIntegerField(default=0)
    summary_snapshot = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prepared_gst_period_reviews",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_gst_period_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("company", "period_start", "period_end")
        ordering = ["-period_start", "company__name"]
        indexes = [
            models.Index(fields=["company", "period_start"], name="core_gst_review_period_idx"),
            models.Index(fields=["status", "period_start"], name="core_gst_review_status_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} GST review {self.period_start:%b %Y}"

    @property
    def is_signed_off(self):
        return self.status == self.STATUS_SIGNED_OFF


class FilingReview(models.Model):
    TYPE_GST_MONTHLY = "GST_MONTHLY"
    TYPE_TDS_QUARTERLY = "TDS_QUARTERLY"
    TYPE_ITR = "ITR"
    TYPE_MCA = "MCA"
    TYPE_TAX_AUDIT = "TAX_AUDIT"
    TYPE_OTHER = "OTHER"
    REVIEW_TYPE_CHOICES = [
        (TYPE_GST_MONTHLY, "GST Monthly"),
        (TYPE_TDS_QUARTERLY, "TDS Quarterly"),
        (TYPE_ITR, "ITR"),
        (TYPE_MCA, "MCA"),
        (TYPE_TAX_AUDIT, "Tax Audit"),
        (TYPE_OTHER, "Other"),
    ]

    STATUS_DRAFT = "draft"
    STATUS_UNDER_REVIEW = "under_review"
    STATUS_REVIEWED = "reviewed"
    STATUS_SENT_BACK = "sent_back"
    STATUS_APPROVED = "approved_for_filing"
    STATUS_REOPENED = "reopened"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_UNDER_REVIEW, "Under Review"),
        (STATUS_REVIEWED, "Reviewed"),
        (STATUS_SENT_BACK, "Sent Back"),
        (STATUS_APPROVED, "Approved for Filing"),
        (STATUS_REOPENED, "Reopened"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="filing_reviews")
    review_type = models.CharField(max_length=30, choices=REVIEW_TYPE_CHOICES, default=TYPE_GST_MONTHLY)
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    readiness_score = models.PositiveSmallIntegerField(default=0)
    risk_score = models.PositiveSmallIntegerField(default=0)
    blocker_snapshot = models.JSONField(default=dict, blank=True)
    waived_blockers = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prepared_filing_reviews",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_filing_reviews",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_filing_reviews",
    )
    sent_back_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_back_filing_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    sent_back_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("company", "review_type", "period_start", "period_end")
        ordering = ["status", "-period_start", "company__name"]
        indexes = [
            models.Index(fields=["company", "review_type", "period_start"], name="core_frev_cmp_typ_per_idx"),
            models.Index(fields=["status", "period_start"], name="core_frev_status_period_idx"),
            models.Index(fields=["approved_by", "approved_at"], name="core_frev_approver_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} {self.get_review_type_display()} review {self.period_start:%b %Y}"

    @property
    def is_approved(self):
        return self.status == self.STATUS_APPROVED


class GSTFilingPack(models.Model):
    STATUS_READY = "ready"
    STATUS_FILED = "filed"
    STATUS_REOPENED = "reopened"
    STATUS_CHOICES = [
        (STATUS_READY, "Ready"),
        (STATUS_FILED, "Filed"),
        (STATUS_REOPENED, "Reopened"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="gst_filing_packs")
    period_start = models.DateField()
    period_end = models.DateField()
    review = models.ForeignKey(
        FilingReview,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gst_filing_packs",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_READY)
    summary_snapshot = models.JSONField(default=dict, blank=True)
    validation_snapshot = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    arn_ack_number = models.CharField(max_length=160, blank=True, verbose_name="ARN / Ack Number")
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_gst_filing_packs",
    )
    filed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="filed_gst_filing_packs",
    )
    filed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("company", "period_start", "period_end")
        ordering = ["-period_start", "company__name"]
        indexes = [
            models.Index(fields=["company", "period_start"], name="core_gst_pack_company_idx"),
            models.Index(fields=["status", "period_start"], name="core_gst_pack_status_idx"),
            models.Index(fields=["filed_by", "filed_at"], name="core_gst_pack_filed_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} GST filing pack {self.period_start:%b %Y}"

    @property
    def is_filed(self):
        return self.status == self.STATUS_FILED


class GSTPostFilingTracker(models.Model):
    STATUS_NOT_CHECKED = "not_checked"
    STATUS_PENDING = "pending"
    STATUS_FILED = "filed"
    STATUS_ACCEPTED = "accepted"
    STATUS_UNDER_NOTICE = "under_notice"
    RETURN_STATUS_CHOICES = [
        (STATUS_NOT_CHECKED, "Not Checked"),
        (STATUS_PENDING, "Pending"),
        (STATUS_FILED, "Filed"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_UNDER_NOTICE, "Under Notice"),
    ]

    IMS_NOT_CHECKED = "not_checked"
    IMS_IN_PROGRESS = "in_progress"
    IMS_COMPLETED = "completed"
    IMS_EXCEPTIONS = "exceptions"
    IMS_STATUS_CHOICES = [
        (IMS_NOT_CHECKED, "Not Checked"),
        (IMS_IN_PROGRESS, "In Progress"),
        (IMS_COMPLETED, "Completed"),
        (IMS_EXCEPTIONS, "Exceptions Open"),
    ]

    PAYMENT_NOT_REQUIRED = "not_required"
    PAYMENT_PENDING = "pending"
    PAYMENT_PAID = "paid"
    PAYMENT_SHORT_PAID = "short_paid"
    PAYMENT_STATUS_CHOICES = [
        (PAYMENT_NOT_REQUIRED, "Not Required"),
        (PAYMENT_PENDING, "Pending"),
        (PAYMENT_PAID, "Paid"),
        (PAYMENT_SHORT_PAID, "Short Paid"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="gst_post_filing_trackers")
    period_start = models.DateField()
    period_end = models.DateField()
    pack = models.OneToOneField(
        GSTFilingPack,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="post_filing_tracker",
    )
    gstr1_status = models.CharField(max_length=20, choices=RETURN_STATUS_CHOICES, default=STATUS_NOT_CHECKED)
    gstr1_arn = models.CharField(max_length=160, blank=True, verbose_name="GSTR-1 ARN")
    gstr1_filed_at = models.DateTimeField(null=True, blank=True)
    gstr3b_status = models.CharField(max_length=20, choices=RETURN_STATUS_CHOICES, default=STATUS_NOT_CHECKED)
    gstr3b_arn = models.CharField(max_length=160, blank=True, verbose_name="GSTR-3B ARN")
    gstr3b_filed_at = models.DateTimeField(null=True, blank=True)
    ims_status = models.CharField(max_length=20, choices=IMS_STATUS_CHOICES, default=IMS_NOT_CHECKED)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default=PAYMENT_PENDING)
    payment_challan_reference = models.CharField(max_length=160, blank=True)
    payment_date = models.DateField(null=True, blank=True)
    itc_at_risk = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    portal_evidence_reference = models.CharField(
        max_length=240,
        blank=True,
        help_text="ARN, acknowledgement PDF reference, challan CIN, or evidence vault path.",
    )
    notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_gst_post_filing_trackers",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("company", "period_start", "period_end")
        ordering = ["-period_start", "company__name"]
        indexes = [
            models.Index(fields=["company", "period_start"], name="core_gst_post_cmp_period_idx"),
            models.Index(fields=["gstr1_status", "gstr3b_status"], name="core_gst_post_return_idx"),
            models.Index(fields=["payment_status", "period_start"], name="core_gst_post_pay_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} GST post-filing {self.period_start:%b %Y}"


def gst_evidence_upload_path(instance, filename):
    return os.path.join(
        "gst_evidence",
        str(instance.company_id),
        instance.period_start.strftime("%Y-%m") if instance.period_start else "unperioded",
        filename,
    )


class GSTEvidenceDocument(models.Model):
    TYPE_GSTR1_ACK = "gstr1_ack"
    TYPE_GSTR3B_ACK = "gstr3b_ack"
    TYPE_CHALLAN = "challan"
    TYPE_NOTICE = "notice"
    TYPE_RESPONSE = "response"
    TYPE_DRC03 = "drc03"
    TYPE_OTHER = "other"
    EVIDENCE_TYPE_CHOICES = [
        (TYPE_GSTR1_ACK, "GSTR-1 Acknowledgement"),
        (TYPE_GSTR3B_ACK, "GSTR-3B Acknowledgement"),
        (TYPE_CHALLAN, "GST Challan / CIN"),
        (TYPE_NOTICE, "GST Notice"),
        (TYPE_RESPONSE, "Notice Response"),
        (TYPE_DRC03, "DRC-03"),
        (TYPE_OTHER, "Other Evidence"),
    ]

    RETURN_GSTR1 = "GSTR1"
    RETURN_GSTR3B = "GSTR3B"
    RETURN_IMS = "GST_IMS"
    RETURN_NOTICE = "NOTICE"
    RETURN_OTHER = "OTHER"
    RETURN_TYPE_CHOICES = [
        (RETURN_GSTR1, "GSTR-1"),
        (RETURN_GSTR3B, "GSTR-3B"),
        (RETURN_IMS, "GST IMS / 2B"),
        (RETURN_NOTICE, "Notice"),
        (RETURN_OTHER, "Other"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="gst_evidence_documents")
    period_start = models.DateField()
    period_end = models.DateField()
    tracker = models.ForeignKey(
        GSTPostFilingTracker,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="evidence_documents",
    )
    pack = models.ForeignKey(
        GSTFilingPack,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="evidence_documents",
    )
    filing = models.ForeignKey(
        ComplianceFiling,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gst_evidence_documents",
    )
    notice = models.ForeignKey(
        ComplianceNotice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gst_evidence_documents",
    )
    evidence_type = models.CharField(max_length=30, choices=EVIDENCE_TYPE_CHOICES, default=TYPE_OTHER)
    return_type = models.CharField(max_length=30, choices=RETURN_TYPE_CHOICES, default=RETURN_OTHER)
    title = models.CharField(max_length=180)
    file = models.FileField(upload_to=gst_evidence_upload_path)
    external_reference = models.CharField(max_length=180, blank=True)
    arn_ack_number = models.CharField(max_length=160, blank=True, verbose_name="ARN / Ack Number")
    challan_reference = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_gst_evidence_documents",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["company", "period_start"], name="core_gst_evid_cmp_period_idx"),
            models.Index(fields=["evidence_type", "return_type"], name="core_gst_evid_type_idx"),
            models.Index(fields=["arn_ack_number"], name="core_gst_evid_arn_idx"),
        ]

    def __str__(self):
        return f"{self.company.name} - {self.title}"


class UserCompanyAccess(models.Model):
    ROLE_CHOICES = [
        ("Admin", "Admin"),
        ("Accountant", "Accountant"),
        ("Viewer", "Viewer"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="company_access",
    )
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="user_access",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="Accountant")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "User Company Access"
        verbose_name_plural = "User Company Access"
        unique_together = ("user", "company")
        ordering = ["company__name"]

    def __str__(self):
        return f"{self.user.email} → {self.company.name} [{self.role}]"


class ImmutableAuditLogQuerySet(models.QuerySet):
    def delete(self):
        raise ValidationError("Audit logs cannot be bulk deleted.")


class AuditLog(models.Model):
    """
    Immutable record of every create / update / delete action on business objects.
    MCA compliant: captures before/after state.
    """
    ACTION_CREATE = "create"
    ACTION_UPDATE = "update"
    ACTION_DELETE = "delete"
    ACTION_CHOICES = [
        (ACTION_CREATE, "Created"),
        (ACTION_UPDATE, "Updated"),
        (ACTION_DELETE, "Deleted"),
    ]

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE,
        related_name="audit_logs",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    model_name = models.CharField(max_length=50, db_index=True)
    record_id = models.PositiveIntegerField(db_index=True, default=0)
    object_repr = models.CharField(max_length=200)
    old_data = models.JSONField(default=dict, blank=True, null=True)
    new_data = models.JSONField(default=dict, blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = ImmutableAuditLogQuerySet.as_manager()

    class Meta:
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
        ordering = ["-timestamp"]
        index_together = [["model_name", "record_id"]]

    def __str__(self):
        return f"{self.action.upper()} {self.model_name} #{self.record_id} by {self.user} @ {self.timestamp}"

    def delete(self, *args, **kwargs):
        # MCA requirement: Audit logs must not be deletable
        raise ValidationError("Audit logs cannot be deleted.")


# ─────────────────────────────────────────────────────────────────────────────
# Bank Reconciliation
# ─────────────────────────────────────────────────────────────────────────────

class BankStatement(models.Model):
    """
    A bank statement upload — one file per upload batch.
    Links to the company's bank-account Ledger so debits/credits can be
    matched against VoucherItems on that ledger.
    """
    company         = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="bank_statements"
    )
    # The ledger that represents this bank account (e.g. "SBI Current Account")
    account_ledger  = models.ForeignKey(
        "ledger.Ledger",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="bank_statements",
        verbose_name="Bank Account Ledger",
    )
    statement_date  = models.DateField(
        help_text="Period covered — usually the last date of the statement.",
    )
    uploaded_at     = models.DateTimeField(auto_now_add=True)
    notes           = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name        = "Bank Statement"
        verbose_name_plural = "Bank Statements"
        ordering            = ["-statement_date", "-uploaded_at"]

    def __str__(self):
        account = self.account_ledger.name if self.account_ledger_id else "—"
        return f"{account} — {self.statement_date}"

    @property
    def total_rows(self):
        return self.rows.count()

    @property
    def reconciled_rows(self):
        return self.rows.filter(is_reconciled=True).count()

    @property
    def pending_rows(self):
        return max(self.total_rows - self.reconciled_rows, 0)


class BankStatementRow(models.Model):
    """
    One transaction row from a bank statement CSV.

    Columns mapped (flexible, case-insensitive):
      date | description / narration | debit | credit | balance
    """
    statement   = models.ForeignKey(
        BankStatement, on_delete=models.CASCADE, related_name="rows"
    )
    date        = models.DateField()
    description = models.CharField(max_length=500)
    debit       = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
        help_text="Amount withdrawn / paid out.",
    )
    credit      = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
        help_text="Amount deposited / received.",
    )
    balance     = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True,
        help_text="Running balance from the bank statement.",
    )
    is_reconciled   = models.BooleanField(default=False)
    matched_voucher = models.ForeignKey(
        "vouchers.Voucher",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="bank_statement_matches",
        help_text="Voucher this row was matched / reconciled against.",
    )
    suggested_ledger = models.ForeignKey(
        "ledger.Ledger",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="bank_statement_suggestions",
        help_text="Intelligently suggested ledger based on description matching.",
    )
    match_confidence = models.PositiveSmallIntegerField(
        default=0,
        help_text="0-100 confidence score from bank reconciliation matching.",
    )
    match_reason = models.CharField(
        max_length=160,
        blank=True,
        help_text="Short explanation for the match or suggestion.",
    )
    potential_duplicate = models.BooleanField(
        default=False,
        help_text="Flags rows that look duplicated across uploaded bank statements.",
    )
    duplicate_group_key = models.CharField(
        max_length=160,
        blank=True,
        db_index=True,
        help_text="Internal duplicate-detection grouping key.",
    )
    row_number  = models.PositiveIntegerField(default=0, help_text="Row order in the CSV.")

    class Meta:
        verbose_name        = "Bank Statement Row"
        verbose_name_plural = "Bank Statement Rows"
        ordering            = ["row_number"]

    def __str__(self):
        return f"{self.date} | {self.description[:50]} | Dr {self.debit} Cr {self.credit}"

    @property
    def amount(self):
        """Net amount: credit is positive, debit is negative."""
        from decimal import Decimal
        return self.credit - self.debit
