from django.contrib import admin

from .models import ImportSession


@admin.register(ImportSession)
class ImportSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "company",
        "source_system",
        "sync_mode",
        "status",
        "total_rows",
        "vouchers_count",
        "created_at",
    )
    list_filter = ("source_system", "sync_mode", "status", "created_at")
    search_fields = ("company__name", "user__email", "source_company_guid", "source_file_hash", "import_fingerprint")
    readonly_fields = ("source_file_hash", "import_fingerprint", "created_at")
