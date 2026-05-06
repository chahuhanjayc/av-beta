"""
inventory/forms.py

Forms for Stock Item CRUD, Godowns, Batches, and the inline stock-item rows on Vouchers.
"""

from decimal import Decimal

from django import forms
from django.forms import inlineformset_factory
from django.forms.models import BaseInlineFormSet

from .models import StockItem, VoucherStockItem, Godown, Batch
from vouchers.models import Voucher


# ─────────────────────────────────────────────────────────────────────────────
# StockItem create / edit form
# ─────────────────────────────────────────────────────────────────────────────

class StockItemForm(forms.ModelForm):
    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)
        # Make HSN and TaxRate optional
        self.fields["hsn_sac"].required  = False
        self.fields["tax_rate"].required = False
        self.fields["hsn_sac"].empty_label  = "— None —"
        self.fields["tax_rate"].empty_label = "— None —"

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if self.company and name:
            qs = StockItem.objects.filter(company=self.company, name__iexact=name)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("A stock item with this name already exists.")
        return name

    class Meta:
        model  = StockItem
        fields = [
            "name", "unit", "opening_quantity",
            "purchase_price", "selling_price",
            "hsn_sac", "tax_rate",
            "low_stock_threshold", "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. Basmati Rice 5kg Bag",
                "autofocus": True,
            }),
            "unit": forms.Select(attrs={"class": "form-select"}),
            "opening_quantity": forms.NumberInput(attrs={
                "class": "form-control", "step": "0.001", "min": "0",
            }),
            "purchase_price": forms.NumberInput(attrs={
                "class": "form-control", "step": "0.01", "min": "0",
                "placeholder": "Default purchase price per unit",
            }),
            "selling_price": forms.NumberInput(attrs={
                "class": "form-control", "step": "0.01", "min": "0",
                "placeholder": "Default selling price per unit",
            }),
            "hsn_sac": forms.Select(attrs={"class": "form-select"}),
            "tax_rate": forms.Select(attrs={"class": "form-select"}),
            "low_stock_threshold": forms.NumberInput(attrs={
                "class": "form-control", "step": "0.001", "min": "0",
                "placeholder": "0 = no alert",
            }),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "low_stock_threshold": "Low Stock Alert Threshold",
        }
        help_texts = {
            "opening_quantity": "Current stock on hand when setting up this item.",
            "low_stock_threshold": "You will be alerted when closing stock falls below this quantity.",
        }


# ─────────────────────────────────────────────────────────────────────────────
# VoucherStockItem inline row (used in voucher_form.html)
# ─────────────────────────────────────────────────────────────────────────────

