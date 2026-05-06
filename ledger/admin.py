"""
ledger/admin.py
"""

from django.contrib import admin
from .models import Ledger, AccountGroup


@admin.register(AccountGroup)
class AccountGroupAdmin(admin.ModelAdmin):
    list_display = ["name", "company", "nature", "parent"]
    list_filter = ["nature", "company"]
    search_fields = ["name"]


@admin.register(Ledger)
class LedgerAdmin(admin.ModelAdmin):
    list_display = ["name", "company", "account_group", "opening_balance", "is_active", "created_at"]
    list_filter = ["account_group__nature", "company", "is_active"]
    search_fields = ["name", "company__name", "gstin", "email", "whatsapp_number"]
    list_select_related = ["company", "account_group"]
    readonly_fields = ["created_at"]
    fieldsets = (
        (None, {"fields": ("company", "name", "account_group", "opening_balance", "is_active")}),
        ("Statutory & GST", {"fields": ("gstin", "pan_number", "email", "whatsapp_number", "address")}),
        ("MSME (MSMED Act)", {"fields": ("is_msme", "msme_reg_number")}),
        ("TDS", {"fields": ("tds_section", "tds_rate", "tds_threshold")}),
        ("Meta", {"fields": ("created_at",)}),
    )
