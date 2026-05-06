"""
core/forms.py
Forms for core app — company creation and company settings.
"""

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from core.phone import normalize_phone_number
from core.upload_validation import BANK_STATEMENT_EXTENSIONS, validate_uploaded_file
from .models import (
    ClientEngagement,
    Company,
    CompanyStatutoryProfile,
    ComplianceFiling,
    ComplianceNotice,
    MarketProofCaseStudy,
    MarketProofExternalEvidence,
    PilotFeedback,
    PracticeTask,
    StatutoryRuleOverride,
)


class CompanySettingsForm(forms.ModelForm):
    """
    Lets an Admin update both the company profile (name, GSTIN, address)
    and the banking / UPI payment details added in Phase 6.
    """

    class Meta:
        model = Company
        fields = [
            # ── Profile ───────────────────────────────────────────────────────
            "name",
            "short_code",
            "gstin",
            "tan",
            "tds_responsible_person",
            "tds_responsible_designation",
            "address",
            "financial_year_start",
            # ── Banking & UPI ─────────────────────────────────────────────────
            "upi_id",
            "bank_name",
            "account_number",
            "ifsc_code",
        ]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. Acme Trading Co.",
            }),
            "short_code": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "ATC (auto-generated if blank)",
                "maxlength": "6",
            }),
            "gstin": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "22AAAAA0000A1Z5",
                "maxlength": "15",
            }),
            "tan": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "MUMA12345A",
                "maxlength": "10",
            }),
            "tds_responsible_person": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. Jay Chauhan",
            }),
            "tds_responsible_designation": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. Partner / Director",
            }),
            "address": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Registered office address (shown on invoices)",
            }),
            "financial_year_start": forms.DateInput(attrs={
                "class": "form-control",
                "type": "date",
            }),
            # Banking
            "upi_id": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. business@ybl",
            }),
            "bank_name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. State Bank of India",
            }),
            "account_number": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. 00000123456789",
            }),
            "ifsc_code": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. SBIN0001234",
                "maxlength": "11",
            }),
        }
        labels = {
            "short_code":          "Voucher Prefix",
            "financial_year_start": "Financial Year Start",
        }
        help_texts = {
            "short_code": "Up to 6 characters used as prefix in voucher numbers (e.g. ABC → ABC2526-00001).",
            "tan": "Required for quarterly TDS return workpapers and TRACES tracking.",
            "upi_id":     "Setting this enables a Pay-Now QR code on all Sales invoices.",
            "ifsc_code":  "11-character code printed on invoices (e.g. SBIN0001234).",
        }

    def clean_gstin(self):
        gstin = self.cleaned_data.get("gstin", "").strip().upper()
        if gstin and len(gstin) != 15:
            raise forms.ValidationError("GSTIN must be exactly 15 characters.")
        return gstin or None

    def clean_ifsc_code(self):
        ifsc = self.cleaned_data.get("ifsc_code", "").strip().upper()
        if ifsc and len(ifsc) != 11:
            raise forms.ValidationError("IFSC code must be exactly 11 characters.")
        return ifsc or None

    def clean_tan(self):
        tan = self.cleaned_data.get("tan", "").strip().upper()
        if tan and len(tan) != 10:
            raise forms.ValidationError("TAN must be exactly 10 characters.")
        return tan or None

    def clean_short_code(self):
        sc = self.cleaned_data.get("short_code", "").strip().upper()
        return sc or None

class AppSettingsForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = [
            "whatsapp_intake_number",
            "e_invoice_enabled",
            "e_invoice_aato_crore",
            "e_invoice_reporting_deadline_days",
            "e_invoice_warning_days",
            "invoice_email_from_name",
            "invoice_email_from_address",
            "invoice_email_reply_to",
            "invoice_email_subject",
            "invoice_email_body",
            "payment_reminder_email_subject",
            "payment_reminder_email_body",
        ]
        widgets = {
            "whatsapp_intake_number": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. +919876543210",
                "inputmode": "tel",
            }),
            "e_invoice_enabled": forms.CheckboxInput(attrs={
                "class": "form-check-input",
            }),
            "e_invoice_aato_crore": forms.NumberInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. 12.50",
                "step": "0.01",
                "min": "0",
            }),
            "e_invoice_reporting_deadline_days": forms.NumberInput(attrs={
                "class": "form-control",
                "min": "1",
                "max": "365",
            }),
            "e_invoice_warning_days": forms.NumberInput(attrs={
                "class": "form-control",
                "min": "0",
                "max": "365",
            }),
            "invoice_email_from_name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. Akshaya Vistara Accounts",
            }),
            "invoice_email_from_address": forms.EmailInput(attrs={
                "class": "form-control",
                "placeholder": "accounts@example.com",
            }),
            "invoice_email_reply_to": forms.EmailInput(attrs={
                "class": "form-control",
                "placeholder": "billing@example.com",
            }),
            "invoice_email_subject": forms.TextInput(attrs={
                "class": "form-control",
            }),
            "invoice_email_body": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 7,
            }),
            "payment_reminder_email_subject": forms.TextInput(attrs={
                "class": "form-control",
            }),
            "payment_reminder_email_body": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 8,
            }),
        }
        labels = {
            "whatsapp_intake_number": "Client WhatsApp Number",
            "e_invoice_enabled": "Enable E-Invoice Watch",
            "e_invoice_aato_crore": "AATO (Rs. crore)",
            "e_invoice_reporting_deadline_days": "IRP Deadline Days",
            "e_invoice_warning_days": "Warn After Days",
            "invoice_email_from_name": "Sender Name",
            "invoice_email_from_address": "Sender Email",
            "invoice_email_reply_to": "Reply-To Email",
            "invoice_email_subject": "Default Subject",
            "invoice_email_body": "Default Message",
            "payment_reminder_email_subject": "Reminder Subject",
            "payment_reminder_email_body": "Reminder Message",
        }
        help_texts = {
            "whatsapp_intake_number": (
                "Business WhatsApp number clients should send bills/documents to. "
                "Incoming webhooks can use this number to identify the company when no portal token is supplied."
            ),
            "e_invoice_enabled": "Use for clients where GST e-invoicing applies. The current IRP restriction is commonly tracked as 30 days for AATO Rs.10 crore and above.",
            "e_invoice_aato_crore": "Optional. Keep it visible for CA review and client onboarding.",
            "e_invoice_reporting_deadline_days": "Default 30 days from invoice date.",
            "e_invoice_warning_days": "Default 25 days, so staff see invoices before the IRP window closes.",
            "invoice_email_from_address": "Leave blank to use the platform default sender.",
            "invoice_email_reply_to": "Leave blank to receive replies at the sender address.",
            "invoice_email_subject": "Use {voucher_number}, {company_name}, {client_name}, and {amount}.",
            "invoice_email_body": "Use {voucher_number}, {company_name}, {client_name}, and {amount}.",
            "payment_reminder_email_subject": (
                "Use {voucher_number}, {company_name}, {client_name}, {amount}, "
                "{outstanding}, {due_date}, and {aging_line}."
            ),
            "payment_reminder_email_body": (
                "Use {voucher_number}, {company_name}, {client_name}, {amount}, "
                "{outstanding}, {due_date}, and {aging_line}."
            ),
        }

    def clean_whatsapp_intake_number(self):
        try:
            return normalize_phone_number(self.cleaned_data.get("whatsapp_intake_number"))
        except ValueError as exc:
            raise ValidationError(str(exc))

    def clean(self):
        cleaned_data = super().clean()
        deadline_days = cleaned_data.get("e_invoice_reporting_deadline_days")
        warning_days = cleaned_data.get("e_invoice_warning_days")

        if deadline_days is not None and deadline_days < 1:
            self.add_error("e_invoice_reporting_deadline_days", "Deadline must be at least 1 day.")
        if warning_days is not None and warning_days < 0:
            self.add_error("e_invoice_warning_days", "Warning day cannot be negative.")
        if deadline_days is not None and warning_days is not None and warning_days > deadline_days:
            self.add_error("e_invoice_warning_days", "Warning day cannot be greater than the reporting deadline.")
        return cleaned_data


