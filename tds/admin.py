from django.contrib import admin

from .models import (
    TDSCertificateIssue,
    TDSFilingPack,
    TDSPostFilingTracker,
    TDSReturnWorkpaper,
    TDSSection,
    TDSEntry,
)


@admin.register(TDSSection)
class TDSSectionAdmin(admin.ModelAdmin):
    list_display = ["company", "nature", "section_code", "description", "is_active"]
    list_filter = ["nature", "is_active"]
    search_fields = ["company__name", "section_code", "description"]
    raw_id_fields = ["company"]


@admin.register(TDSEntry)
class TDSEntryAdmin(admin.ModelAdmin):
    list_display = [
        "company",
        "transaction_date",
        "section",
        "deductee_ledger",
        "tds_amount",
        "is_deposited",
        "challan_number",
    ]
    list_filter = ["is_deposited", "transaction_date", "section__nature"]
    search_fields = ["company__name", "deductee_ledger__name", "pan_number", "challan_number", "bsr_code"]
    raw_id_fields = ["company", "section", "deductee_ledger", "tds_ledger", "voucher"]


@admin.register(TDSReturnWorkpaper)
class TDSReturnWorkpaperAdmin(admin.ModelAdmin):
    list_display = [
        "company",
        "form_type",
        "financial_year_start",
        "quarter",
        "status",
        "due_date",
        "ack_number",
    ]
    list_filter = ["form_type", "quarter", "status", "due_date"]
    search_fields = ["company__name", "ack_number", "traces_token"]
    raw_id_fields = ["company", "prepared_by", "reviewed_by", "filed_by"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(TDSFilingPack)
class TDSFilingPackAdmin(admin.ModelAdmin):
    list_display = [
        "company",
        "form_type",
        "financial_year_start",
        "quarter",
        "status",
        "due_date",
        "ack_number",
    ]
    list_filter = ["form_type", "quarter", "status", "due_date"]
    search_fields = ["company__name", "ack_number", "notes"]
    raw_id_fields = ["company", "workpaper", "generated_by", "filed_by"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(TDSPostFilingTracker)
class TDSPostFilingTrackerAdmin(admin.ModelAdmin):
    list_display = [
        "pack",
        "statement_status",
        "correction_required",
        "correction_status",
        "status_checked_at",
        "updated_by",
    ]
    list_filter = ["statement_status", "correction_required", "correction_status"]
    search_fields = ["pack__company__name", "traces_request_number", "justification_request_number", "conso_request_number"]
    raw_id_fields = ["pack", "updated_by"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(TDSCertificateIssue)
class TDSCertificateIssueAdmin(admin.ModelAdmin):
    list_display = [
        "pack",
        "entry_serial",
        "certificate_type",
        "deductee_name",
        "deductee_pan",
        "tds_amount",
        "status",
        "issued_at",
    ]
    list_filter = ["certificate_type", "status", "issue_channel"]
    search_fields = ["pack__company__name", "deductee_name", "deductee_pan", "request_number", "evidence_reference"]
    raw_id_fields = ["pack", "deductee_ledger", "issued_by"]
    readonly_fields = ["created_at", "updated_at"]
