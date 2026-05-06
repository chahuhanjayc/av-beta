from django.db import models
from core.models import Company

class ReconciliationEntry(models.Model):
    SOURCE_CHOICES = [
        ('BANK', 'Bank Statement'),
        ('GST', 'GSTR-2B Portal'),
    ]
    STATUS_CHOICES = [
        ('MATCHED', 'Matched'),
        ('MISSING_IN_BOOKS', 'Missing in Books'),
        ('MISSING_IN_PORTAL', 'Missing in Portal'),
        ('MISMATCH', 'Amount/Date Mismatch'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="reconciliation_entries")
    source_type = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    reference_number = models.CharField(max_length=100, help_text="Transaction Ref or Invoice Number")
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    date = models.DateField()
    
    # Reconciliation Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='MISSING_IN_BOOKS')
    matched_voucher = models.ForeignKey(
        'vouchers.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name="reconciliation_matches"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.source_type} | {self.reference_number} | {self.amount}"


class GSTR2BEntry(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="gstr2b_entries")
    gstin = models.CharField(max_length=15, verbose_name="Supplier GSTIN")
    invoice_number = models.CharField(max_length=50)
    invoice_date = models.DateField()
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, help_text="Total GST (IGST or CGST+SGST)")
    matched = models.BooleanField(default=False)
    matched_voucher = models.ForeignKey(
        'vouchers.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name="gstr2b_matches"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "GSTR-2B Entry"
        verbose_name_plural = "GSTR-2B Entries"
        unique_together = ("company", "gstin", "invoice_number")

    def __str__(self):
        return f"{self.gstin} | {self.invoice_number} | ₹{self.tax_amount}"


class BankEntry(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="bank_entries")
    date = models.DateField()
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    description = models.TextField()
    matched = models.BooleanField(default=False)
    matched_voucher = models.ForeignKey(
        'vouchers.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name="bank_matches"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Bank Entry"
        verbose_name_plural = "Bank Entries"
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date} | ₹{self.amount} | {self.description[:30]}"
