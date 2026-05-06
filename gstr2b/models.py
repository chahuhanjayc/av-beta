from django.db import models
from core.models import Company

class PortalGSTR2BEntry(models.Model):
    MATCH_STATUS_CHOICES = [
        ('matched', 'Matched'),
        ('missing_in_books', 'Missing in Books'),
        ('missing_in_portal', 'Missing in Portal'),
    ]
    ACTION_STATUS_CHOICES = [
        ("new", "New"),
        ("accepted", "Accepted"),
        ("pending", "Pending"),
        ("rejected", "Rejected"),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="portal_gstr2b_entries")
    gstin = models.CharField(max_length=15)
    supplier_name = models.CharField(max_length=255, null=True, blank=True)
    invoice_number = models.CharField(max_length=50)
    invoice_date = models.DateField()
    taxable_value = models.DecimalField(max_digits=20, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=20, decimal_places=2)
    
    is_matched = models.BooleanField(default=False)
    match_status = models.CharField(max_length=20, choices=MATCH_STATUS_CHOICES)
    matched_voucher = models.ForeignKey(
        "vouchers.Voucher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="portal_gstr2b_matches",
    )
    match_score = models.PositiveSmallIntegerField(default=0)
    action_status = models.CharField(
        max_length=20,
        choices=ACTION_STATUS_CHOICES,
        default="new",
    )
    action_note = models.CharField(max_length=300, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Portal GSTR-2B Entry"
        verbose_name_plural = "Portal GSTR-2B Entries"
        unique_together = ('company', 'gstin', 'invoice_number', 'invoice_date')

    def __str__(self):
        return f"{self.gstin} - {self.invoice_number} ({self.match_status})"
