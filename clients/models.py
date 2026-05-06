from django.db import models
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from core.models import Company

class ClientSubscription(models.Model):
    PLAN_BASIC = "basic"
    PLAN_PRO = "pro"
    PLAN_ENTERPRISE = "enterprise"
    
    PLAN_CHOICES = [
        (PLAN_BASIC, "Basic (50 Vouchers/mo)"),
        (PLAN_PRO, "Pro (500 Vouchers/mo)"),
        (PLAN_ENTERPRISE, "Enterprise (Unlimited)"),
    ]
    
    STATUS_ACTIVE = "active"
    STATUS_TRIAL = "trial"
    STATUS_EXPIRED = "expired"
    STATUS_SUSPENDED = "suspended"
    
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_TRIAL, "Trial"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_SUSPENDED, "Suspended"),
    ]

    company = models.OneToOneField(
        Company, on_delete=models.CASCADE, related_name="subscription"
    )
    primary_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="managed_subscriptions"
    )
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default=PLAN_BASIC)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_TRIAL)
    
    subscription_start = models.DateTimeField(default=timezone.now)
    subscription_end = models.DateTimeField()
    
    last_payment_date = models.DateTimeField(null=True, blank=True)
    last_payment_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    
    voucher_count_monthly = models.PositiveIntegerField(default=0)
    last_reset_date = models.DateTimeField(default=timezone.now)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Client Subscription"
        verbose_name_plural = "Client Subscriptions"

    def __str__(self):
        return f"{self.company.name} - {self.get_plan_display()}"

    def is_active(self):
        if self.status == self.STATUS_SUSPENDED:
            return False
        return self.subscription_end > timezone.now()

    def get_voucher_limit(self):
        limits = {
            self.PLAN_BASIC: 50,
            self.PLAN_PRO: 500,
            self.PLAN_ENTERPRISE: 999999,
        }
        return limits.get(self.plan, 50)

    def usage_percentage(self):
        limit = self.get_voucher_limit()
        if limit == 0: return 100
        return min(round((self.voucher_count_monthly / limit) * 100), 100)


class PaymentTransaction(models.Model):
    METHOD_CHOICES = [
        ("upi", "UPI"),
        ("card", "Credit/Debit Card"),
        ("bank", "Bank Transfer"),
    ]
    
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]

    client_subscription = models.ForeignKey(
        ClientSubscription, on_delete=models.CASCADE, related_name="payments"
    )
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    payment_date = models.DateTimeField(default=timezone.now)
    payment_method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    transaction_id = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    expiry_date = models.DateTimeField(help_text="The new subscription end date after this payment.")

    class Meta:
        verbose_name = "Payment Transaction"
        verbose_name_plural = "Payment Transactions"
        ordering = ["-payment_date"]

    def __str__(self):
        return f"Payment {self.transaction_id} - {self.amount}"