# ─────────────────────────────────────────────────────────────────────────────
# Bank Statement Upload form
# ─────────────────────────────────────────────────────────────────────────────

class CompanyStatutoryProfileForm(forms.ModelForm):
    class Meta:
        model = CompanyStatutoryProfile
        fields = [
            "gst_registered",
            "gst_return_frequency",
            "gstr1_frequency",
            "qrmp_group",
            "gstr1_monthly_due_day",
            "gstr1_quarterly_due_day",
            "gstr3b_monthly_due_day",
            "gstr3b_qrmp_due_day",
            "gst_late_fee_per_day",
            "gst_nil_late_fee_per_day",
            "gst_interest_rate_percent",
            "tds_applicable",
            "tds_24q_enabled",
            "tds_26q_enabled",
            "tds_27q_enabled",
            "tds_deposit_due_day",
            "tds_march_deposit_due_day",
            "tds_deposit_interest_rate_percent_per_month",
            "tds_return_late_fee_per_day",
            "msme_watch_enabled",
            "msme_default_credit_days",
            "msme_interest_rate_percent",
            "due_date_grace_days",
            "rules_notes",
        ]
        widgets = {
            "gst_registered": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "gst_return_frequency": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "gstr1_frequency": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "qrmp_group": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "gstr1_monthly_due_day": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1, "max": 31}),
            "gstr1_quarterly_due_day": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1, "max": 31}),
            "gstr3b_monthly_due_day": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1, "max": 31}),
            "gstr3b_qrmp_due_day": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1, "max": 31}),
            "gst_late_fee_per_day": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01", "min": 0}),
            "gst_nil_late_fee_per_day": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01", "min": 0}),
            "gst_interest_rate_percent": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01", "min": 0}),
            "tds_applicable": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "tds_24q_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "tds_26q_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "tds_27q_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "tds_deposit_due_day": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1, "max": 31}),
            "tds_march_deposit_due_day": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1, "max": 31}),
            "tds_deposit_interest_rate_percent_per_month": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01", "min": 0}),
            "tds_return_late_fee_per_day": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01", "min": 0}),
            "msme_watch_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "msme_default_credit_days": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1, "max": 90}),
            "msme_interest_rate_percent": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01", "min": 0}),
            "due_date_grace_days": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0, "max": 30}),
            "rules_notes": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 3}),
        }
        labels = {
            "gst_registered": "GST Registered",
            "gst_return_frequency": "GSTR-3B Frequency",
            "gstr1_frequency": "GSTR-1 Frequency",
            "qrmp_group": "QRMP Group",
            "gst_late_fee_per_day": "GST Late Fee / Day",
            "gst_nil_late_fee_per_day": "Nil Return Late Fee / Day",
            "gst_interest_rate_percent": "GST Interest %",
            "tds_applicable": "TDS Applicable",
            "tds_deposit_due_day": "TDS Deposit Day",
            "tds_march_deposit_due_day": "March TDS Deposit Day",
            "tds_deposit_interest_rate_percent_per_month": "TDS Interest % / Month",
            "tds_return_late_fee_per_day": "TDS Return Fee / Day",
            "msme_watch_enabled": "MSME Watch",
            "msme_default_credit_days": "Default MSME Credit Days",
            "msme_interest_rate_percent": "MSME Interest %",
            "due_date_grace_days": "Internal Grace Days",
            "rules_notes": "CA Notes",
        }
        help_texts = {
            "due_date_grace_days": "Internal triage buffer only. It does not change statutory due dates.",
            "rules_notes": "Document client-specific assumptions, notifications, and reviewer decisions.",
        }

    def clean(self):
        cleaned_data = super().clean()
        qrmp_group = cleaned_data.get("qrmp_group")
        if qrmp_group == CompanyStatutoryProfile.QRMP_GROUP_A:
            cleaned_data["gstr3b_qrmp_due_day"] = 22
        elif qrmp_group == CompanyStatutoryProfile.QRMP_GROUP_B:
            cleaned_data["gstr3b_qrmp_due_day"] = 24
        return cleaned_data


