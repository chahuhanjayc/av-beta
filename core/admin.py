"""
core/admin.py
"""

from django.contrib import admin, messages
from django.conf import settings
from django.db import transaction, IntegrityError
from django.apps import apps
from django.utils.html import format_html
from django.http import HttpResponseRedirect
from django.urls import reverse
from .models import (
    AuditLog,
    ChecklistItem,
    ClientEngagement,
    Company,
    CompanyStatutoryProfile,
    CompanySettings,
    ComplianceFiling,
    ComplianceNotice,
    FilingReview,
    GSTEvidenceDocument,
    GSTFilingPack,
    GSTPeriodReview,
    GSTPostFilingTracker,
    MarketProofCaseStudy,
    MarketProofExternalEvidence,
    PilotFeedback,
    PracticeTask,
    StatutoryRuleOverride,
    UserCompanyAccess,
)


@admin.register(ChecklistItem)
class ChecklistItemAdmin(admin.ModelAdmin):
    list_display = ["name", "month", "company", "is_completed", "completed_at", "completed_by"]
    list_filter = ["is_completed", "month", "company", "name"]
    search_fields = ["name", "company__name"]
    autocomplete_fields = ["company", "completed_by"]
    actions = ["mark_as_completed"]

    @admin.action(description="Mark selected items as completed")
    def mark_as_completed(self, request, queryset):
        from django.utils import timezone
        queryset.update(
            is_completed=True,
            completed_at=timezone.now(),
            completed_by=request.user
        )
        self.message_user(request, "Items marked as completed.", level=messages.SUCCESS)


@admin.register(PracticeTask)
class PracticeTaskAdmin(admin.ModelAdmin):
    list_display = ["title", "company", "task_type", "priority", "status", "due_date", "assigned_to"]
    list_filter = ["task_type", "priority", "status", "due_date"]
    search_fields = ["title", "company__name", "reference", "description"]
    autocomplete_fields = ["company", "assigned_to", "created_by", "completed_by"]


