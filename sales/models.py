from django.db import models
from decimal import Decimal
from core.models import Company
from ledger.models import Ledger
from inventory.models import StockItem
from vouchers.models import Voucher

class SalesOrder(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('PENDING', 'Pending Approval'),
        ('APPROVED', 'Approved'),
        ('SHIPPED', 'Shipped'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="sales_orders")
    customer = models.ForeignKey(Ledger, on_delete=models.PROTECT, related_name="sales_orders", limit_choices_to={'account_group__nature': 'Asset'})
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="sales_orders")
    qty = models.DecimalField(max_digits=15, decimal_places=3)
    price = models.DecimalField(max_digits=15, decimal_places=2, help_text="Unit price")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    
    # Sales Order -> Invoice link
    invoice = models.ForeignKey(Voucher, on_delete=models.SET_NULL, null=True, blank=True, related_name="sales_orders", help_text="Linked Sales Invoice")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"SO-{self.id} | {self.customer.name} | {self.item.name}"