class StatutoryRuleOverrideForm(forms.ModelForm):
    class Meta:
        model = StatutoryRuleOverride
        fields = [
            "rule_type",
            "period_start",
            "period_end",
            "original_due_date",
            "override_due_date",
            "late_fee_per_day",
            "interest_rate_percent",
            "reason",
        ]
        widgets = {
            "rule_type": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "period_start": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "period_end": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "original_due_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "override_due_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "late_fee_per_day": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01", "min": 0}),
            "interest_rate_percent": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01", "min": 0}),
            "reason": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 2}),
        }
        labels = {
            "rule_type": "Rule",
            "period_start": "Period From",
            "period_end": "Period To",
            "original_due_date": "Old Due Date",
            "override_due_date": "New Due Date",
            "late_fee_per_day": "Late Fee / Day",
            "interest_rate_percent": "Interest %",
            "reason": "Reason / Notification",
        }
        help_texts = {
            "period_start": "Leave blank to make the override open-ended for this rule.",
            "reason": "Add notification reference, CA judgement, or client-specific reason.",
        }


class BankStatementForm(forms.ModelForm):
    statement_file = forms.FileField(
        label="Bank Statement File",
        widget=forms.ClearableFileInput(attrs={
            "class": "form-control",
            "accept": ".csv,.xlsx,.pdf,.png,.jpg,.jpeg"
        }),
        help_text="Upload a CSV, XLSX, PDF or Image from your bank. "
                  "For PDF/Image, we will use OCR to extract Date, Description, Debit, and Credit.",
    )

    class Meta:
        from .models import BankStatement
        model  = BankStatement
        fields = ["account_ledger", "statement_date", "notes"]
        widgets = {
            "account_ledger": forms.Select(attrs={"class": "form-select"}),
            "statement_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "notes": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Optional notes (e.g. April 2024 SBI Statement)",
            }),
        }
        labels = {
            "account_ledger": "Bank Account Ledger",
            "statement_date": "Statement Date",
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        from ledger.models import Ledger
        if company:
            # Offer ledgers whose name contains common bank keywords, or show all
            self.fields["account_ledger"].queryset = Ledger.objects.filter(
                company=company, is_active=True
            ).order_by("name")
        else:
            self.fields["account_ledger"].queryset = Ledger.objects.none()
        self.fields["account_ledger"].required  = False
        self.fields["account_ledger"].empty_label = "— Select Bank Account Ledger (optional) —"
        self.fields["notes"].required           = False

    def clean_statement_file(self):
        file_obj = self.cleaned_data.get("statement_file")
        try:
            return validate_uploaded_file(
                file_obj,
                allowed_extensions=BANK_STATEMENT_EXTENSIONS,
                max_mb=20,
            )
        except Exception as exc:
            raise forms.ValidationError(str(exc))


class PracticeTaskForm(forms.ModelForm):
    class Meta:
        model = PracticeTask
        fields = [
            "company",
            "title",
            "task_type",
            "priority",
            "status",
            "due_date",
            "period_start",
            "period_end",
            "assigned_to",
            "reference",
            "description",
        ]
        widgets = {
            "company": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. File GSTR-3B for April"}),
            "task_type": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "due_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "period_start": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "period_end": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "reference": forms.TextInput(attrs={"class": "form-control", "placeholder": "Notice no / filing ref"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, companies=None, users=None, **kwargs):
        super().__init__(*args, **kwargs)
        if companies is not None:
            self.fields["company"].queryset = companies
        if users is not None:
            self.fields["assigned_to"].queryset = users
        self.fields["assigned_to"].required = False
        self.fields["assigned_to"].empty_label = "Unassigned"
        self.fields["description"].required = False
        self.fields["reference"].required = False
        self.fields["period_start"].required = False
        self.fields["period_end"].required = False


class PilotFeedbackForm(forms.ModelForm):
    create_follow_up_task = forms.BooleanField(
        required=False,
        initial=True,
        label="Create follow-up task",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = PilotFeedback
        fields = [
            "company",
            "feedback_type",
            "sentiment",
            "confidence_score",
            "severity",
            "status",
            "occurred_on",
            "assigned_to",
            "client_contact",
            "competitor_reference",
            "evidence_reference",
            "summary",
            "detail",
        ]
        widgets = {
            "company": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "feedback_type": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "sentiment": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "confidence_score": forms.NumberInput(attrs={
                "class": "form-control form-control-sm",
                "min": "0",
                "max": "10",
                "step": "1",
            }),
            "severity": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "status": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "occurred_on": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "assigned_to": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "client_contact": forms.TextInput(attrs={
                "class": "form-control form-control-sm",
                "placeholder": "Name, email, phone, or role",
            }),
            "competitor_reference": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "evidence_reference": forms.TextInput(attrs={
                "class": "form-control form-control-sm",
                "placeholder": "Call note, ticket id, quote ref, or evidence link",
            }),
            "summary": forms.TextInput(attrs={
                "class": "form-control form-control-sm",
                "placeholder": "Short client signal or blocker",
            }),
            "detail": forms.Textarea(attrs={
                "class": "form-control form-control-sm",
                "rows": 4,
                "placeholder": "What did the CA/client say, what failed, what should happen next?",
            }),
        }
        labels = {
            "confidence_score": "Confidence",
            "client_contact": "Client Contact",
            "competitor_reference": "Competitor / Current Tool",
            "evidence_reference": "Evidence Reference",
            "occurred_on": "Date",
        }
        help_texts = {
            "confidence_score": "0 means no confidence; 10 means ready to replace the old workflow.",
            "evidence_reference": "Use a call note, support ticket, recording, uploaded file reference, or client quote reference.",
        }

    def __init__(self, *args, companies=None, users=None, **kwargs):
        super().__init__(*args, **kwargs)
        if companies is not None:
            self.fields["company"].queryset = companies
        if users is not None:
            self.fields["assigned_to"].queryset = users
        self.fields["assigned_to"].required = False
        self.fields["assigned_to"].empty_label = "Unassigned"
        self.fields["client_contact"].required = False
        self.fields["competitor_reference"].required = False
        self.fields["evidence_reference"].required = False
        self.fields["detail"].required = False


class MarketProofCaseStudyForm(forms.ModelForm):
    create_follow_up_task = forms.BooleanField(
        required=False,
        initial=True,
        label="Create proof follow-up task",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = MarketProofCaseStudy
        fields = [
            "company",
            "title",
            "status",
            "outcome",
            "migration_source",
            "client_contact",
            "client_role",
            "testimonial_quote",
            "publish_consent",
            "anonymized",
            "consent_reference",
            "evidence_reference",
            "before_process_hours",
            "after_process_hours",
            "monthly_documents",
            "monthly_invoices",
            "gst_periods_completed",
            "tally_parallel_run_days",
            "cutover_date",
            "commercial_value",
            "value_summary",
            "owner",
        ]
        widgets = {
            "company": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "title": forms.TextInput(attrs={
                "class": "form-control form-control-sm",
                "placeholder": "e.g. Tally to Akshaya GST close pilot",
            }),
            "status": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "outcome": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "migration_source": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "client_contact": forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "Name or approval contact"}),
            "client_role": forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "Owner / accountant / finance lead"}),
            "testimonial_quote": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 4}),
            "publish_consent": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "anonymized": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "consent_reference": forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "Email, ticket, or signed consent ref"}),
            "evidence_reference": forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "Call note, evidence pack, dashboard, or recording ref"}),
            "before_process_hours": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.25", "min": "0"}),
            "after_process_hours": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.25", "min": "0"}),
            "monthly_documents": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": "0"}),
            "monthly_invoices": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": "0"}),
            "gst_periods_completed": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": "0"}),
            "tally_parallel_run_days": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": "0"}),
            "cutover_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "commercial_value": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01", "min": "0"}),
            "value_summary": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 3}),
            "owner": forms.Select(attrs={"class": "form-select form-select-sm"}),
        }
        labels = {
            "migration_source": "Replaced Tool",
            "publish_consent": "Publish Consent",
            "anonymized": "Use Anonymized Name",
            "before_process_hours": "Before Hours",
            "after_process_hours": "After Hours",
            "gst_periods_completed": "GST Periods",
            "tally_parallel_run_days": "Parallel Run Days",
            "commercial_value": "Commercial Value",
        }

    def __init__(self, *args, companies=None, users=None, **kwargs):
        super().__init__(*args, **kwargs)
        if companies is not None:
            self.fields["company"].queryset = companies
        if users is not None:
            self.fields["owner"].queryset = users
        self.fields["owner"].required = False
        self.fields["owner"].empty_label = "Unassigned"
        for field_name in [
            "client_contact",
            "client_role",
            "testimonial_quote",
            "consent_reference",
            "evidence_reference",
            "before_process_hours",
            "after_process_hours",
            "cutover_date",
            "value_summary",
        ]:
            self.fields[field_name].required = False

    def clean(self):
        cleaned = super().clean()
        before = cleaned.get("before_process_hours")
        after = cleaned.get("after_process_hours")
        if before is not None and after is not None and after > before:
            self.add_error("after_process_hours", "After hours should not be greater than before hours.")
        return cleaned


