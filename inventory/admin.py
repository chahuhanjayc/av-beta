"""
inventory/admin.py

Django admin registration for Inventory models.
"""

from django.contrib import admin
from .models import HSN_SAC, TaxRate, StockItem, StockLedger, VoucherStockItem, CompanySettings


@admin.register(HSN_SAC)
class HSN_SACAdmin(admin.ModelAdmin):
    list_display  = ("code", "description")
    search_fields = ("code", "description")
    ordering      = ("code",)


@admin.register(TaxRate)
class TaxRateAdmin(admin.ModelAdmin):
    list_display  = ("rate", "description")
    ordering      = ("rate",)


class StockLedgerInline(admin.TabularInline):
    model          = StockLedger
    extra          = 0
    readonly_fields = ("voucher", "date", "quantity", "rate", "created_at")
    can_delete     = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(StockItem)
class StockItemAdmin(admin.ModelAdmin):
    list_display   = (
        "name", "company", "unit", "opening_quantity",
        "purchase_price", "selling_price", "is_active",
    )
    list_filter    = ("company", "unit", "is_active")
    search_fields  = ("name", "company__name")
    list_select_related = ("company", "hsn_sac", "tax_rate")
    inlines        = [StockLedgerInline]
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {
            "fields": ("company", "name", "unit", "is_active"),
        }),
        ("Pricing", {
            "fields": ("purchase_price", "selling_price"),
        }),
        ("Stock", {
            "fields": ("opening_quantity", "low_stock_threshold", "prevent_negative_stock"),
        }),
        ("GST", {
            "fields": ("hsn_sac", "tax_rate"),
            "classes": ("collapse",),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )


@admin.register(StockLedger)
class StockLedgerAdmin(admin.ModelAdmin):
    list_display        = ("stock_item", "date", "quantity", "rate", "voucher")
    list_filter         = ("stock_item__company", "date")
    search_fields       = ("stock_item__name", "voucher__number")
    list_select_related = ("stock_item", "voucher")
    readonly_fields     = ("created_at",)
    ordering            = ("-date", "-created_at")

    def has_add_permission(self, request):
        # Stock ledger entries are always created programmatically via vouchers
        return False


@admin.register(VoucherStockItem)
class VoucherStockItemAdmin(admin.ModelAdmin):
    list_display        = ("voucher", "stock_item", "quantity", "rate")
    list_select_related = ("voucher", "stock_item")
    search_fields       = ("voucher__number", "stock_item__name")
    ordering            = ("-voucher__date",)


@admin.register(CompanySettings)
class CompanySettingsAdmin(admin.ModelAdmin):
    list_display = ("company", "valuation_method", "prevent_negative_stock")
    list_filter = ("valuation_method", "prevent_negative_stock")
    search_fields = ("company__name",)
