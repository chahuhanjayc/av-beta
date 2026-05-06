"""orders/forms.py"""
from django import forms
from django.forms import inlineformset_factory
from .models import Order, OrderItem
from ledger.models import Ledger
from inventory.models import StockItem


class OrderForm(forms.ModelForm):
    class Meta:
        model  = Order
        fields = ["order_type", "party_ledger", "order_date", "expected_date", "narration"]
        widgets = {
            "order_type":    forms.Select(attrs={"class": "form-select"}),
            "party_ledger":  forms.Select(attrs={"class": "form-select"}),
            "order_date":    forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "expected_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "narration":     forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Notes / terms (optional)"}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["party_ledger"].queryset = Ledger.objects.filter(
                company=company, is_active=True,
                account_group__name__in=["Sundry Creditors", "Sundry Debtors"],
            ).order_by("name")
        else:
            self.fields["party_ledger"].queryset = Ledger.objects.none()
        self.fields["expected_date"].required = False
        self.fields["narration"].required     = False


class OrderItemForm(forms.ModelForm):
    class Meta:
        model  = OrderItem
        fields = ["stock_item", "description", "quantity", "rate"]
        widgets = {
            "stock_item":  forms.Select(attrs={"class": "form-select order-item-select"}),
            "description": forms.TextInput(attrs={"class": "form-control", "placeholder": "Description (if no item)"}),
            "quantity":    forms.NumberInput(attrs={"class": "form-control", "step": "0.001", "min": "0.001", "placeholder": "Qty"}),
            "rate":        forms.NumberInput(attrs={"class": "form-control", "step": "0.01",  "min": "0",     "placeholder": "Rate ₹"}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["stock_item"].queryset = StockItem.objects.filter(
                company=company, is_active=True
            ).order_by("name")
        else:
            self.fields["stock_item"].queryset = StockItem.objects.none()
        self.fields["stock_item"].required  = False
        self.fields["stock_item"].empty_label = "— Stock Item (optional) —"
        self.fields["description"].required = False


OrderItemFormSet = inlineformset_factory(
    Order, OrderItem, form=OrderItemForm,
    extra=3, min_num=1, validate_min=True, can_delete=True,
)
