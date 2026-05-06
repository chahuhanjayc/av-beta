"""
orders/models.py — Phase 5: Purchase & Sales Orders
"""

from decimal import Decimal
from django.db import models
from django.core.exceptions import ValidationError

from core.models import Company
from ledger.models import Ledger
from inventory.models import StockItem


class Order(models.Model):

    ORDER_TYPE_CHOICES = [("Purchase", "Purchase Order"), ("Sales", "Sales Order")]
    STATUS_DRAFT     = "Draft"
    STATUS_CONFIRMED = "Confirmed"
    STATUS_PARTIAL   = "Partially Fulfilled"
    STATUS_FULFILLED = "Fulfilled"
    STATUS_CANCELLED = "Cancelled"
    STATUS_CHOICES = [
        ("Draft",                "Draft"),
        ("Confirmed",            "Confirmed"),
        ("Partially Fulfilled",  "Partially Fulfilled"),
        ("Fulfilled",            "Fulfilled"),
        ("Cancelled",            "Cancelled"),
    ]

    company       = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="orders")
    order_type    = models.CharField(max_length=10, choices=ORDER_TYPE_CHOICES)
    number        = models.CharField(max_length=30, blank=True)
    party_ledger  = models.ForeignKey(
        Ledger, on_delete=models.PROTECT, related_name="orders",
        help_text="Supplier (PO) or Customer (SO).",
    )
    order_date    = models.DateField()
    expected_date = models.DateField(null=True, blank=True, help_text="Expected delivery / dispatch date.")
    status        = models.CharField(max_length=25, choices=STATUS_CHOICES, default="Draft")
    narration     = models.TextField(blank=True)
    fulfilled_voucher = models.ForeignKey(
        "vouchers.Voucher", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="source_orders",
    )
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Order"
        verbose_name_plural = "Orders"
        ordering            = ["-order_date", "-created_at"]
        indexes             = [models.Index(fields=["company", "status", "order_type"])]

    def __str__(self):
        return f"{self.order_type} Order {self.number or self.pk} — {self.party_ledger.name}"

    def save(self, *args, **kwargs):
        if not self.number:
            super().save(*args, **kwargs)
            prefix = "PO" if self.order_type == "Purchase" else "SO"
            self.number = f"{prefix}-{self.pk:05d}"
            Order.objects.filter(pk=self.pk).update(number=self.number)
            return
        super().save(*args, **kwargs)

    @property
    def total_amount(self):
        return sum(item.amount for item in self.items.all())

    @property
    def is_editable(self):
        return self.status in ("Draft", "Confirmed")


class OrderItem(models.Model):
    order       = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    stock_item  = models.ForeignKey(
        StockItem, on_delete=models.PROTECT, related_name="order_items",
        null=True, blank=True,
    )
    description   = models.CharField(max_length=255, blank=True)
    quantity      = models.DecimalField(max_digits=15, decimal_places=3)
    rate          = models.DecimalField(max_digits=15, decimal_places=2)
    fulfilled_qty = models.DecimalField(max_digits=15, decimal_places=3, default=Decimal("0.000"))

    class Meta:
        verbose_name = "Order Item"

    def __str__(self):
        name = self.stock_item.name if self.stock_item_id else self.description
        return f"{name} × {self.quantity} @ ₹{self.rate}"

    @property
    def amount(self):
        return (self.quantity * self.rate).quantize(Decimal("0.01"))

    @property
    def pending_qty(self):
        return self.quantity - self.fulfilled_qty

    def clean(self):
        if not self.stock_item_id and not self.description:
            raise ValidationError("Provide either a stock item or a description.")