class MarketProofExternalEvidenceForm(forms.ModelForm):
    create_follow_up_task = forms.BooleanField(
        required=False,
        initial=True,
        label="Create follow-up task if not verified",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = MarketProofExternalEvidence
        fields = [
            "company",
            "category",
            "status",
            "source",
            "title",
            "evidence_reference",
            "artifact_sha256",
            "evidence_url",
            "notes",
            "due_date",
            "expires_on",
            "owner",
        ]
        widgets = {
            "company": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "category": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "status": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "source": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "title": forms.TextInput(attrs={
                "class": "form-control form-control-sm",
                "placeholder": "e.g. GST provider production credential approval",
            }),
            "evidence_reference": forms.TextInput(attrs={
                "class": "form-control form-control-sm",
                "placeholder": "ARN, provider ticket, pack id, signed email, or artifact id",
            }),
            "artifact_sha256": forms.TextInput(attrs={
                "class": "form-control form-control-sm",
                "placeholder": "64-character SHA-256 if an artifact is archived",
            }),
            "evidence_url": forms.URLInput(attrs={
                "class": "form-control form-control-sm",
                "placeholder": "https://...",
            }),
            "notes": forms.Textarea(attrs={
                "class": "form-control form-control-sm",
                "rows": 3,
                "placeholder": "What was verified, who supplied it, and what remains open?",
            }),
            "due_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "expires_on": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "owner": forms.Select(attrs={"class": "form-select form-select-sm"}),
        }
        labels = {
            "artifact_sha256": "Artifact SHA-256",
            "evidence_url": "Evidence URL",
            "expires_on": "Expires On",
        }

    def __init__(self, *args, companies=None, users=None, **kwargs):
        super().__init__(*args, **kwargs)
        if companies is not None:
            self.fields["company"].queryset = companies
        if users is not None:
            self.fields["owner"].queryset = users
        self.fields["owner"].required = False
        self.fields["owner"].empty_label = "Unassigned"
        for field_name in ["evidence_reference", "artifact_sha256", "evidence_url", "notes", "due_date", "expires_on"]:
            self.fields[field_name].required = False

    def clean_artifact_sha256(self):
        value = (self.cleaned_data.get("artifact_sha256") or "").strip().lower()
        if value and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
            raise ValidationError("Enter a valid 64-character SHA-256 hash.")
        return value

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get("status")
        reference = (cleaned.get("evidence_reference") or "").strip()
        artifact_hash = (cleaned.get("artifact_sha256") or "").strip()
        evidence_url = (cleaned.get("evidence_url") or "").strip()
        if status == MarketProofExternalEvidence.STATUS_VERIFIED and not (reference or artifact_hash or evidence_url):
            raise ValidationError("Verified evidence needs a reference, URL, or SHA-256 artifact hash.")
        return cleaned


class ClientEngagementForm(forms.ModelForm):
    class Meta:
        model = ClientEngagement
        fields = [
            "status",
            "service_package",
            "monthly_retainer",
            "billing_cycle",
            "renewal_date",
            "partner_owner",
            "manager_owner",
            "risk_rating",
            "scope_summary",
            "out_of_scope",
            "internal_notes",
            "last_reviewed_at",
        ]
        widgets = {
            "status": forms.Select(attrs={"class": "form-select"}),
            "service_package": forms.Select(attrs={"class": "form-select"}),
            "monthly_retainer": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "billing_cycle": forms.Select(attrs={"class": "form-select"}),
            "renewal_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "partner_owner": forms.Select(attrs={"class": "form-select"}),
            "manager_owner": forms.Select(attrs={"class": "form-select"}),
            "risk_rating": forms.Select(attrs={"class": "form-select"}),
            "scope_summary": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "out_of_scope": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "internal_notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "last_reviewed_at": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        }

    def __init__(self, *args, users=None, **kwargs):
        super().__init__(*args, **kwargs)
        if users is not None:
            self.fields["partner_owner"].queryset = users
            self.fields["manager_owner"].queryset = users
        self.fields["partner_owner"].required = False
        self.fields["manager_owner"].required = False
        self.fields["partner_owner"].empty_label = "Unassigned"
        self.fields["manager_owner"].empty_label = "Unassigned"
        for field_name in ["renewal_date", "scope_summary", "out_of_scope", "internal_notes", "last_reviewed_at"]:
            self.fields[field_name].required = False


class ComplianceFilingForm(forms.ModelForm):
    class Meta:
        model = ComplianceFiling
        fields = [
            "company",
            "filing_type",
            "title",
            "status",
            "priority",
            "period_start",
            "period_end",
            "due_date",
            "assigned_to",
            "reviewer",
            "arn_ack_number",
            "portal_status",
            "notes",
            "review_notes",
        ]
        widgets = {
            "company": forms.Select(attrs={"class": "form-select"}),
            "filing_type": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. GSTR-3B - April 2026"}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "period_start": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "period_end": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "due_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "reviewer": forms.Select(attrs={"class": "form-select"}),
            "arn_ack_number": forms.TextInput(attrs={"class": "form-control", "placeholder": "ARN, acknowledgement, or challan no."}),
            "portal_status": forms.TextInput(attrs={"class": "form-control", "placeholder": "Portal status"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "review_notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, companies=None, users=None, **kwargs):
        super().__init__(*args, **kwargs)
        if companies is not None:
            self.fields["company"].queryset = companies
        if users is not None:
            self.fields["assigned_to"].queryset = users
            self.fields["reviewer"].queryset = users
        self.fields["assigned_to"].required = False
        self.fields["assigned_to"].empty_label = "Unassigned"
        self.fields["reviewer"].required = False
        self.fields["reviewer"].empty_label = "No reviewer"
        for name in ("period_start", "period_end", "due_date", "arn_ack_number", "portal_status", "notes", "review_notes"):
            self.fields[name].required = False


class ComplianceCalendarGenerationForm(forms.Form):
    companies = forms.ModelMultipleChoiceField(
        queryset=Company.objects.none(),
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": 8}),
        label="Clients",
    )
    from_date = forms.DateField(
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"class": "form-control", "type": "date"}),
        input_formats=["%Y-%m-%d"],
        label="First period month",
    )
    months = forms.IntegerField(
        min_value=1,
        max_value=12,
        initial=3,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 12}),
        label="Months to generate",
    )
    assigned_to = forms.ModelChoiceField(
        queryset=get_user_model().objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Owner",
    )
    reviewer = forms.ModelChoiceField(
        queryset=get_user_model().objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Reviewer",
    )

    include_ims = forms.BooleanField(
        required=False,
        initial=True,
        label="GST IMS review",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    include_gstr1 = forms.BooleanField(
        required=False,
        initial=True,
        label="GSTR-1",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    include_gstr3b = forms.BooleanField(
        required=False,
        initial=True,
        label="GSTR-3B",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    include_tds_payment = forms.BooleanField(
        required=False,
        initial=True,
        label="TDS payment",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    include_tds_returns = forms.BooleanField(
        required=False,
        initial=True,
        label="Quarterly TDS 24Q/26Q",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    ims_review_day = forms.IntegerField(
        min_value=1,
        max_value=31,
        initial=10,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 31}),
        label="IMS day",
    )
    gstr1_day = forms.IntegerField(
        min_value=1,
        max_value=31,
        initial=11,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 31}),
        label="GSTR-1 day",
    )
    gstr3b_day = forms.IntegerField(
        min_value=1,
        max_value=31,
        initial=20,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 31}),
        label="GSTR-3B day",
    )
    tds_payment_day = forms.IntegerField(
        min_value=1,
        max_value=31,
        initial=7,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 31}),
        label="TDS payment day",
    )

    gstr9_due = forms.DateField(
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"class": "form-control", "type": "date"}),
        input_formats=["%Y-%m-%d"],
        label="GSTR-9 due",
    )
    gstr9c_due = forms.DateField(
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"class": "form-control", "type": "date"}),
        input_formats=["%Y-%m-%d"],
        label="GSTR-9C due",
    )
    itr_due = forms.DateField(
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"class": "form-control", "type": "date"}),
        input_formats=["%Y-%m-%d"],
        label="ITR due",
    )
    tax_audit_due = forms.DateField(
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"class": "form-control", "type": "date"}),
        input_formats=["%Y-%m-%d"],
        label="Tax audit due",
    )
    mca_aoc4_due = forms.DateField(
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"class": "form-control", "type": "date"}),
        input_formats=["%Y-%m-%d"],
        label="MCA AOC-4 due",
    )
    mca_mgt7_due = forms.DateField(
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"class": "form-control", "type": "date"}),
        input_formats=["%Y-%m-%d"],
        label="MCA MGT-7 due",
    )

    def __init__(self, *args, companies=None, users=None, **kwargs):
        super().__init__(*args, **kwargs)
        if companies is not None:
            self.fields["companies"].queryset = companies
        if users is not None:
            self.fields["assigned_to"].queryset = users
            self.fields["reviewer"].queryset = users
        self.fields["assigned_to"].empty_label = "Unassigned"
        self.fields["reviewer"].empty_label = "No reviewer"

    def clean(self):
        cleaned = super().clean()
        if not any(
            cleaned.get(name)
            for name in (
                "include_ims",
                "include_gstr1",
                "include_gstr3b",
                "include_tds_payment",
                "include_tds_returns",
                "gstr9_due",
                "gstr9c_due",
                "itr_due",
                "tax_audit_due",
                "mca_aoc4_due",
                "mca_mgt7_due",
            )
        ):
            raise forms.ValidationError("Select at least one monthly template or annual due date.")
        return cleaned


