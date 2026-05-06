"""
ledger/forms.py
"""

from django import forms
from django.core.exceptions import ValidationError
from core.phone import normalize_phone_number
from .models import Ledger, AccountGroup


class LedgerForm(forms.ModelForm):
    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)
        if company:
            self.fields["account_group"].queryset = AccountGroup.objects.filter(company=company)
        self.fields["account_group"].empty_label = "— Select Group —"

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if self.company and name:
            qs = Ledger.objects.filter(company=self.company, name__iexact=name)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("A ledger with this name already exists.")
        return name

    def clean_whatsapp_number(self):
        try:
            return normalize_phone_number(self.cleaned_data.get("whatsapp_number"))
        except ValueError as exc:
            raise ValidationError(str(exc))

    class Meta:
        model = Ledger
        fields = [
            "name", "account_group", "opening_balance",
            "gstin", "pan_number", "email", "whatsapp_number", "address",
            "credit_limit", "credit_days",
            "tds_section", "tds_rate", "tds_threshold",
            "is_msme", "msme_reg_number",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "account_group": forms.Select(attrs={"class": "form-select"}),
            "opening_balance": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01"}
            ),
            "gstin": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. 24AAAAA0000A1Z5 (optional)",
                    "maxlength": "15",
                    "style": "text-transform:uppercase;",
                }
            ),
            "pan_number": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "ABCDE1234F",
                    "maxlength": "10",
                    "style": "text-transform:uppercase;",
                }
            ),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "whatsapp_number": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. +919876543210",
                    "inputmode": "tel",
                }
            ),
            "address": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 2,
                    "placeholder": "Registered address for billing",
                }
            ),
            "credit_limit": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0"}
            ),
            "credit_days": forms.NumberInput(
                attrs={"class": "form-control", "step": "1", "min": "0"}
            ),
            "tds_section": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "194C / 194J", "maxlength": "10"}
            ),
            "tds_rate": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0"}
            ),
            "tds_threshold": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0"}
            ),
            "is_msme": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "msme_reg_number": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Udyam registration number"}
            ),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
