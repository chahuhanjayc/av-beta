"""
ocr/admin.py
"""

from django.contrib import admin
from django.utils.html import format_html
from .models import OCRSubmission


@admin.register(OCRSubmission)
class OCRSubmissionAdmin(admin.ModelAdmin):
    list_display = [
        "pk", "company", "filename_display", "status",
        "vendor_name", "total_amount", "confidence_display",
        "has_duplicate", "created_at",
    ]
    list_filter         = ["status", "company", "created_at"]
    search_fields       = ["extracted_text", "company__name"]
    list_select_related = ["company", "duplicate_of"]
    readonly_fields     = [
        "extracted_text", "parsed_json", "created_at", "updated_at",
        "linked_voucher", "ocr_error", "task_id", "duplicate_of",
    ]
    date_hierarchy = "created_at"

    fieldsets = (
        ("Submission",  {"fields": ("company", "file", "status", "task_id")}),
        ("OCR Output",  {"fields": ("extracted_text", "parsed_json", "ocr_error")}),
        ("Duplicate",   {"fields": ("duplicate_of",)}),
        ("Result",      {"fields": ("linked_voucher",)}),
        ("Timestamps",  {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    # ── Custom column methods ─────────────────────────────────────────────────

    def filename_display(self, obj):
        return obj.filename()
    filename_display.short_description = "File"

    def vendor_name(self, obj):
        return obj.parsed_json.get("vendor_name", "—")
    vendor_name.short_description = "Vendor"

    def total_amount(self, obj):
        amt = obj.parsed_json.get("total_amount", "")
        return f"₹{amt}" if amt else "—"
    total_amount.short_description = "Total"

    def confidence_display(self, obj):
        score = obj.parsed_json.get("confidence_score", None)
        if score is None:
            return "—"
        colour = "green" if score >= 70 else ("orange" if score >= 40 else "red")
        return format_html(
            '<span style="color:{};font-weight:600;">{} %</span>',
            colour, score,
        )
    confidence_display.short_description = "Confidence"

    def has_duplicate(self, obj):
        if obj.duplicate_of_id:
            return format_html(
                '<a href="/admin/ocr/ocrsubmission/{}/change/" '
                'style="color:orange;">⚠ #{}</a>',
                obj.duplicate_of_id, obj.duplicate_of_id,
            )
        return "—"
    has_duplicate.short_description = "Duplicate Of"
