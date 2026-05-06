"""
vouchers/forms.py

VoucherForm + VoucherItemFormSet + VoucherStockItemFormSet.

The view is responsible for passing company so we filter ledger choices.
VoucherItemForm includes an optional reference_voucher field for bill-to-bill tracking.
"""

from decimal import Decimal
from django import forms
from django.forms import inlineformset_factory

from core.upload_validation import DOCUMENT_EXTENSIONS, validate_uploaded_file
from .models import Voucher, VoucherItem
from ledger.models import Ledger
from django.forms.models import BaseInlineFormSet


def _set_blank_choice_label(field, label):
    choices = [(value, text) for value, text in list(field.choices) if value != ""]
    field.choices = [("", label), *choices]


class VoucherForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _set_blank_choice_label(self.fields["voucher_type"], "Select voucher type")
        _set_blank_choice_label(self.fields["place_of_supply"], "Select state")
        _set_blank_choice_label(self.fields["transport_mode"], "Select mode")
        _set_blank_choice_label(self.fields["vehicle_type"], "Select vehicle type")

    class Meta:
        model = Voucher
        fields = [
            "date",
            "due_date",
            "voucher_type",
            "narration",
            "place_of_supply",
            "reverse_charge",
            "dispatch_pincode",
            "ship_to_pincode",
            "transport_mode",
            "transport_distance_km",
            "transporter_id",
            "transporter_name",
            "transport_doc_no",
            "transport_doc_date",
            "vehicle_number",
            "vehicle_type",
            "document",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "due_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}
            ),
            "voucher_type": forms.Select(attrs={"class": "form-select"}),
            "narration": forms.Textarea(
                attrs={"class": "form-control", "rows": 2, "placeholder": "Optional memo…"}
            ),
            "place_of_supply": forms.Select(
                attrs={"class": "form-select form-select-sm"}
            ),
            "reverse_charge": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
            "document": forms.ClearableFileInput(
                attrs={"class": "form-control form-control-sm"}
            ),
            "dispatch_pincode": forms.NumberInput(
                attrs={"class": "form-control form-control-sm", "min": "100000", "max": "999999"}
            ),
            "ship_to_pincode": forms.NumberInput(
                attrs={"class": "form-control form-control-sm", "min": "100000", "max": "999999"}
            ),
            "transport_mode": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "transport_distance_km": forms.NumberInput(
                attrs={"class": "form-control form-control-sm", "min": "0"}
            ),
            "transporter_id": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "transporter_name": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "transport_doc_no": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "transport_doc_date": forms.DateInput(attrs={"class": "form-control form-control-sm", "type": "date"}),
            "vehicle_number": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            "vehicle_type": forms.Select(attrs={"class": "form-select form-select-sm"}),
        }
        labels = {
            "place_of_supply": "Place of Supply",
            "reverse_charge":  "Reverse Charge (RCM)",
            "dispatch_pincode": "Dispatch PIN",
            "ship_to_pincode": "Ship-to PIN",
            "transport_mode": "Mode",
            "transport_distance_km": "Distance (km)",
            "transporter_id": "Transporter ID",
            "transporter_name": "Transporter",
            "transport_doc_no": "Transport Doc No",
            "transport_doc_date": "Transport Doc Date",
            "vehicle_number": "Vehicle No",
            "vehicle_type": "Vehicle Type",
        }

    def clean_document(self):
        document = self.cleaned_data.get("document")
        try:
            return validate_uploaded_file(
                document,
                allowed_extensions=DOCUMENT_EXTENSIONS,
                max_mb=20,
            )
        except Exception as exc:
            raise forms.ValidationError(str(exc))