@admin.register(PilotFeedback)
class PilotFeedbackAdmin(admin.ModelAdmin):
    list_display = ["summary", "company", "feedback_type", "sentiment", "confidence_score", "severity", "status", "occurred_on", "assigned_to"]
    list_filter = ["feedback_type", "sentiment", "severity", "status", "competitor_reference", "occurred_on"]
    search_fields = ["summary", "detail", "company__name", "company__gstin", "client_contact", "evidence_reference"]
    autocomplete_fields = ["company", "assigned_to", "recorded_by", "follow_up_task"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(MarketProofCaseStudy)
class MarketProofCaseStudyAdmin(admin.ModelAdmin):
    list_display = ["title", "company", "status", "outcome", "migration_source", "publish_consent", "commercial_value", "owner", "updated_at"]
    list_filter = ["status", "outcome", "migration_source", "publish_consent", "anonymized", "cutover_date"]
    search_fields = ["title", "company__name", "company__gstin", "testimonial_quote", "evidence_reference", "consent_reference"]
    autocomplete_fields = ["company", "owner", "approved_by", "created_by"]
    readonly_fields = ["created_at", "updated_at", "approved_at", "published_at"]


@admin.register(MarketProofExternalEvidence)
class MarketProofExternalEvidenceAdmin(admin.ModelAdmin):
    list_display = ["title", "company", "category", "status", "source", "due_date", "expires_on", "owner", "verified_by"]
    list_filter = ["category", "status", "source", "due_date", "expires_on"]
    search_fields = ["title", "company__name", "company__gstin", "evidence_reference", "artifact_sha256", "notes"]
    autocomplete_fields = ["company", "owner", "verified_by", "created_by", "follow_up_task"]
    readonly_fields = ["created_at", "updated_at", "verified_at"]


@admin.register(ComplianceFiling)
class ComplianceFilingAdmin(admin.ModelAdmin):
    list_display = ["title", "company", "filing_type", "status", "priority", "due_date", "assigned_to", "reviewer"]
    list_filter = ["filing_type", "status", "priority", "due_date", "source"]
    search_fields = ["title", "company__name", "arn_ack_number", "portal_status", "notes"]
    autocomplete_fields = ["company", "assigned_to", "reviewer", "created_by", "filed_by", "related_task"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(ComplianceNotice)
class ComplianceNoticeAdmin(admin.ModelAdmin):
    list_display = ["title", "company", "notice_type", "status", "priority", "response_due_date", "assigned_to"]
    list_filter = ["notice_type", "status", "priority", "response_due_date"]
    search_fields = ["title", "company__name", "reference_number", "portal_status", "description", "response_summary"]
    autocomplete_fields = ["company", "assigned_to", "created_by", "closed_by", "related_task", "related_filing"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(GSTPeriodReview)
class GSTPeriodReviewAdmin(admin.ModelAdmin):
    list_display = ["company", "period_start", "period_end", "status", "risk_score", "reviewed_by", "reviewed_at"]
    list_filter = ["status", "period_start", "reviewed_at"]
    search_fields = ["company__name", "notes"]
    autocomplete_fields = ["company", "prepared_by", "reviewed_by"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(FilingReview)
class FilingReviewAdmin(admin.ModelAdmin):
    list_display = [
        "company",
        "review_type",
        "period_start",
        "period_end",
        "status",
        "readiness_score",
        "risk_score",
        "reviewed_by",
        "approved_by",
        "approved_at",
    ]
    list_filter = ["review_type", "status", "period_start", "approved_at"]
    search_fields = ["company__name", "notes"]
    autocomplete_fields = ["company", "prepared_by", "reviewed_by", "approved_by", "sent_back_by"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(GSTFilingPack)
class GSTFilingPackAdmin(admin.ModelAdmin):
    list_display = [
        "company",
        "period_start",
        "period_end",
        "status",
        "arn_ack_number",
        "generated_by",
        "filed_by",
        "filed_at",
    ]
    list_filter = ["status", "period_start", "filed_at"]
    search_fields = ["company__name", "arn_ack_number", "notes"]
    autocomplete_fields = ["company", "review", "generated_by", "filed_by"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(GSTPostFilingTracker)
class GSTPostFilingTrackerAdmin(admin.ModelAdmin):
    list_display = [
        "company",
        "period_start",
        "period_end",
        "gstr1_status",
        "gstr3b_status",
        "ims_status",
        "payment_status",
        "itc_at_risk",
        "updated_by",
    ]
    list_filter = ["gstr1_status", "gstr3b_status", "ims_status", "payment_status", "period_start"]
    search_fields = [
        "company__name",
        "gstr1_arn",
        "gstr3b_arn",
        "payment_challan_reference",
        "portal_evidence_reference",
        "notes",
    ]
    autocomplete_fields = ["company", "pack", "updated_by"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(GSTEvidenceDocument)
class GSTEvidenceDocumentAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "company",
        "period_start",
        "evidence_type",
        "return_type",
        "arn_ack_number",
        "challan_reference",
        "uploaded_by",
        "uploaded_at",
    ]
    list_filter = ["evidence_type", "return_type", "period_start", "uploaded_at"]
    search_fields = [
        "title",
        "company__name",
        "external_reference",
        "arn_ack_number",
        "challan_reference",
        "notes",
    ]
    autocomplete_fields = ["company", "tracker", "pack", "filing", "notice", "uploaded_by"]
    readonly_fields = ["uploaded_at"]


@admin.register(CompanySettings)
class CompanySettingsAdmin(admin.ModelAdmin):
    list_display = ["company", "books_closed_until", "inventory_locked_until", "bank_locked_until"]
    autocomplete_fields = ["company"]
    search_fields = ["company__name"]


@admin.register(CompanyStatutoryProfile)
class CompanyStatutoryProfileAdmin(admin.ModelAdmin):
    list_display = [
        "company",
        "gst_registered",
        "gst_return_frequency",
        "gstr1_frequency",
        "tds_applicable",
        "msme_watch_enabled",
        "updated_at",
    ]
    list_filter = ["gst_registered", "gst_return_frequency", "gstr1_frequency", "tds_applicable", "msme_watch_enabled"]
    search_fields = ["company__name", "company__gstin", "company__tan", "rules_notes"]
    autocomplete_fields = ["company", "updated_by"]
    readonly_fields = ["updated_at"]


@admin.register(StatutoryRuleOverride)
class StatutoryRuleOverrideAdmin(admin.ModelAdmin):
    list_display = ["company", "rule_type", "period_start", "period_end", "override_due_date", "is_active", "created_by"]
    list_filter = ["rule_type", "is_active", "override_due_date"]
    search_fields = ["company__name", "reason"]
    autocomplete_fields = ["company", "created_by"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(ClientEngagement)
class ClientEngagementAdmin(admin.ModelAdmin):
    list_display = [
        "company",
        "status",
        "service_package",
        "monthly_retainer",
        "billing_cycle",
        "risk_rating",
        "renewal_date",
        "partner_owner",
    ]
    list_filter = ["status", "service_package", "billing_cycle", "risk_rating", "renewal_date"]
    search_fields = ["company__name", "company__gstin", "scope_summary", "internal_notes"]
    autocomplete_fields = ["company", "partner_owner", "manager_owner"]
    readonly_fields = ["created_at", "updated_at"]


class UserCompanyAccessInline(admin.TabularInline):
    model = UserCompanyAccess
    extra = 1
    autocomplete_fields = ["user"]


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ["name", "short_code", "gstin", "tan", "created_at"]
    search_fields = ["name", "gstin", "tan", "short_code"]
    list_filter = ["created_at"]
    inlines = [UserCompanyAccessInline]
    readonly_fields = ["created_at"]
    fieldsets = (
        (None, {"fields": ("name", "short_code", "gstin", "tan")}),
        ("Details", {"fields": ("address", "financial_year_start")}),
        ("TDS / TRACES", {"fields": ("tds_responsible_person", "tds_responsible_designation")}),
        ("Banking & UPI", {"fields": ("upi_id", "bank_name", "account_number", "ifsc_code")}),
        ("Meta", {"fields": ("created_at",)}),
    )
    actions = ["wipe_all_company_data", "delete_company_completely"]
    destructive_actions = {"wipe_all_company_data", "delete_company_completely"}

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not settings.DEBUG:
            for action_name in self.destructive_actions:
                actions.pop(action_name, None)
        return actions

    def _get_related_data_info(self, obj):
        """Check if the company has any related business data."""
        # 1. Vouchers
        Voucher = apps.get_model('vouchers', 'Voucher')
        if Voucher.objects.filter(company=obj).exists(): return "Vouchers"
        
        # 2. Ledgers
        Ledger = apps.get_model('ledger', 'Ledger')
        if Ledger.objects.filter(company=obj).exists(): return "Ledgers"

        # 3. Inventory
        StockItem = apps.get_model('inventory', 'StockItem')
        if StockItem.objects.filter(company=obj).exists(): return "Inventory Items"

        # 4. OCR
        OCRSubmission = apps.get_model('ocr', 'OCRSubmission')
        if OCRSubmission.objects.filter(company=obj).exists(): return "OCR Submissions"
        
        # 5. Audit & Bank
        if AuditLog.objects.filter(company=obj).exists(): return "Audit Logs"
        BankStatement = apps.get_model('core', 'BankStatement')
        if BankStatement.objects.filter(company=obj).exists(): return "Bank Statements"

        return None

    def delete_view(self, request, object_id, extra_context=None):
        """
        Intercept the delete flow early. If related data exists, block and redirect.
        """
        obj = self.get_object(request, object_id)
        if obj:
            related_name = self._get_related_data_info(obj)
            if related_name:
                self.message_user(
                    request,
                    format_html(
                        "<strong>Deletion Blocked:</strong> Cannot delete company '{}' because it has existing {}. "
                        "Please use the <strong>'Wipe all company data'</strong> action first.",
                        obj.name, related_name
                    ),
                    level=messages.ERROR
                )
                return HttpResponseRedirect(reverse('admin:core_company_change', args=(object_id,)))

        try:
            return super().delete_view(request, object_id, extra_context)
        except IntegrityError:
            self.message_user(request, "Database integrity error: This company is still referenced by other records.", level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:core_company_change', args=(object_id,)))

    def delete_model(self, request, obj):
        """Delete the company record if safe."""
        obj.delete()

    def delete_queryset(self, request, queryset):
        """Bulk delete safety check."""
        for obj in queryset:
            related_name = self._get_related_data_info(obj)
            if related_name:
                self.message_user(request, f"Skipped '{obj.name}': has existing {related_name}.", level=messages.ERROR)
            else:
                try:
                    obj.delete()
                except Exception as e:
                    self.message_user(request, f"Failed to delete '{obj.name}': {str(e)}", level=messages.ERROR)

    @admin.action(description="Wipe all company data (Dangerous)")
    def wipe_all_company_data(self, request, queryset):
        """
        Wipe tool: Clears all related multi-tenant data while keeping the Company record.
        Required order to satisfy PROTECT constraints.
        """
        if not settings.DEBUG:
            self.message_user(
                request,
                "Company wipe actions are disabled outside DEBUG mode.",
                level=messages.ERROR,
            )
            return

        for obj in queryset:
            name = obj.name
            try:
                with transaction.atomic():
                    # 1. OCR Submissions
                    apps.get_model('ocr', 'OCRSubmission').objects.filter(company=obj).delete()
                    
                    # 2. Vouchers & Sequences (vouchers app)
                    # Note: Voucher delete cascades to VoucherItem and StockLedger
                    apps.get_model('vouchers', 'Voucher').objects.filter(company=obj).delete()
                    apps.get_model('vouchers', 'VoucherSequence').objects.filter(company=obj).delete()
                    
                    # 3. Inventory (inventory app)
                    apps.get_model('inventory', 'Batch').objects.filter(company=obj).delete()
                    apps.get_model('inventory', 'Godown').objects.filter(company=obj).delete()
                    apps.get_model('inventory', 'StockItem').objects.filter(company=obj).delete()
                    
                    # 4. Ledgers (ledger app)
                    apps.get_model('ledger', 'Ledger').objects.filter(company=obj).delete()
                    
                    # 5. Cost Centers & Assets (if apps exist)
                    try:
                        apps.get_model('costcenter', 'CostCenter').objects.filter(company=obj).delete()
                    except (LookupError, AttributeError): pass
                    try:
                        apps.get_model('fixedassets', 'FixedAsset').objects.filter(company=obj).delete()
                    except (LookupError, AttributeError): pass
                    
                    # 6. Core Related (Bank Statements, Access)
                    apps.get_model('core', 'BankStatement').objects.filter(company=obj).delete()
                    UserCompanyAccess.objects.filter(company=obj).delete()
                    
                self.message_user(
                    request, 
                    f"Business data for '{name}' was deleted. Audit logs were preserved.",
                    level=messages.SUCCESS
                )
            except Exception as e:
                self.message_user(request, f"Failed to wipe '{name}': {str(e)}", level=messages.ERROR)

    @admin.action(description="Delete company with all related data (Dangerous)")
    def delete_company_completely(self, request, queryset):
        """Full wipe + Delete company record."""
        if not settings.DEBUG:
            self.message_user(
                request,
                "Company delete-with-data actions are disabled outside DEBUG mode.",
                level=messages.ERROR,
            )
            return

        for obj in queryset:
            name = obj.name
            try:
                # Reuse the wipe logic
                self.wipe_all_company_data(request, [obj])
                if AuditLog.objects.filter(company=obj).exists():
                    self.message_user(
                        request,
                        f"Company '{name}' was not deleted because audit logs must be preserved.",
                        level=messages.ERROR,
                    )
                    continue
                # If wipe was successful, the last message would be SUCCESS
                # But we need to actually delete the obj now
                obj.delete()
                self.message_user(request, f"Company '{name}' and all associated records deleted.", level=messages.SUCCESS)
            except Exception as e:
                self.message_user(request, f"Failed to delete '{name}': {str(e)}", level=messages.ERROR)


@admin.register(UserCompanyAccess)
class UserCompanyAccessAdmin(admin.ModelAdmin):
    list_display = ["user", "company", "role", "created_at"]
    search_fields = ["user__email", "company__name"]
    list_filter = ["role", "company"]
    autocomplete_fields = ["user", "company"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display  = ["timestamp", "user", "action", "model_name", "record_id", "object_repr", "company"]
    list_filter   = ["action", "model_name", "company", "timestamp"]
    search_fields = ["object_repr", "user__email", "model_name", "record_id"]
    readonly_fields = [
        "company", "user", "action", "model_name",
        "record_id", "object_repr", "old_data", "new_data", "timestamp",
    ]
    list_select_related = ["user", "company"]
    ordering = ["-timestamp"]

    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False
