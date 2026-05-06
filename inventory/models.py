"""
inventory/models.py

Inventory Management — Phase 4.1 + Phase 4.2 (Godowns & Batches).

Models:
  HSN_SAC           — HSN/SAC codes for GST classification (shared lookup table)
  TaxRate           — GST tax rate presets (0%, 5%, 12%, 18%, 28%)
  Godown            — Warehouse / storage location (company-scoped)
  Batch             — Lot/batch number for a stock item (company-scoped)
  StockItem         — A product/good bought and sold by the company (multi-tenant)
  StockLedger       — Every stock movement tied to a Voucher
  VoucherStockItem  — Links a Voucher to stock items with qty and rate
  StockValuationEntry — Track cost of lots for FIFO/AVG valuation
  CompanySettings   — Company-level inventory preferences

Design rules:
  - Multi-tenant: StockItem is always scoped to a Company.
  - StockLedger quantity > 0 → Inward (Purchase); < 0 → Outward (Sales).
  - Closing stock = opening_quantity + Σ(StockLedger.quantity).
  - Valuation: Weighted Average Cost (WAC) or FIFO.
  - Godown and Batch are optional (nullable) on StockLedger and VoucherStockItem.
"""

from decimal import Decimal
from datetime import date
from django.db import models
from django.core.exceptions import ValidationError

from core.models import Company
from vouchers.models import Voucher


# ─────────────────────────────────────────────────────────────────────────────
# Lookup Tables (shared, not company-scoped — shared across all companies)
# ─────────────────────────────────────────────────────────────────────────────

class HSN_SAC(models.Model):
    """
    HSN (Harmonized System of Nomenclature) / SAC (Services Accounting Code).
    Codes used for GST classification of goods and services.
    These are standard Indian GST codes — shared across all companies.
    """
    code        = models.CharField(max_length=20, unique=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name        = "HSN / SAC Code"
        verbose_name_plural = "HSN / SAC Codes"
        ordering            = ["code"]

    def __str__(self):
        if self.description:
            return f"{self.code} — {self.description[:50]}"
        return self.code


class TaxRate(models.Model):
    """
    Standard GST tax rate presets.
    Common rates: 0%, 5%, 12%, 18%, 28%.
    """
    rate        = models.DecimalField(max_digits=5, decimal_places=2, unique=True)
    description = models.CharField(max_length=100, blank=True,
                                   help_text="e.g. 'GST 18%' or 'Exempt'")

    class Meta:
        verbose_name        = "Tax Rate"
        verbose_name_plural = "Tax Rates"
        ordering            = ["rate"]

    def __str__(self):
        desc = f" ({self.description})" if self.description else ""
        return f"{self.rate}%{desc}"


# ─────────────────────────────────────────────────────────────────────────────
# Godown  — warehouse / storage location
# ─────────────────────────────────────────────────────────────────────────────

class Godown(models.Model):
    """
    A physical storage location (warehouse, shop floor, cold storage, etc.)
    scoped to a company.  Stock movements can optionally record which godown
    the goods went into / came out of.
    """
    company     = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="godowns"
    )
    name        = models.CharField(max_length=200)
    location    = models.CharField(
        max_length=300, blank=True,
        help_text="Optional address or description of the location.",
    )
    is_primary  = models.BooleanField(
        default=False,
        help_text="The main warehouse for the company."
    )
    is_active   = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Godown"
        verbose_name_plural = "Godowns"
        ordering            = ["name"]
        unique_together     = ("company", "name")

    def __str__(self):
        return self.name


# ─────────────────────────────────────────────────────────────────────────────
# Batch  — lot / batch number
# ─────────────────────────────────────────────────────────────────────────────

