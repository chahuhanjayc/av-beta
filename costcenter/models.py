"""
costcenter/models.py

Phase 4 — Cost Centers & Budgeting.

Models:
  CostCenter   — A department, project, or profit center (company-scoped).
                 Can be tagged on individual VoucherItem rows so income/expense
                 can be broken down beyond just ledger-group level.

  BudgetHead   — A budgeted amount for a specific Ledger in a given financial
                 year.  Variance = Actual (from VoucherItems) - Budget.
"""

from decimal import Decimal
from django.db import models
from core.models import Company
from ledger.models import Ledger


# ─────────────────────────────────────────────────────────────────────────────
# CostCenter
# ─────────────────────────────────────────────────────────────────────────────

class CostCenter(models.Model):
    """
    A named cost / profit center to which income and expense entries are allocated.

    Examples: Sales Team, Marketing, R&D, Project Alpha, Warehouse.
    VoucherItems carry an optional FK to CostCenter so reports can slice P&L
    by center rather than just by ledger group.
    """
    company     = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="cost_centers"
    )
    name        = models.CharField(max_length=200)
    category    = models.CharField(max_length=100, blank=True, help_text="e.g. Project, Department, etc.")
    code        = models.CharField(
        max_length=20, blank=True,
        help_text="Short code for quick reference, e.g. MKT, SALES, WH.",
    )
    description = models.CharField(max_length=300, blank=True)
    is_active   = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Cost Center"
        verbose_name_plural = "Cost Centers"
        ordering            = ["name"]
        unique_together     = ("company", "name")

    def __str__(self):
        if self.code:
            return f"[{self.code}] {self.name}"
        return self.name


# ─────────────────────────────────────────────────────────────────────────────
# BudgetHead
# ─────────────────────────────────────────────────────────────────────────────

class BudgetHead(models.Model):
    """
    A budget target for a specific Ledger within a financial year.

    financial_year example: "2024-25"
    period: "Annual" or a month abbreviation "Apr", "May", … "Mar"
    """

    PERIOD_CHOICES = [
        ("Annual", "Annual (Full Year)"),
        ("Apr", "April"),  ("May", "May"),  ("Jun", "June"),
        ("Jul", "July"),   ("Aug", "August"), ("Sep", "September"),
        ("Oct", "October"),("Nov", "November"),("Dec", "December"),
        ("Jan", "January"),("Feb", "February"),("Mar", "March"),
    ]

    company         = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="budget_heads"
    )
    ledger          = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, related_name="budget_heads",
        help_text="The ledger account this budget applies to.",
    )
    cost_center     = models.ForeignKey(
        CostCenter, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="budget_heads",
        help_text="Restrict this budget to a specific cost center (optional).",
    )
    financial_year  = models.CharField(
        max_length=7,
        help_text="e.g. 2024-25",
    )
    period          = models.CharField(
        max_length=10, choices=PERIOD_CHOICES, default="Annual",
    )
    budgeted_amount = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0.00"),
        help_text="Target amount for this period.",
    )
    notes           = models.CharField(max_length=300, blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Budget Head"
        verbose_name_plural = "Budget Heads"
        ordering            = ["financial_year", "ledger__name", "period"]
        unique_together     = ("company", "ledger", "cost_center", "financial_year", "period")

    def __str__(self):
        cc = f" / {self.cost_center.name}" if self.cost_center_id else ""
        return (
            f"{self.ledger.name}{cc} — {self.financial_year} / {self.period}: "
            f"₹{self.budgeted_amount:,.2f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Cost Allocation
# ─────────────────────────────────────────────────────────────────────────────

class CostAllocationRule(models.Model):
    METHOD_CHOICES = [
        ('PERCENTAGE', 'Fixed Percentage'),
        ('REVENUE', 'Based on Revenue'),
        ('HEADCOUNT', 'Based on Headcount'),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="allocation_rules")
    ledger = models.ForeignKey(
        Ledger, on_delete=models.CASCADE, related_name="allocation_rules",
        help_text="The indirect expense ledger to allocate. If blank, applies to all unallocated indirect expenses.",
        null=True, blank=True
    )
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default='PERCENTAGE')
    
    class Meta:
        verbose_name = "Cost Allocation Rule"
        verbose_name_plural = "Cost Allocation Rules"

    def __str__(self):
        l_name = self.ledger.name if self.ledger else "All Indirect Expenses"
        return f"{l_name} ({self.method})"


class AllocationPercentage(models.Model):
    rule = models.ForeignKey(CostAllocationRule, on_delete=models.CASCADE, related_name="percentages")
    cost_center = models.ForeignKey(CostCenter, on_delete=models.CASCADE)
    percentage = models.DecimalField(max_digits=5, decimal_places=2, help_text="Percentage (0-100)")

    class Meta:
        unique_together = ("rule", "cost_center")


class Budget(models.Model):
    cost_center   = models.ForeignKey(CostCenter, on_delete=models.CASCADE, related_name="monthly_budgets")
    monthly_limit = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0.00"))
    year          = models.PositiveSmallIntegerField()
    month         = models.PositiveSmallIntegerField(help_text="1-12 (Jan-Dec)")

    class Meta:
        unique_together = ("cost_center", "year", "month")
        ordering = ["-year", "-month", "cost_center__name"]

    def __str__(self):
        return f"{self.cost_center.name} — {self.month}/{self.year}: ₹{self.monthly_limit:,.2f}"
