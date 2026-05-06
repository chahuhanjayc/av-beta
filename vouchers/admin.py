"""
vouchers/admin.py
"""

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.html import format_html
from .models import Voucher, VoucherItem, VoucherSequence


class VoucherItemInline(admin.TabularInline):
    model = VoucherItem
    fk_name = "voucher"          # disambiguates from reference_voucher FK
    extra = 2
    fields = ["ledger", "entry_type", "amount", "narration", "reference_voucher"]
    autocomplete_fields = ["ledger"]


@admin.register(Voucher)
class VoucherAdmin(admin.ModelAdmin):
    list_display = [
        "number", "company", "voucher_type", "date",
        "balanced_status", "narration", "created_at",
    ]
    list_filter = ["voucher_type", "company", "date"]
    search_fields = ["number", "narration", "company__name"]
    readonly_fields = ["number", "created_at", "updated_at"]
    date_hierarchy = "date"
    inlines = [VoucherItemInline]
    list_select_related = ["company"]

    fieldsets = (
        (None, {"fields": ("company", "number", "voucher_type", "date", "narration")}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def balanced_status(self, obj):
        if obj.is_balanced():
            return format_html('<span style="color:green;font-weight:bold;">✔ Balanced</span>')
        return format_html('<span style="color:red;font-weight:bold;">✘ Unbalanced</span>')

    balanced_status.short_description = "Status"

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        voucher = form.instance
        try:
            voucher.validate_balance()
        except ValidationError as exc:
            raise ValidationError({"items": exc.messages})

    def delete_model(self, request, obj):
        self._delete_with_inventory_rebuild([obj])

    def delete_queryset(self, request, queryset):
        self._delete_with_inventory_rebuild(queryset)

    def _delete_with_inventory_rebuild(self, vouchers):
        from inventory.valuation_utils import rebuild_valuation_for_items

        affected_stock_items = set()
        with transaction.atomic():
            for voucher in vouchers:
                if voucher.status == "APPROVED":
                    raise ValidationError(
                        "Approved vouchers are hard locked. Unapprove before deleting."
                    )
                for movement in voucher.stock_movements.select_related("batch"):
                    affected_stock_items.add(movement.stock_item_id)
                    if movement.batch:
                        movement.batch.quantity -= movement.quantity
                        movement.batch.save(update_fields=["quantity"])
                voucher.delete()
            rebuild_valuation_for_items(affected_stock_items)


@admin.register(VoucherItem)
class VoucherItemAdmin(admin.ModelAdmin):
    list_display = ["voucher", "ledger", "entry_type", "amount"]
    search_fields = ["voucher__number", "ledger__name"]
    list_select_related = ["voucher", "ledger"]
    autocomplete_fields = ["ledger"]


@admin.register(VoucherSequence)
class VoucherSequenceAdmin(admin.ModelAdmin):
    list_display = ["company", "financial_year", "last_number"]
    list_filter = ["company", "financial_year"]
    readonly_fields = ["company", "financial_year", "last_number"]
