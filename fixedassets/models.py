"""
fixedassets/models.py — Phase 7A: Fixed Assets & Depreciation

Models:
  AssetGroup       — Category of fixed assets (e.g. Buildings, Plant & Machinery)
  FixedAsset       — Individual asset master
  AssetDepreciation— Annual depreciation record per asset per FY
"""

from decimal import Decimal
from django.db import models
from core.models import Company
from ledger.models import Ledger


class AssetGroup(models.Model):
    """Grouping for fixed assets — maps to ledger sub-groups."""
    company                 = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="asset_groups")
    name                    = models.CharField(max_length=100)
    asset_ledger            = models.ForeignKey(
        Ledger, on_delete=models.PROTECT, related_name="asset_groups_as_asset",
        null=True, blank=True, help_text="Asset (Balance Sheet) ledger."
    )
    depreciation_ledger     = models.ForeignKey(
        Ledger, on_delete=models.PROTECT, related_name="asset_groups_as_depr",
        null=True, blank=True, help_text="Depreciation Expense ledger."
    )
    accumulated_depr_ledger = models.ForeignKey(
        Ledger, on_delete=models.PROTECT, related_name="asset_groups_as_accum",
        null=True, blank=True, help_text="Accumulated Depreciation ledger (contra asset)."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name        = "Asset Group"
        verbose_name_plural = "Asset Groups"
        ordering            = ["name"]
        unique_together     = ("company", "name")

    def __str__(self):
        return self.name


class FixedAsset(models.Model):
    METHOD_SLM = "SLM"
    METHOD_WDV = "WDV"
    METHOD_CHOICES = [
        ("SLM", "Straight Line Method (SLM)"),
        ("WDV", "Written Down Value (WDV)"),
    ]
    STATUS_ACTIVE   = "Active"
    STATUS_DISPOSED = "Disposed"
    STATUS_CHOICES  = [("Active", "Active"), ("Disposed", "Disposed")]

    company             = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="fixed_assets")
    asset_group         = models.ForeignKey(AssetGroup, on_delete=models.PROTECT, related_name="assets")
    name                = models.CharField(max_length=200)
    asset_code          = models.CharField(max_length=30, blank=True)
    purchase_date       = models.DateField()
    purchase_value      = models.DecimalField(max_digits=15, decimal_places=2)
    salvage_value       = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0.00"))
    useful_life_years   = models.PositiveSmallIntegerField(default=5)
    depreciation_method = models.CharField(max_length=3, choices=METHOD_CHOICES, default=METHOD_SLM)
    wdv_rate            = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("15.00"),
                                              help_text="WDV rate % per year")
    location            = models.CharField(max_length=200, blank=True)
    serial_number       = models.CharField(max_length=100, blank=True)
    status              = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    disposal_date       = models.DateField(null=True, blank=True)
    disposal_value      = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    notes               = models.TextField(blank=True)
    created_at          = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Fixed Asset"
        verbose_name_plural = "Fixed Assets"
        ordering            = ["name"]
        indexes             = [models.Index(fields=["company", "status"])]

    def __str__(self):
        code = f" [{self.asset_code}]" if self.asset_code else ""
        return f"{self.name}{code}"

    @property
    def depreciable_value(self):
        return self.purchase_value - self.salvage_value

    @property
    def annual_depreciation_slm(self):
        if self.useful_life_years > 0:
            return (self.depreciable_value / self.useful_life_years).quantize(Decimal("0.01"))
        return Decimal("0.00")

    @property
    def accumulated_depreciation(self):
        result = self.depreciations.aggregate(total=models.Sum("depreciation_amount"))["total"]
        return result or Decimal("0.00")

    @property
    def book_value(self):
        return self.purchase_value - self.accumulated_depreciation

    def compute_depreciation_for_fy(self, opening_book_value: Decimal) -> Decimal:
        """Compute annual depreciation given the opening book value for a FY."""
        if self.depreciation_method == self.METHOD_SLM:
            return self.annual_depreciation_slm
        else:
            return (opening_book_value * self.wdv_rate / 100).quantize(Decimal("0.01"))


class AssetDepreciation(models.Model):
    """One record per asset per financial year."""
    asset               = models.ForeignKey(FixedAsset, on_delete=models.CASCADE, related_name="depreciations")
    financial_year      = models.CharField(max_length=7, help_text="e.g. 2024-25")
    book_value_opening  = models.DecimalField(max_digits=15, decimal_places=2)
    depreciation_amount = models.DecimalField(max_digits=15, decimal_places=2)
    book_value_closing  = models.DecimalField(max_digits=15, decimal_places=2)
    posted_voucher      = models.ForeignKey(
        "vouchers.Voucher", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="depreciation_entries"
    )
    posted_at           = models.DateTimeField(null=True, blank=True)
    notes               = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name        = "Asset Depreciation"
        verbose_name_plural = "Asset Depreciations"
        unique_together     = ("asset", "financial_year")
        ordering            = ["financial_year"]

    def __str__(self):
        return f"{self.asset.name} — FY {self.financial_year} — ₹{self.depreciation_amount}"
