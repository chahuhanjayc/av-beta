from django.conf import settings
from django.db import models
from django.utils import timezone


class PersonalCategory(models.Model):
    name = models.CharField(max_length=100)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    icon = models.CharField(max_length=50, default="bi-tag")
    color = models.CharField(max_length=20, default="#4f46e5")

    class Meta:
        verbose_name_plural = "Personal Categories"
        unique_together = ("name", "user")
        ordering = ["name"]

    def __str__(self):
        return self.name


class PersonalExpense(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    date = models.DateField(default=timezone.now)
    category = models.ForeignKey(PersonalCategory, on_delete=models.CASCADE)
    item_name = models.CharField(max_length=120, blank=True)
    description = models.CharField(max_length=255, blank=True)
    payment_method = models.CharField(max_length=50, default="Cash")
    quantity = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        label = self.item_name or self.category.name
        return f"{self.date} - {label} - {self.amount}"


class PersonalIncome(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    date = models.DateField(default=timezone.now)
    source = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date} - {self.source} - {self.amount}"


class PersonalBudget(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    category = models.ForeignKey(PersonalCategory, on_delete=models.CASCADE)
    monthly_limit = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        unique_together = ("user", "category")
        ordering = ["category__name"]

    def __str__(self):
        return f"{self.category.name} - INR {self.monthly_limit}"


class PersonalExpenseTemplate(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    category = models.ForeignKey(PersonalCategory, on_delete=models.CASCADE)
    item_name = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=50, default="Cash")
    quantity = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "name")
        ordering = ["name"]

    def __str__(self):
        return self.name


class PersonalFinanceSettings(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    currency_code = models.CharField(max_length=10, default="INR")
    billing_start_day = models.PositiveSmallIntegerField(default=1)
    payment_methods = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"SpendSight Settings - {self.user}"
