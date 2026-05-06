"""
forex/models.py — Phase 8A: Multi-currency Support

Models:
  Currency      — Currency master (INR is base, others are foreign)
  ExchangeRate  — Daily exchange rate table (1 foreign unit = X INR)
"""

from decimal import Decimal
from django.db import models
from django.core.exceptions import ValidationError
from core.models import Company


class Currency(models.Model):
    """Currency master. One currency per company should be is_base=True."""
    company     = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="currencies")
    code        = models.CharField(max_length=3, help_text="ISO 4217 code, e.g. USD, EUR, GBP")
    name        = models.CharField(max_length=50, help_text="e.g. US Dollar")
    symbol      = models.CharField(max_length=5, default="$")
    is_base     = models.BooleanField(
        default=False,
        help_text="Mark as True for the base/home currency (typically INR). Only one per company."
    )
    is_active   = models.BooleanField(default=True)

    class Meta:
        verbose_name        = "Currency"
        verbose_name_plural = "Currencies"
        ordering            = ["code"]
        unique_together     = ("company", "code")

    def __str__(self):
        return f"{self.code} — {self.name}"

    def clean(self):
        if self.is_base:
            qs = Currency.objects.filter(company=self.company, is_base=True)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError("Only one base currency is allowed per company.")

    @property
    def latest_rate(self):
        """Return the most recent exchange rate (1 unit of this currency = X base units)."""
        er = self.exchange_rates.order_by("-date").first()
        return er.rate if er else Decimal("1.00")


class ExchangeRate(models.Model):
    """Daily exchange rate: 1 unit of currency = rate units of base currency."""
    SOURCE_CHOICES = [
        ("RBI", "RBI (Reserve Bank of India)"),
        ("SBI", "SBI (State Bank of India)"),
        ("FEDAI", "FEDAI (Foreign Exchange Dealers' Association of India)"),
        ("Customs", "CBIC (Customs Exchange Rates)"),
        ("Manual", "Manual/Other"),
    ]
    currency    = models.ForeignKey(Currency, on_delete=models.CASCADE, related_name="exchange_rates")
    date        = models.DateField(help_text="Effective date of this rate")
    rate        = models.DecimalField(
        max_digits=18, decimal_places=6,
        help_text="How many base-currency units = 1 unit of this currency (e.g. 1 USD = 83.5 INR)"
    )
    buying_rate  = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True,
                                       help_text="Bank buying rate (optional)")
    selling_rate = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True,
                                       help_text="Bank selling rate (optional)")
    source       = models.CharField(
        max_length=50, 
        choices=SOURCE_CHOICES,
        default="Manual",
        help_text="The source or reference for this exchange rate"
    )
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Exchange Rate"
        verbose_name_plural = "Exchange Rates"
        ordering            = ["-date"]
        unique_together     = ("currency", "date")

    def __str__(self):
        return f"1 {self.currency.code} = {self.rate} on {self.date}"

    @classmethod
    def get_rate(cls, currency, date):
        """
        Get the most recent exchange rate for a currency on or before a given date.
        Returns 1.0 if the currency is the base currency or no rate found.
        """
        if currency.is_base:
            return Decimal("1.000000")
        er = cls.objects.filter(currency=currency, date__lte=date).order_by("-date").first()
        return er.rate if er else Decimal("1.000000")
