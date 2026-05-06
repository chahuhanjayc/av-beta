from django.db import models
from django.conf import settings
from core.models import Company

class ImportSession(models.Model):
    STATUS_CHOICES = [
        ('uploaded', 'Uploaded'),
        ('parsed', 'Parsed'),
        ('confirmed', 'Confirmed'),
        ('failed', 'Failed'),
    ]
    SOURCE_TALLY_EXCEL = 'tally_excel'
    SOURCE_TALLY_XML = 'tally_xml'
    SOURCE_GENERIC_EXCEL = 'generic_excel'
    SOURCE_GENERIC_CSV = 'generic_csv'
    SOURCE_CHOICES = [
        (SOURCE_TALLY_EXCEL, 'Tally Excel Export'),
        (SOURCE_TALLY_XML, 'Tally XML Export'),
        (SOURCE_GENERIC_EXCEL, 'Generic Excel'),
        (SOURCE_GENERIC_CSV, 'Generic CSV'),
    ]
    SYNC_ONE_TIME = 'one_time'
    SYNC_INCREMENTAL = 'incremental'
    SYNC_REPLACE_PERIOD = 'replace_period'
    SYNC_MODE_CHOICES = [
        (SYNC_ONE_TIME, 'One-time Import'),
        (SYNC_INCREMENTAL, 'Incremental Sync'),
        (SYNC_REPLACE_PERIOD, 'Replace Selected Period'),
    ]
    APPROVAL_PENDING = 'pending'
    APPROVAL_APPROVED = 'approved'
    APPROVAL_REVOKED = 'revoked'
    APPROVAL_STATUS_CHOICES = [
        (APPROVAL_PENDING, 'Pending Review'),
        (APPROVAL_APPROVED, 'Approved'),
        (APPROVAL_REVOKED, 'Revoked'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    file = models.FileField(upload_to='migrations/%Y/%m/%d/')
    file_type = models.CharField(max_length=10, choices=[('excel', 'Excel'), ('csv', 'CSV')])
    source_system = models.CharField(max_length=30, choices=SOURCE_CHOICES, default=SOURCE_TALLY_EXCEL)
    sync_mode = models.CharField(max_length=30, choices=SYNC_MODE_CHOICES, default=SYNC_ONE_TIME)
    source_company_guid = models.CharField(max_length=80, blank=True)
    source_period_start = models.DateField(null=True, blank=True)
    source_period_end = models.DateField(null=True, blank=True)
    source_file_hash = models.CharField(max_length=64, blank=True, db_index=True)
    import_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    approval_status = models.CharField(max_length=20, choices=APPROVAL_STATUS_CHOICES, default=APPROVAL_PENDING, db_index=True)
    approval_checklist = models.JSONField(default=dict, blank=True)
    approval_note = models.TextField(blank=True)
    approval_snapshot = models.JSONField(default=dict, blank=True)
    approval_evidence_hash = models.CharField(max_length=64, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_import_sessions',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='revoked_import_approvals',
    )
    approval_revoked_at = models.DateTimeField(null=True, blank=True)
    approval_revoke_note = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='uploaded')
    raw_preview = models.JSONField(null=True, blank=True)
    detected_mapping = models.JSONField(null=True, blank=True)
    total_rows = models.IntegerField(default=0)
    vouchers_count = models.IntegerField(default=0)
    opening_balances_count = models.IntegerField(default=0)
    total_debit = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    total_credit = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    ledger_mapping = models.JSONField(null=True, blank=True)
    skipped_rows = models.JSONField(null=True, blank=True)
    detected_opening_balances = models.JSONField(null=True, blank=True)
    validation_report = models.JSONField(null=True, blank=True)
    duplicate_voucher_count = models.IntegerField(default=0)
    unbalanced_voucher_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Session {self.id} - {self.company.name} ({self.status})"