class Batch(models.Model):
    """
    A batch / lot number attached to a stock item.
    Useful for FMCG, pharma, food — anything with expiry dates or lot traceability.
    """
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="batches",
        null=True, blank=True
    )
    stock_item = models.ForeignKey(
        "StockItem", on_delete=models.CASCADE, related_name="batches"
    )
    godown = models.ForeignKey(
        Godown, on_delete=models.CASCADE, related_name="batches",
        null=True, blank=True
    )
    batch_number = models.CharField(max_length=100)
    expiry_date = models.DateField(
        null=True, blank=True,
        help_text="Leave blank if this item has no expiry.",
    )
    purchase_rate = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0.00")
    )
    quantity = models.DecimalField(
        max_digits=15, decimal_places=3, default=Decimal("0.000")
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Batch"
        verbose_name_plural = "Batches"
        ordering            = ["stock_item", "batch_number"]
        unique_together     = ("company", "stock_item", "batch_number", "godown")

    def __str__(self):
        godown_name = self.godown.name if self.godown_id else "No godown"
        return f"{self.stock_item.name} | {self.batch_number} | {godown_name}"

    @property
    def is_expired(self):
        if self.expiry_date:
            return self.expiry_date < date.today()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# StockItem
# ─────────────────────────────────────────────────────────────────────────────

class StockItem(models.Model):
    """
    A product/item that the company buys and sells.

    Opening quantity is set once at creation.  All subsequent movements are
    tracked via StockLedger entries.  current_stock() computes the live qty.
    """
    UNIT_CHOICES = [
        ("Nos",    "Nos"),
        ("Kgs",    "Kgs"),
        ("Boxes",  "Boxes"),
        ("Dozen",  "Dozen"),
        ("Meters", "Meters"),
        ("Pieces", "Pieces"),
    ]

    VALUATION_METHODS = [
        ("WAC", "Weighted Average Cost"),
        ("FIFO", "First-In, First-Out"),
    ]

    name             = models.CharField(max_length=255)
    company          = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="stock_items"
    )
    unit             = models.CharField(max_length=20, choices=UNIT_CHOICES, default="Nos")
    valuation_method = models.CharField(
        max_length=10, choices=VALUATION_METHODS, default="WAC"
    )
    opening_quantity = models.DecimalField(
        max_digits=15, decimal_places=3, default=Decimal("0.000"),
        help_text="Initial stock quantity when item was set up.",
    )
    purchase_price   = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0.00"),
        help_text="Default purchase price per unit (used as fallback for WAC).",
    )
    selling_price    = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0.00"),
        help_text="Default selling price per unit (auto-fills voucher lines).",
    )
    hsn_sac          = models.ForeignKey(
        HSN_SAC, null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name="HSN / SAC Code",
    )
    tax_rate         = models.ForeignKey(
        TaxRate, null=True, blank=True, on_delete=models.SET_NULL,
    )
    low_stock_threshold = models.DecimalField(
        max_digits=15, decimal_places=3, default=Decimal("0.000"),
        help_text="Show alert when closing stock falls below this level. 0 = no alert.",
    )
    is_active        = models.BooleanField(default=True)
    prevent_negative_stock = models.BooleanField(
        default=False,
        help_text="If enabled, the system will block transactions that result in negative stock.",
    )
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Stock Item"
        verbose_name_plural = "Stock Items"
        ordering            = ["name"]
        unique_together     = ("company", "name")

    def __str__(self):
        return f"{self.name} ({self.unit})"

    # ── Stock quantity helpers ────────────────────────────────────────────────

    def total_inward(self, start_date=None, end_date=None):
        """Sum of positive (purchase) movements."""
        qs = self.ledger_entries.filter(quantity__gt=0)
        if start_date:
            qs = qs.filter(date__gte=start_date)
        if end_date:
            qs = qs.filter(date__lte=end_date)
        return qs.aggregate(total=models.Sum("quantity"))["total"] or Decimal("0.000")

    def total_outward(self, start_date=None, end_date=None):
        """Absolute sum of negative (sales) movements."""
        qs = self.ledger_entries.filter(quantity__lt=0)
        if start_date:
            qs = qs.filter(date__gte=start_date)
        if end_date:
            qs = qs.filter(date__lte=end_date)
        total = qs.aggregate(total=models.Sum("quantity"))["total"] or Decimal("0.000")
        return abs(total)

    def closing_quantity(self, end_date=None):
        """
        Closing stock = opening_quantity + Σ(StockLedger.quantity up to end_date).
        Negative movements (outward) naturally subtract from the total.
        """
        qs = self.ledger_entries.all()
        if end_date:
            qs = qs.filter(date__lte=end_date)
        net = qs.aggregate(total=models.Sum("quantity"))["total"] or Decimal("0.000")
        return self.opening_quantity + net

    def weighted_average_cost(self, end_date=None):
        """
        WAC = Σ(purchase_qty × purchase_rate) / Σ(purchase_qty).
        Falls back to purchase_price if no StockLedger purchase entries exist.
        """
        purchases   = self.ledger_entries.filter(quantity__gt=0)
        if end_date:
            purchases = purchases.filter(date__lte=end_date)
        total_qty   = (
            purchases.aggregate(t=models.Sum("quantity"))["t"] or Decimal("0.000")
        )
        if total_qty > 0:
            total_value = sum(e.quantity * e.rate for e in purchases)
            return (total_value / total_qty).quantize(Decimal("0.01"))
        return self.purchase_price

    def closing_stock_value(self, end_date=None):
        """Closing qty × WAC — used for Stock Valuation report."""
        qty = self.closing_quantity(end_date=end_date)
        wac = self.weighted_average_cost(end_date=end_date)
        return (qty * wac).quantize(Decimal("0.01"))

    def is_low_stock(self, end_date=None):
        if self.low_stock_threshold <= 0:
            return False
        return self.closing_quantity(end_date=end_date) < self.low_stock_threshold


