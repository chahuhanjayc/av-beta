"""fixedassets/forms.py"""

from django import forms
from .models import AssetGroup, FixedAsset, AssetDepreciation


def _text(ph=""): return forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": ph})
def _num():       return forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01"})
def _date():      return forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"})
def _select():    return forms.Select(attrs={"class": "form-select form-select-sm"})
def _check():     return forms.CheckboxInput(attrs={"class": "form-check-input"})
def _textarea():  return forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 2})


class AssetGroupForm(forms.ModelForm):
    class Meta:
        model  = AssetGroup
        fields = ["name", "asset_ledger", "depreciation_ledger", "accumulated_depr_ledger", "is_active"]
        widgets = {
            "name":                    _text("e.g. Plant & Machinery"),
            "asset_ledger":            _select(),
            "depreciation_ledger":     _select(),
            "accumulated_depr_ledger": _select(),
            "is_active":               _check(),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        from ledger.models import Ledger
        if company:
            qs = Ledger.objects.filter(company=company, is_active=True).order_by("name")
            self.fields["asset_ledger"].queryset            = qs
            self.fields["depreciation_ledger"].queryset    = qs
            self.fields["accumulated_depr_ledger"].queryset = qs
        for f in ["asset_ledger", "depreciation_ledger", "accumulated_depr_ledger"]:
            self.fields[f].required = False


class FixedAssetForm(forms.ModelForm):
    class Meta:
        model  = FixedAsset
        fields = [
            "asset_group", "name", "asset_code", "purchase_date", "purchase_value",
            "salvage_value", "useful_life_years", "depreciation_method", "wdv_rate",
            "location", "serial_number", "notes",
        ]
        widgets = {
            "asset_group":         _select(),
            "name":                _text("Asset description"),
            "asset_code":          _text("FA-001"),
            "purchase_date":       _date(),
            "purchase_value":      _num(),
            "salvage_value":       _num(),
            "useful_life_years":   forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1}),
            "depreciation_method": _select(),
            "wdv_rate":            _num(),
            "location":            _text("e.g. Factory, Head Office"),
            "serial_number":       _text(),
            "notes":               _textarea(),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["asset_group"].queryset = AssetGroup.objects.filter(
                company=company, is_active=True
            ).order_by("name")


class AssetDisposalForm(forms.ModelForm):
    """Simple form to record asset disposal."""
    class Meta:
        model  = FixedAsset
        fields = ["disposal_date", "disposal_value", "notes"]
        widgets = {
            "disposal_date":  _date(),
            "disposal_value": _num(),
            "notes":          _textarea(),
        }
