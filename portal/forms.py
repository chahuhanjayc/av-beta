from datetime import timedelta

from django import forms
from django.utils import timezone

from core.models import Company
from core.phone import normalize_phone_number
from .models import ClientDocumentRequest, PortalUser


CLIENT_REQUEST_TEMPLATES = {
    "gst_invoice": {
        "label": "GST purchase invoices",
        "document_type": ClientDocumentRequest.TYPE_GST_INVOICE,
        "title": "Upload GST purchase invoices",
        "notes": "Please upload all pending supplier invoices/bills for GST reconciliation.",
        "due_days": 2,
    },
    "bank_statement": {
        "label": "Bank statement",
        "document_type": ClientDocumentRequest.TYPE_BANK,
        "title": "Upload bank statement",
        "notes": "Please upload the bank statement for the requested period in PDF or Excel format.",
        "due_days": 3,
    },
    "tds_challan": {
        "label": "TDS challan",
        "document_type": ClientDocumentRequest.TYPE_TDS,
        "title": "Upload TDS challan",
        "notes": "Please upload the TDS challan, payment proof, or related TDS working.",
        "due_days": 2,
    },
    "ledger_confirmation": {
        "label": "Ledger confirmation",
        "document_type": ClientDocumentRequest.TYPE_LEDGER_CONFIRMATION,
        "title": "Upload ledger confirmation",
        "notes": "Please upload signed ledger confirmation or balance confirmation evidence.",
        "due_days": 7,
    },
    "notice_evidence": {
        "label": "Notice evidence",
        "document_type": ClientDocumentRequest.TYPE_GST_NOTICE,
        "title": "Upload notice evidence",
        "notes": "Please upload all evidence, notices, replies, and supporting documents for this notice.",
        "due_days": 1,
    },
    "other": {
        "label": "Other document",
        "document_type": ClientDocumentRequest.TYPE_OTHER,
        "title": "Upload requested document",
        "notes": "Please upload the requested document.",
        "due_days": 3,
    },
}


class ClientDocumentRequestForm(forms.ModelForm):
    template = forms.ChoiceField(
        choices=[("", "Select template"), *[(key, value["label"]) for key, value in CLIENT_REQUEST_TEMPLATES.items()]],
        required=False,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    create_task = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = ClientDocumentRequest
        fields = [
            "company",
            "portal_user",
            "recipient_email",
            "recipient_whatsapp_number",
            "document_type",
            "title",
            "due_date",
            "source_reference",
            "notes",
        ]
        widgets = {
            "company": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "portal_user": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "recipient_email": forms.EmailInput(
                attrs={"class": "form-control form-control-sm", "placeholder": "client@example.com"}
            ),
            "recipient_whatsapp_number": forms.TextInput(
                attrs={"class": "form-control form-control-sm", "placeholder": "+919876543210"}
            ),
            "document_type": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "title": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "due_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "source_reference": forms.TextInput(
                attrs={"class": "form-control form-control-sm", "placeholder": "Optional internal reference"}
            ),
            "notes": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 4}),
        }

    def __init__(self, *args, companies=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.companies = companies
        if companies is not None:
            self.fields["company"].queryset = companies
            self.fields["portal_user"].queryset = PortalUser.objects.filter(
                linked_ledger__company__in=companies,
                is_active=True,
            ).select_related("linked_ledger__company").order_by("linked_ledger__company__name", "name")
        self.fields["portal_user"].required = False
        self.fields["portal_user"].empty_label = "No portal user linked"
        self.fields["recipient_email"].required = False
        self.fields["recipient_whatsapp_number"].required = False

    def clean_company(self):
        company = self.cleaned_data["company"]
        if self.companies is not None and not self.companies.filter(pk=company.pk).exists():
            raise forms.ValidationError("You do not have permission to create requests for this company.")
        return company

    def clean(self):
        cleaned = super().clean()
        company = cleaned.get("company")
        portal_user = cleaned.get("portal_user")
        if company and portal_user and portal_user.linked_ledger.company_id != company.pk:
            self.add_error("portal_user", "Portal user must belong to the selected company.")
        if portal_user and not cleaned.get("recipient_email"):
            cleaned["recipient_email"] = portal_user.email
        return cleaned

    def clean_recipient_whatsapp_number(self):
        value = self.cleaned_data.get("recipient_whatsapp_number")
        if not value:
            return value
        try:
            return normalize_phone_number(value)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc


def initial_for_template(template_code):
    template = CLIENT_REQUEST_TEMPLATES.get(template_code)
    if not template:
        return {}
    return {
        "template": template_code,
        "document_type": template["document_type"],
        "title": template["title"],
        "notes": template["notes"],
        "due_date": timezone.localdate() + timedelta(days=template["due_days"]),
    }


class ClientRequestCampaignForm(forms.Form):
    template = forms.ChoiceField(
        choices=[("", "Select template"), *[(key, value["label"]) for key, value in CLIENT_REQUEST_TEMPLATES.items()]],
        required=False,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    company = forms.ModelChoiceField(
        queryset=Company.objects.none(),
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    portal_users = forms.ModelMultipleChoiceField(
        queryset=PortalUser.objects.none(),
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
    )
    document_type = forms.ChoiceField(
        choices=ClientDocumentRequest.DOCUMENT_TYPE_CHOICES,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    title = forms.CharField(
        max_length=180,
        widget=forms.TextInput(attrs={"class": "form-control form-control-sm"}),
    )
    due_date = forms.DateField(
        widget=forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
    )
    source_reference_prefix = forms.CharField(
        max_length=120,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": "e.g. GST-APR-2026",
            }
        ),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 4}),
    )
    create_task = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    send_email = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, companies=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.companies = companies
        if companies is not None:
            self.fields["company"].queryset = companies
        selected_company = None
        company_value = None
        if self.is_bound:
            company_value = self.data.get("company")
        elif self.initial.get("company"):
            company_value = self.initial.get("company")

        if companies is not None and company_value:
            selected_company = companies.filter(pk=company_value).first()
        elif companies is not None and companies.count() == 1:
            selected_company = companies.first()

        portal_users = PortalUser.objects.filter(
            linked_ledger__company__in=companies or [],
            is_active=True,
        ).select_related("linked_ledger", "linked_ledger__company")
        if selected_company:
            portal_users = portal_users.filter(linked_ledger__company=selected_company)
        self.fields["portal_users"].queryset = portal_users.order_by("name", "email")

    def clean_company(self):
        company = self.cleaned_data["company"]
        if self.companies is not None and not self.companies.filter(pk=company.pk).exists():
            raise forms.ValidationError("You do not have permission to create campaigns for this company.")
        return company

    def clean(self):
        cleaned = super().clean()
        company = cleaned.get("company")
        portal_users = cleaned.get("portal_users")
        if company and portal_users:
            invalid = [
                user for user in portal_users
                if user.linked_ledger.company_id != company.pk
            ]
            if invalid:
                self.add_error("portal_users", "All selected clients must belong to the selected company.")
        return cleaned
