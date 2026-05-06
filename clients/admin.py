from django.contrib import admin
from django.utils import timezone
from .models import ClientSubscription, PaymentTransaction

@admin.register(ClientSubscription)
class ClientSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("company", "plan", "status", "subscription_end", "voucher_count_monthly")
    list_filter = ("plan", "status")
    search_fields = ("company__name", "primary_user__email")
    readonly_fields = ("created_at", "updated_at")
    
    actions = ["extend_subscription_30_days", "mark_as_expired"]

    def extend_subscription_30_days(self, request, queryset):
        for sub in queryset:
            sub.subscription_end += timezone.timedelta(days=30)
            sub.status = ClientSubscription.STATUS_ACTIVE
            sub.save()
    extend_subscription_30_days.short_description = "Extend selected by 30 days"

    def mark_as_expired(self, request, queryset):
        queryset.update(status=ClientSubscription.STATUS_EXPIRED)
    mark_as_expired.short_description = "Mark selected as Expired"

@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_id", "client_subscription", "amount", "payment_date", "status")
    list_filter = ("status", "payment_method")
    search_fields = ("transaction_id", "client_subscription__company__name")
    
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # If a payment is marked as completed, update the subscription
        if obj.status == "completed":
            sub = obj.client_subscription
            sub.subscription_end = obj.expiry_date
            sub.last_payment_date = obj.payment_date
            sub.last_payment_amount = obj.amount
            sub.status = ClientSubscription.STATUS_ACTIVE
            sub.save()
