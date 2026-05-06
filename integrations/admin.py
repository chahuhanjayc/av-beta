from django.contrib import admin

from .models import IntegrationConnector, IntegrationRequestLog, IntegrationRetryJob, StatutoryExportLog


@admin.register(IntegrationRequestLog)
class IntegrationRequestLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "company", "service", "provider", "status", "voucher")
    list_filter = ("service", "status", "provider", "created_at")
    search_fields = ("company__name", "voucher__number", "request_id", "error_message")
    readonly_fields = (
        "request_id",
        "company",
        "voucher",
        "requested_by",
        "provider",
        "service",
        "status",
        "request_digest",
        "response_code",
        "response_payload",
        "error_message",
        "created_at",
    )


@admin.register(IntegrationConnector)
class IntegrationConnectorAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "connector_type",
        "provider_name",
        "mode",
        "status",
        "last_success_at",
        "updated_at",
    )
    list_filter = ("connector_type", "mode", "status", "updated_at")
    search_fields = ("company__name", "provider_name", "gstin", "tan", "username", "credential_reference")
    readonly_fields = ("created_at", "updated_at", "last_success_at", "last_failure_at")
    fieldsets = (
        (None, {
            "fields": ("company", "connector_type", "display_name", "provider_name", "mode", "status"),
        }),
        ("Identity", {
            "fields": ("gstin", "tan", "username", "base_url", "credential_reference", "credential_last_rotated_at"),
        }),
        ("Operational Status", {
            "fields": ("last_success_at", "last_failure_at", "last_error", "metadata", "notes"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
        }),
    )


@admin.register(IntegrationRetryJob)
class IntegrationRetryJobAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "company",
        "service",
        "provider",
        "status",
        "priority",
        "attempts",
        "next_attempt_at",
    )
    list_filter = ("service", "status", "priority", "created_at", "next_attempt_at")
    search_fields = ("company__name", "provider", "last_error", "request_log__request_id", "voucher__number")
    readonly_fields = ("created_at", "updated_at", "resolved_at")
    fieldsets = (
        (None, {
            "fields": ("company", "connector", "request_log", "voucher", "service", "provider"),
        }),
        ("Retry State", {
            "fields": ("status", "priority", "attempts", "max_attempts", "next_attempt_at", "last_error"),
        }),
        ("Payload Evidence", {
            "fields": ("request_payload", "response_payload"),
        }),
        ("Ownership", {
            "fields": ("created_by", "resolved_by", "resolved_at"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
        }),
    )


@admin.register(StatutoryExportLog)
class StatutoryExportLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "company", "export_type", "status", "period_start", "period_end", "file_name")
    list_filter = ("export_type", "status", "created_at")
    search_fields = ("company__name", "file_name", "file_sha256", "portal_reference")
    readonly_fields = (
        "company",
        "connector",
        "generated_by",
        "export_type",
        "status",
        "period_start",
        "period_end",
        "file_name",
        "file_sha256",
        "row_count",
        "amount_total",
        "validation_summary",
        "portal_reference",
        "created_at",
    )
