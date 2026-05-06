"""
costcenter/forms.py
"""
from django import forms
from .models import CostCenter, BudgetHead
from ledger.models import Ledger


class CostCenterForm(forms.ModelForm):
    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if self.company and name:
            qs = CostCenter.objects.filter(company=self.company, name__iexact=name)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("A cost center with this name already exists.")
        return name

    class Meta:
        model  = CostCenter
        fields = ["name", "code", "description", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. Marketing, R&D, Warehouse",
                "autofocus": True,
            }),
            "code": forms.TextInput(attrs={
                "class": "form-control text-uppercase",
                "placeholder": "e.g. MKT (optional)",
                "maxlength": "20",
            }),
            "description": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Brief description (optional)",
            }),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class BudgetHeadForm(forms.ModelForm):
    class Meta:
        model  = BudgetHead
        fields = [
            "ledger", "cost_center", "financial_year",
            "period", "budgeted_amount", "notes",
        ]
        widgets = {
            "ledger": forms.Select(attrs={"class": "form-select"}),
            "cost_center": forms.Select(attrs={"class": "form-select"}),
            "financial_year": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. 2024-25",
                "maxlength": "7",
            }),
            "period": forms.Select(attrs={"class": "form-select"}),
            "budgeted_amount": forms.NumberInput(attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0",
                "placeholder": "₹ Target amount",
            }),
            "notes": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Optional notes",
            }),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["ledger"].queryset = Ledger.objects.filter(
                company=company, is_active=True
            ).order_by("name")
            self.fields["cost_center"].queryset = CostCenter.objects.filter(
                company=company, is_active=True
            ).order_by("name")
        else:
            self.fields["ledger"].queryset     = Ledger.objects.none()
            self.fields["cost_center"].queryset = CostCenter.objects.none()
        self.fields["cost_center"].required  = False
        self.fields["cost_center"].empty_label = "— All Centers (company-wide) —"
        self.fields["notes"].required        = False
