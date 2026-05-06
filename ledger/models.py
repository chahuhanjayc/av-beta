"""
ledger/models.py

Ledger: a named account belonging to a Company, categorised by group.
current_balance() computes the running balance from voucher items.

Changelog:
  - Added "Equity" to GROUP_CHOICES (required for Balance Sheet)
  - Added `parent` FK for sub-ledger hierarchy (e.g. Bank Accounts → SBI Current)
  - Added `updated_at` for audit trail
  - Fixed current_balance() to use DB-level aggregation (was loading all rows into memory)
"""

from decimal import Decimal
from django.db import models
from core.models import Company


from django.core.validators import RegexValidator

# GSTIN regex validator for India
gstin_validator = RegexValidator(
    regex=r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$',
    message="Enter a valid 15-character GSTIN (e.g., 24AAAAA0000A1Z5)."
)

class AccountGroup(models.Model):
    NATURE_CHOICES = [
        ("Asset",     "Asset"),
        ("Liability", "Liability"),
        ("Income",    "Income"),
        ("Expense",   "Expense"),
        ("Equity",    "Equity"),
        ("Tax",       "Tax"),
    ]

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="account_groups",
    )
    name = models.CharField(max_length=255)
    nature = models.CharField(max_length=20, choices=NATURE_CHOICES)
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )
    threshold_limit = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0.00"),
        help_text="Statutory threshold limit for this group (e.g., TDS limit)."
    )

    class Meta:
        verbose_name = "Account Group"
        verbose_name_plural = "Account Groups"
        unique_together = ("company", "name")
        ordering = ["nature", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_nature_display()})"


class Ledger(models.Model):
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="ledgers",
    )
    name = models.CharField(max_length=255)
    account_group = models.ForeignKey(
        AccountGroup,
        on_delete=models.PROTECT,
        related_name="ledgers",
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
        help_text=(
            "Optional parent account for sub-ledger hierarchy. "
            "Example: 'Bank Accounts' → 'SBI Current Account'."
        ),
    )
    opening_balance = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0.00")
    )
    gstin = models.CharField(
        max_length=15, blank=True, null=True,
        verbose_name="GSTIN",
        help_text="15-character GST Identification Number of this party (for GSTR-1 B2B invoices)",
        validators=[gstin_validator]
    )
    address = models.TextField(
        blank=True, null=True,
        help_text="Registered office address or billing address.",
    )
    email = models.EmailField(
        blank=True, null=True,
        help_text="Primary contact email for statutory communications."
    )
    whatsapp_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Vendor/client WhatsApp number for statutory follow-ups.",
    )
    is_active = models.BooleanField(default=True)
    
    # Credit Control
    credit_limit = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True,
        help_text="Maximum total outstanding allowed for this customer."
    )
    credit_days = models.IntegerField(
        null=True, blank=True,
        help_text="Maximum allowed age of unpaid invoices (in days)."
    )
    
    # Statutory / TDS
    pan_number = models.CharField(
        max_length=10, blank=True, null=True,
        verbose_name="PAN",
        help_text="10-character Permanent Account Number (required for TDS)"
    )
    tds_section = models.CharField(
        max_length=10, blank=True, null=True,
        verbose_name="TDS Section",
        help_text="e.g. 194C, 194J, 194H"
    )
    tds_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        help_text="Standard TDS rate for this ledger"
    )
    tds_threshold = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("30000.00"),
        help_text="Single bill threshold for TDS deduction (e.g. 30000 for 194C)"
    )

    # MSME
    is_msme = models.BooleanField(
        default=False,
        verbose_name="Is MSME?",
        help_text="If Yes, payment must be within 45 days (MSMED Act)."
    )
    msme_reg_number = models.CharField(
        max_length=50, blank=True, null=True,
        verbose_name="MSME Registration",
        help_text="Udyam Registration Number (URN)."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ledger"
        verbose_name_plural = "Ledgers"
        unique_together = ("company", "name")
        ordering = ["account_group__nature", "name"]

    def __str__(self):
        return f"{self.name} ({self.account_group.name})"

    def current_balance(self):
        """
        Signed balance = opening_balance + Credit - Debit from voucher items.

        The app stores balances as credit-positive and debit-negative:
        positive = Cr balance, negative = Dr balance.
        """
        from django.db.models import Sum
        from vouchers.models import VoucherItem

        agg = VoucherItem.objects.filter(
            ledger=self, voucher__company=self.company, voucher__status="APPROVED"
        ).aggregate(
            total_dr=Sum("amount", filter=models.Q(entry_type='DR')),
            total_cr=Sum("amount", filter=models.Q(entry_type='CR')),
        )
        total_dr = agg["total_dr"] or Decimal("0.00")
        total_cr = agg["total_cr"] or Decimal("0.00")
        return self.opening_balance + total_cr - total_dr

    @property
    def balance_amount(self):
        """Absolute current balance for display."""
        return abs(self.current_balance())

    @property
    def balance_side(self):
        """Display side for the signed current balance."""
        return "Cr" if self.current_balance() >= Decimal("0.00") else "Dr"
