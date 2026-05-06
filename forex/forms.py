"""forex/forms.py"""

from django import forms
from .models import Currency, ExchangeRate


def _text(ph=""): return forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": ph})
def _num():       return forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.0001"})
def _date():      return forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"})
def _select():    return forms.Select(attrs={"class": "form-select form-select-sm"})
def _check():     return forms.CheckboxInput(attrs={"class": "form-check-input"})


class CurrencyForm(forms.ModelForm):
    class Meta:
        model  = Currency
        fields = ["code", "name", "symbol", "is_base", "is_active"]
        widgets = {
            "code":      _text("USD"),
            "name":      _text("US Dollar"),
            "symbol":    _text("$"),
            "is_base":   _check(),
            "is_active": _check(),
        }


class ExchangeRateForm(forms.ModelForm):
    class Meta:
        model  = ExchangeRate
        fields = ["currency", "date", "rate", "buying_rate", "selling_rate", "source"]
        widgets = {
            "currency":     _select(),
            "date":         _date(),
            "rate":         forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.000001"}),
            "buying_rate":  forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.000001"}),
            "selling_rate": forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.000001"}),
            "source":       _select(),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            qs = Currency.objects.filter(
                company=company, is_active=True, is_base=False
            ).order_by("code")
            self.fields["currency"].queryset = qs
            if not qs.exists():
                self.fields["currency"].empty_label = "No active foreign currencies found. Add one first."
        for f in ["buying_rate", "selling_rate"]:
            self.fields[f].required = False