# ─────────────────────────────────────────────────────────────────────────────
# StockLedger  — immutable movement log
# ─────────────────────────────────────────────────────────────────────────────

class StockLedger(models.Model):
    """
    One row per stock movement.

    quantity > 0  → Inward  (Purchase voucher)
    quantity < 0  → Outward (Sales voucher)

    Deleted automatically (CASCADE) when the parent Voucher is deleted.
    Always created inside a transaction.atomic() block together with the
    Voucher and VoucherItems so accounting + inventory stay in sync.
    """
    stock_item = models.ForeignKey(
        StockItem, on_delete=models.CASCADE, related_name="ledger_entries"
    )
    voucher    = models.ForeignKey(
        Voucher, on_delete=models.CASCADE, related_name="stock_movements"
    )
    godown     = models.ForeignKey(
        Godown, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="stock_movements",
        help_text="Storage location for this movement (optional).",
    )
    batch      = models.ForeignKey(
        Batch, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="stock_movements",
        help_text="Batch/lot for this movement (optional).",
    )
    date       = models.DateField()
    quantity   = models.DecimalField(
        max_digits=15, decimal_places=3,
        help_text="Positive = inward (purchase), Negative = outward (sales).",
    )
    rate       = models.DecimalField(
        max_digits=15, decimal_places=2,
        help_text="Rate per unit at transaction time.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Stock Ledger Entry"
        verbose_name_plural = "Stock Ledger Entries"
        ordering            = ["date", "created_at"]
        indexes             = [
            models.Index(fields=["stock_item", "date"]),
            models.Index(fields=["voucher"]),
        ]

    def __str__(self):
        direction = "Inward" if self.quantity >= 0 else "Outward"
        return (
            f"{self.stock_item.name} | {direction} {abs(self.quantity)} "
            f"{self.stock_item.unit} @ ₹{self.rate} | {self.date}"
        )

    @property
    def amount(self):
        return (abs(self.quantity) * self.rate).quantize(Decimal("0.01"))


# ─────────────────────────────────────────────────────────────────────────────
# VoucherStockItem  — link between Voucher and StockItem rows
# ─────────────────────────────────────────────────────────────────────────────

class VoucherStockItem(models.Model):
    voucher = models.ForeignKey(
        "vouchers.Voucher", on_delete=models.CASCADE, related_name="voucher_stock_items"
    )
    stock_item = models.ForeignKey(
        "inventory.StockItem",
        on_delete=models.PROTECT,
        related_name="voucher_lines",
    )
    godown = models.ForeignKey(
        "inventory.Godown",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voucher_stock_items",
        help_text="Source/destination godown for this line (optional).",
    )
    batch = models.ForeignKey(
        "inventory.Batch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voucher_stock_items",
        help_text="Batch/lot number for this line (optional).",
    )
    quantity = models.DecimalField(
        max_digits=15, decimal_places=3,
        help_text="Quantity of this stock item in the voucher.",
    )
    rate = models.DecimalField(
        max_digits=15, decimal_places=2,
        help_text="Rate per unit at the time of transaction.",
    )

    class Meta:
        verbose_name = "Voucher Stock Item"
        verbose_name_plural = "Voucher Stock Items"

    def __str__(self):
        return f"{self.stock_item.name} | {self.quantity}"

    def total_amount(self):
        return (abs(self.quantity) * self.rate).quantize(Decimal("0.01"))

    def _assert_parent_editable(self):
        if (
            self.voucher_id
            and self.voucher.status == "APPROVED"
            and not getattr(self.voucher, "_allow_locked_child_edit", False)
        ):
            from django.core.exceptions import ValidationError
            raise ValidationError(
                "Approved vouchers are hard locked. Unapprove the voucher before editing stock lines."
            )

    def save(self, *args, **kwargs):
        self._assert_parent_editable()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._assert_parent_editable()
        return super().delete(*args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# StockValuationEntry — Track cost of lots for FIFO/AVG valuation
# ─────────────────────────────────────────────────────────────────────────────

class StockValuationEntry(models.Model):
    item = models.ForeignKey(
        StockItem, on_delete=models.CASCADE, related_name="valuation_entries"
    )
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    rate = models.DecimalField(max_digits=15, decimal_places=2)
    remaining_quantity = models.DecimalField(
        max_digits=15, decimal_places=3, default=Decimal("0.000")
    )
    date = models.DateField(default=date.today)
    voucher = models.ForeignKey(
        Voucher, on_delete=models.CASCADE, null=True, blank=True, 
        related_name="valuation_entries"
    )

    class Meta:
        verbose_name = "Stock Valuation Entry"
        verbose_name_plural = "Stock Valuation Entries"
        ordering = ["date", "id"]

    def __str__(self):
        return f"{self.item.name} | Qty: {self.quantity} | Rate: {self.rate}"


# ─────────────────────────────────────────────────────────────────────────────
# CompanySettings — Inventory Configuration
# ─────────────────────────────────────────────────────────────────────────────

class CompanySettings(models.Model):
    VALUATION_CHOICES = [
        ('FIFO', 'FIFO'),
        ('AVG', 'Weighted Average'),
    ]
    company = models.OneToOneField(
        Company, on_delete=models.CASCADE, related_name="inventory_settings"
    )
    valuation_method = models.CharField(
        max_length=10, choices=VALUATION_CHOICES, default='AVG'
    )
    prevent_negative_stock = models.BooleanField(
        default=False,
        help_text="Block sales/returns that would take company stock below zero.",
    )

    class Meta:
        verbose_name = "Company Inventory Settings"
        verbose_name_plural = "Company Inventory Settings"

    def __str__(self):
        return f"{self.company.name} Settings"


# ─────────────────────────────────────────────────────────────────────────────
# StockEntry — Non-financial stock adjustments
# ─────────────────────────────────────────────────────────────────────────────

class StockEntry(models.Model):
    """
    Non-financial stock adjustments (Waste, Production, Internal Transfer, etc.)
    that do NOT necessarily have an accounting voucher.
    """
    ENTRY_TYPES = [
        ("Adjustment", "Adjustment"),
        ("Waste",      "Waste"),
        ("Production", "Production"),
        ("Transfer",   "Transfer"),
    ]

    company    = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="stock_entries")
    date       = models.DateField(default=date.today)
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPES, default="Adjustment")
    narration  = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Stock Entry"
        verbose_name_plural = "Stock Entries"
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.entry_type} | {self.date}"


class StockEntryItem(models.Model):
    """Line items for a StockEntry."""
    stock_entry = models.ForeignKey(StockEntry, on_delete=models.CASCADE, related_name="items")
    stock_item  = models.ForeignKey(StockItem, on_delete=models.PROTECT)
    godown      = models.ForeignKey(Godown, on_delete=models.SET_NULL, null=True, blank=True)
    # Positive for addition (production), Negative for reduction (waste/consumption)
    quantity    = models.DecimalField(max_digits=15, decimal_places=3)
    rate        = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0.00"))

    def __str__(self):
        return f"{self.stock_item.name} | {self.quantity}"


class LandedCost(models.Model):
    ALLOCATION_METHODS = [
        ('EQUAL', 'Equal split across items'),
        ('QUANTITY', 'Split by quantity ratio'),
    ]
    voucher = models.OneToOneField(
        "vouchers.Voucher", on_delete=models.CASCADE, related_name="landed_cost",
        limit_choices_to={'voucher_type': 'Purchase'}
    )
    total_extra_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    allocation_method = models.CharField(max_length=10, choices=ALLOCATION_METHODS, default='EQUAL')

    def __str__(self):
        return f"Landed Cost for {self.voucher.number} — ₹{self.total_extra_cost}"
