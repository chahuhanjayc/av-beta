"""tds/forms.py"""

from django import forms
from .models import TDSReturnWorkpaper, TDSSection, TDSEntry


def _text(ph=""): return forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": ph})
def _num():       return forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01"})
def _date():      return forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"})
def _select():    return forms.Select(attrs={"class": "form-select form-select-sm"})
def _check():     return forms.CheckboxInput(attrs={"class": "form-check-input"})


class TDSSectionForm(forms.ModelForm):
    class Meta:
        model  = TDSSection
        fields = [
            "nature", "section_code", "description", "threshold",
            "rate_individual", "rate_company", "surcharge_rate", "is_active",
        ]
        widgets = {
            "nature":          _select(),
            "section_code":    _text("e.g. 194C"),
            "description":     _text("Nature of payment"),
            "threshold":       _num(),
            "rate_individual": _num(),
            "rate_company":    _num(),
            "surcharge_rate":  _num(),
            "is_active":       _check(),
        }


class TDSEntryForm(forms.ModelForm):
    class Meta:
        model  = TDSEntry
        fields = [
            "section", "deductee_ledger", "tds_ledger", "transaction_date",
            "deductee_type", "deductible_amount", "rate_applied", "tds_amount",
            "pan_number", "voucher", "notes",
        ]
        widgets = {
            "section":           _select(),
            "deductee_ledger":   _select(),
            "tds_ledger":        _select(),
            "transaction_date":  _date(),
            "deductee_type":     _select(),
            "deductible_amount": _num(),
            "rate_applied":      _num(),
            "tds_amount":        _num(),
            "pan_number":        _text("ABCDE1234F"),
            "voucher":           _select(),
            "notes":             _text(),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            from ledger.models import Ledger
            from vouchers.models import Voucher
            qs_ledger = Ledger.objects.filter(company=company, is_active=True).order_by("name")
            self.fields["deductee_ledger"].queryset = qs_ledger
            self.fields["tds_ledger"].queryset      = qs_ledger
            self.fields["section"].queryset = TDSSection.objects.filter(
                company=company, is_active=True
            ).order_by("section_code")
            self.fields["voucher"].queryset = Voucher.objects.filter(
                company=company
            ).order_by("-date")[:200]
        self.fields["voucher"].required   = False
        self.fields["tds_ledger"].required = False


class TDSDepositForm(forms.ModelForm):
    """Mark TDS as deposited."""
    class Meta:
        model  = TDSEntry
        fields = ["deposit_date", "challan_number", "bsr_code"]
        widgets = {
            "deposit_date":   _date(),
            "challan_number": _text("Challan serial number"),
            "bsr_code":       _text("7-digit BSR code"),
        }


class TDSReturnWorkpaperForm(forms.ModelForm):
    class Meta:
        model = TDSReturnWorkpaper
        fields = [
            "status",
            "fvu_status",
            "challan_status",
            "traces_statement_status",
            "form16_status",
            "traces_token",
            "ack_number",
            "notes",
        ]
        widgets = {
            "status": _select(),
            "fvu_status": _select(),
            "challan_status": _select(),
            "traces_statement_status": _select(),
            "form16_status": _select(),
            "traces_token": _text("TRACES request / token"),
            "ack_number": _text("Acknowledgement number"),
            "notes": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 3}),
        }

    def clean_traces_token(self):
        return (self.cleaned_data.get("traces_token") or "").strip()

    def clean_ack_number(self):
        return (self.cleaned_data.get("ack_number") or "").strip()
