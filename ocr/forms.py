"""
ocr/forms.py

Two forms:
  1. OCRUploadForm  — file upload step
  2. OCRVerifyForm  — editable parsed fields before confirming the voucher
"""

from django import forms
from ledger.models import Ledger
from core.upload_validation import DOCUMENT_EXTENSIONS, validate_uploaded_file
from .models import OCRSubmission


# ---------------------------------------------------------------------------
# Step 1 — Upload
# ---------------------------------------------------------------------------
MAX_FILE_MB = 20


class OCRUploadForm(forms.ModelForm):
    class Meta:
        model = OCRSubmission
        fields = ["file"]
        widgets = {
            "file": forms.FileInput(
                attrs={
                    "class": "form-control",
                    "accept": "image/*,.pdf",
                }
            )
        }

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if not f:
            return f

        try:
            return validate_uploaded_file(
                f,
                allowed_extensions=DOCUMENT_EXTENSIONS,
                max_mb=MAX_FILE_MB,
            )
        except Exception as exc:
            raise forms.ValidationError(str(exc))


# ---------------------------------------------------------------------------
# Step 2 — Verify / Edit extracted fields
# ---------------------------------------------------------------------------
class OCRVerifyForm(forms.Form):
    """
    Editable form shown after extraction. All fields are pre-filled from parsed_json.
    User can correct any mistakes before confirming the Purchase Voucher.
    """

    vendor_name = forms.CharField(
        label="Vendor Name",
        required=True,
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "e.g. ABC Suppliers Pvt Ltd"}
        ),
    )
    gstin = forms.CharField(
        label="Vendor GSTIN",
        required=False,
        max_length=15,
        widget=forms.TextInput(
            attrs={
                "class": "form-control font-monospace text-uppercase",
                "placeholder": "e.g. 27AABCU9603R1ZX",
                "maxlength": "15",
            }
        ),
    )
    date = forms.DateField(
        label="Bill Date",
        required=True,
        input_formats=[
            "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y",
            "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
        ],
        widget=forms.DateInput(
            attrs={"class": "form-control", "type": "date"},
            format="%Y-%m-%d",
        ),
    )
    total_amount = forms.DecimalField(
        label="Total Amount (₹)",
        required=True,
        max_digits=15,
        decimal_places=2,
        min_value=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "placeholder": "0.00",
            }
        ),
    )
    expense_ledger = forms.ModelChoiceField(
        label="Expense / Purchase Ledger (Dr)",
        queryset=Ledger.objects.none(),
        required=True,
        help_text="The ledger that gets debited (e.g. Purchase Account, Office Expenses).",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    payment_ledger = forms.ModelChoiceField(
        label="Payment / Creditor Ledger (Cr)",
        queryset=Ledger.objects.none(),
        required=True,
        help_text="The ledger that gets credited (e.g. Cash, Bank, Creditor account).",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    narration = forms.CharField(
        label="Narration",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "e.g. Purchase bill from ABC Suppliers — Invoice #INV-001",
            }
        ),
    )

    def __init__(self, *args, company=None, initial_parsed=None, **kwargs):
        super().__init__(*args, **kwargs)

        if company:
            all_active = Ledger.objects.filter(company=company, is_active=True).order_by(
                "account_group__nature", "name"
            )
            expense_qs = all_active.filter(account_group__nature__in=["Expense", "Asset"])
            payment_qs = all_active.filter(account_group__nature__in=["Asset", "Liability"])

            self.fields["expense_ledger"].queryset = expense_qs
            self.fields["payment_ledger"].queryset = payment_qs

        # Pre-fill from OCR parsed data
        if initial_parsed:
            pj = initial_parsed
            self._prefill_initial(pj, company)

    def _prefill_initial(self, pj: dict, company):
        """Apply parsed OCR values as form initial data."""
        if pj.get("vendor_name"):
            self.fields["vendor_name"].initial = pj["vendor_name"]
        if pj.get("gstin"):
            self.fields["gstin"].initial = pj["gstin"].upper()
        if pj.get("total_amount"):
            self.fields["total_amount"].initial = pj["total_amount"]

        # Date normalisation
        raw_date = pj.get("date", "")
        if raw_date:
            import re
            from datetime import datetime
            date_formats = [
                "%Y-%m-%d", "%Y/%m/%d",
                "%d/%m/%Y", "%d-%m-%Y",
                "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
            ]
            for fmt in date_formats:
                try:
                    parsed_dt = datetime.strptime(raw_date.strip(), fmt)
                    self.fields["date"].initial = parsed_dt.strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue

        # Auto-select vendor ledger if matched
        vendor_ledger_id = pj.get("vendor_ledger_id")
        if vendor_ledger_id and company:
            from ledger.models import Ledger
            try:
                lobj = Ledger.objects.get(pk=vendor_ledger_id, company=company)
                # If it's an expense-type → set as expense_ledger
                if lobj.group in ("Expense",):
                    self.fields["expense_ledger"].initial = lobj.pk
                else:
                    self.fields["payment_ledger"].initial = lobj.pk
            except Ledger.DoesNotExist:
                pass

        # Auto-narration
        vendor = pj.get("vendor_name", "")
        amount = pj.get("total_amount", "")
        date   = pj.get("date", "")
        if not self.fields["narration"].initial and vendor:
            self.fields["narration"].initial = (
                f"Purchase bill from {vendor}"
                + (f" dated {date}" if date else "")
                + (f" — ₹{amount}" if amount else "")
            )