class VoucherStockItemForm(forms.ModelForm):
    class Meta:
        model  = VoucherStockItem
        fields = ["stock_item", "quantity", "rate", "godown", "batch"]
        widgets = {
            "stock_item": forms.Select(attrs={
                "class": "form-select stock-item-select",
            }),
            "quantity": forms.NumberInput(attrs={
                "class": "form-control stock-qty",
                "step": "0.001", "min": "0.001",
                "placeholder": "Qty",
            }),
            "rate": forms.NumberInput(attrs={
                "class": "form-control stock-rate",
                "step": "0.01", "min": "0",
                "placeholder": "Rate ₹",
            }),
            "godown": forms.Select(attrs={
                "class": "form-select form-select-sm godown-select",
            }),
            "batch": forms.Select(attrs={
                "class": "form-select form-select-sm batch-select",
            }),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["stock_item"].queryset = StockItem.objects.filter(
                company=company, is_active=True
            ).order_by("name")
            self.fields["godown"].queryset = Godown.objects.filter(
                company=company, is_active=True
            ).order_by("name")
        else:
            self.fields["stock_item"].queryset = StockItem.objects.none()
            self.fields["godown"].queryset     = Godown.objects.none()
        self.fields["stock_item"].empty_label = "— Select Item —"
        self.fields["stock_item"].required    = False  # Allow empty rows
        self.fields["quantity"].required      = False
        self.fields["rate"].required          = False
        self.fields["godown"].required        = False
        self.fields["godown"].empty_label     = "— Godown (optional) —"
        self.fields["batch"].required         = False
        self.fields["batch"].empty_label      = "— Batch (optional) —"
        # Batch queryset: only batches for the company's items
        if company:
            self.fields["batch"].queryset = Batch.objects.filter(
                stock_item__company=company
            ).select_related("stock_item").order_by("stock_item__name", "batch_number")
        else:
            self.fields["batch"].queryset = Batch.objects.none()


    def clean(self):
        cleaned = super().clean()
        stock_item = cleaned.get("stock_item")
        quantity = cleaned.get("quantity")
        rate = cleaned.get("rate")
        godown = cleaned.get("godown")
        batch = cleaned.get("batch")

        if not stock_item:
            if any([quantity, rate, godown, batch]):
                self.add_error("stock_item", "Select a stock item for this line.")
            return cleaned

        if quantity in (None, "") or quantity <= Decimal("0.000"):
            self.add_error("quantity", "Quantity is required when a stock item is selected.")
        if rate in (None, ""):
            self.add_error("rate", "Rate is required when a stock item is selected.")
        elif rate < Decimal("0.00"):
            self.add_error("rate", "Rate cannot be negative.")

        if batch:
            if batch.stock_item_id != stock_item.pk:
                self.add_error("batch", "Selected batch does not belong to this stock item.")
            if godown and batch.godown_id and batch.godown_id != godown.pk:
                self.add_error("batch", "Selected batch belongs to a different godown.")
            if not godown and batch.godown_id:
                cleaned["godown"] = batch.godown

        return cleaned


# Formset factory — attached to a Voucher
class BaseVoucherStockItemFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        seen = set()

        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            cleaned = form.cleaned_data
            if cleaned.get("DELETE"):
                continue

            stock_item = cleaned.get("stock_item")
            if not stock_item:
                continue

            godown = cleaned.get("godown")
            batch = cleaned.get("batch")
            key = (
                stock_item.pk,
                godown.pk if godown else None,
                batch.pk if batch else None,
            )
            if key in seen:
                raise forms.ValidationError(
                    "Duplicate stock item rows are not allowed. Merge the quantity into one row."
                )
            seen.add(key)


VoucherStockItemFormSet = inlineformset_factory(
    Voucher,
    VoucherStockItem,
    form=VoucherStockItemForm,
    formset=BaseVoucherStockItemFormSet,
    extra=1,
    min_num=0,
    validate_min=False,
    can_delete=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# Godown form
# ─────────────────────────────────────────────────────────────────────────────

class GodownForm(forms.ModelForm):
    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if self.company and name:
            qs = Godown.objects.filter(company=self.company, name__iexact=name)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("A godown with this name already exists.")
        return name

    class Meta:
        model  = Godown
        fields = ["name", "location", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. Main Warehouse, Cold Storage",
                "autofocus": True,
            }),
            "location": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "Address or brief description (optional)",
            }),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Batch form
# ─────────────────────────────────────────────────────────────────────────────

class BatchForm(forms.ModelForm):
    class Meta:
        model  = Batch
        fields = ["stock_item", "godown", "batch_number", "expiry_date", "purchase_rate", "quantity"]
        widgets = {
            "stock_item": forms.Select(attrs={"class": "form-select"}),
            "godown": forms.Select(attrs={"class": "form-select"}),
            "batch_number": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "e.g. LOT-20240101",
            }),
            "expiry_date": forms.DateInput(attrs={
                "class": "form-control", "type": "date",
            }),
            "purchase_rate": forms.NumberInput(attrs={
                "class": "form-control", "step": "0.01",
            }),
            "quantity": forms.NumberInput(attrs={
                "class": "form-control", "step": "0.001",
            }),
        }

    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)
        if company:
            self.fields["stock_item"].queryset = StockItem.objects.filter(
                company=company, is_active=True
            ).order_by("name")
            self.fields["godown"].queryset = Godown.objects.filter(
                company=company, is_active=True
            ).order_by("name")
        else:
            self.fields["stock_item"].queryset = StockItem.objects.none()
            self.fields["godown"].queryset = Godown.objects.none()
        
        self.fields["expiry_date"].required = False

    def clean_batch_number(self):
        return (self.cleaned_data.get("batch_number") or "").strip()

    def clean(self):
        cleaned = super().clean()
        company = self.company
        stock_item = cleaned.get("stock_item")
        godown = cleaned.get("godown")
        batch_number = cleaned.get("batch_number")

        if company and stock_item and batch_number:
            qs = Batch.objects.filter(
                company=company,
                stock_item=stock_item,
                godown=godown,
                batch_number__iexact=batch_number,
            )
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error("batch_number", "This batch already exists for the selected item and godown.")

        return cleaned

    def save(self, commit=True):
        batch = super().save(commit=False)
        if self.company:
            batch.company = self.company
        if commit:
            batch.save()
            self.save_m2m()
        return batch