class VoucherItemForm(forms.ModelForm):
    class Meta:
        model = VoucherItem
        fields = [
            "ledger", "entry_type", "amount", "narration", 
            "cost_center", "reference_voucher",
            "stock_item", "godown", "batch", "quantity", "rate"
        ]
        widgets = {
            "ledger": forms.Select(attrs={"class": "form-select ledger-select"}),
            "entry_type": forms.Select(attrs={"class": "form-select entry-type-select"}),
            "amount": forms.NumberInput(
                attrs={"class": "form-control amount-input", "step": "0.01", "min": "0"}
            ),
            "narration": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Line note (optional)"}
            ),
            "reference_voucher": forms.Select(
                attrs={
                    "class": "form-select form-select-sm ref-voucher-select",
                    "title": "Bill-to-Bill: which invoice does this line settle?",
                }
            ),
            "cost_center": forms.Select(
                attrs={
                    "class": "form-select form-select-sm cost-center-select",
                    "title": "Tag to a Cost Center (optional)",
                }
            ),
            "stock_item": forms.Select(attrs={"class": "form-select stock-item-select"}),
            "godown": forms.Select(attrs={"class": "form-select godown-select"}),
            "batch": forms.Select(attrs={"class": "form-select batch-select"}),
            "quantity": forms.NumberInput(
                attrs={"class": "form-control quantity-input", "step": "0.001"}
            ),
            "rate": forms.NumberInput(
                attrs={"class": "form-control rate-input", "step": "0.01", "min": "0"}
            ),
        }
        labels = {
            "reference_voucher": "Against Invoice",
            "cost_center":       "Cost Center",
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["ledger"].queryset = Ledger.objects.filter(
                company=company, is_active=True
            ).select_related("account_group").distinct().order_by(
                "account_group__nature", "account_group__name", "name"
            )
            self.fields["ledger"].label_from_instance = (
                lambda obj: f"{obj.name} ({obj.account_group.name} / {obj.account_group.nature})"
            )

            # Only show Sales and Purchase vouchers as reference targets
            self.fields["reference_voucher"].queryset = Voucher.objects.filter(
                company=company,
                voucher_type__in=["Sales", "Purchase"],
            ).order_by("-date")

            # Cost Centers for this company
            try:
                from costcenter.models import CostCenter
                self.fields["cost_center"].queryset = CostCenter.objects.filter(
                    company=company, is_active=True
                ).order_by("name")
            except Exception:
                pass

            # Inventory fields
            from inventory.models import StockItem, Godown, Batch
            self.fields["stock_item"].queryset = StockItem.objects.filter(
                company=company, is_active=True
            ).order_by("name")

            self.fields["godown"].queryset = Godown.objects.filter(
                company=company, is_active=True
            ).order_by("name")

            self.fields["batch"].queryset = Batch.objects.filter(
                company=company
            ).order_by("batch_number")

        else:
            self.fields["reference_voucher"].queryset = Voucher.objects.none()
            try:
                from costcenter.models import CostCenter
                self.fields["cost_center"].queryset = CostCenter.objects.none()
            except Exception:
                pass
            from inventory.models import StockItem, Godown, Batch
            self.fields["stock_item"].queryset = StockItem.objects.none()
            self.fields["godown"].queryset = Godown.objects.none()
            self.fields["batch"].queryset = Batch.objects.none()

        # Make fields optional to allow blank rows in formsets
        self.fields["ledger"].required = False
        self.fields["ledger"].empty_label = "Select party / company ledger"
        self.fields["entry_type"].required = False
        self.fields["entry_type"].choices = [("", "Cr / Dr"), ("CR", "Cr"), ("DR", "Dr")]
        self.fields["amount"].required = False
        self.fields["stock_item"].required = False
        self.fields["stock_item"].empty_label = "No stock item"
        self.fields["godown"].required = False
        self.fields["godown"].empty_label = "No godown"
        self.fields["batch"].required = False
        self.fields["batch"].empty_label = "No batch"
        self.fields["quantity"].required = False
        self.fields["rate"].required = False
        
        # Make reference optional
        self.fields["reference_voucher"].required = False
        self.fields["reference_voucher"].empty_label = "— None (not bill-linked) —"
        self.fields["cost_center"].required = False
        self.fields["cost_center"].empty_label = "— No Cost Center —"

    def clean(self):
        cleaned_data = super().clean()
        stock_item = cleaned_data.get("stock_item")
        quantity = cleaned_data.get("quantity")
        rate = cleaned_data.get("rate")

        if stock_item:
            if quantity in (None, "") or quantity <= Decimal("0.000"):
                self.add_error("quantity", "Quantity is required when a stock item is selected.")
            if rate in (None, ""):
                self.add_error("rate", "Rate is required when a stock item is selected.")
        else:
            cleaned_data["godown"] = None
            cleaned_data["batch"] = None
            cleaned_data["quantity"] = quantity or Decimal("0.000")
            cleaned_data["rate"] = rate or Decimal("0.00")

        return cleaned_data


class BaseVoucherItemFormSet(BaseInlineFormSet):
    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs['company'] = getattr(self, 'company', None)
        return kwargs

# ---------------------------------------------------------------------------
# Inline formset factory — used in the view
# ---------------------------------------------------------------------------
VoucherItemFormSet = inlineformset_factory(
    Voucher,
    VoucherItem,
    form=VoucherItemForm,
    formset=BaseVoucherItemFormSet,
    fk_name="voucher",       # disambiguates from reference_voucher FK
    extra=1,
    min_num=1,
    validate_min=True,
    can_delete=True,
)
