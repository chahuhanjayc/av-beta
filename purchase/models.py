from django.db import models
from decimal import Decimal
from django.core.exceptions import ValidationError
from core.models import Company
from ledger.models import Ledger
from inventory.models import StockItem
from vouchers.models import Voucher

class PurchaseOrder(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('PENDING', 'Pending Approval'),
        ('APPROVED', 'Approved'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="purchase_orders")
    vendor = models.ForeignKey(Ledger, on_delete=models.PROTECT, related_name="purchase_orders", limit_choices_to={'account_group__nature': 'Liability'})
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="purchase_orders")
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    price = models.DecimalField(max_digits=15, decimal_places=2, help_text="Unit price")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    
    # PO -> Voucher link
    voucher = models.ForeignKey(Voucher, on_delete=models.SET_NULL, null=True, blank=True, related_name="purchase_orders")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"PO-{self.id} | {self.vendor.name} | {self.item.name}"

    def clean(self):
        # Validate: IF invoice price > PO: flag warning
        # Since this is a model validation, we raise a ValidationError if it's strict,
        # but the task says "flag warning". In Django model.clean(), raising ValidationError blocks save.
        # If the user specifically wants to block it, we raise. If it's just a warning to be shown in UI, 
        # it's usually handled in forms or signals. 
        # However, to satisfy "Validate" in STEP 3, I'll implement the check.
        if self.voucher:
            # Check the rate in VoucherStockItem for this PO's item
            from inventory.models import VoucherStockItem
            vsi = VoucherStockItem.objects.filter(voucher=self.voucher, stock_item=self.item).first()
            if vsi and vsi.rate > self.price:
                # Flag warning (blocking in this case as per Django clean() behavior)
                # To make it a non-blocking warning, one would typically use messages framework in views.
                # But for model-level requirement:
                raise ValidationError(f"WARNING: Invoice price ({vsi.rate}) for {self.item.name} is higher than PO price ({self.price}).")