class ComplianceNoticeForm(forms.ModelForm):
    class Meta:
        model = ComplianceNotice
        fields = [
            "company",
            "notice_type",
            "title",
            "reference_number",
            "issue_date",
            "response_due_date",
            "status",
            "priority",
            "assigned_to",
            "related_filing",
            "portal_status",
            "description",
            "response_summary",
        ]
        widgets = {
            "company": forms.Select(attrs={"class": "form-select"}),
            "notice_type": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. GST notice response"}),
            "reference_number": forms.TextInput(attrs={"class": "form-control", "placeholder": "Notice ref / DIN / ARN"}),
            "issue_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "response_due_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "related_filing": forms.Select(attrs={"class": "form-select"}),
            "portal_status": forms.TextInput(attrs={"class": "form-control", "placeholder": "Portal status"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "response_summary": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, companies=None, users=None, **kwargs):
        super().__init__(*args, **kwargs)
        if companies is not None:
            self.fields["company"].queryset = companies
            self.fields["related_filing"].queryset = ComplianceFiling.objects.filter(company__in=companies).order_by("-due_date", "company__name")
        else:
            self.fields["related_filing"].queryset = ComplianceFiling.objects.none()
        if users is not None:
            self.fields["assigned_to"].queryset = users
        self.fields["assigned_to"].required = False
        self.fields["assigned_to"].empty_label = "Unassigned"
        self.fields["related_filing"].required = False
        self.fields["related_filing"].empty_label = "No related filing"
        for name in ("reference_number", "issue_date", "response_due_date", "portal_status", "description", "response_summary"):
            self.fields[name].required = False
