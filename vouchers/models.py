"""
vouchers/models.py

Voucher + VoucherItem models.

DOUBLE ENTRY RULE:
  Every voucher saved must have ΣDebit == ΣCredit.
  Enforced at model level (clean) AND in the view (atomic transaction).

VOUCHER NUMBER FORMAT:
  {ShortCode}{FY}-{SEQUENCE:05d}   e.g.  ABC2425-00001
  Generated thread-safely with select_for_update on VoucherSequence.

BILL-TO-BILL TRACKING:
  VoucherItem.reference_voucher is an optional FK back to another Voucher.
  It records "this payment/receipt line settles that specific invoice."
  Used by the Outstanding Statement report to compute unsettled balances.

Changelog:
  - validate_balance(): public method for view-level atomic enforcement
  - clean(): delegates to validate_balance(); guards against admin inline edge case
  - is_balanced(), total_debit(), total_credit(): switched to DB aggregation
  - VoucherItem.clean(): added zero-line guard (both Dr and Cr == 0 is invalid)
"""

from decimal import Decimal
from datetime import timezone as dt_timezone, timedelta
import logging
from django.conf import settings
from django.db import models, transaction
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.utils import timezone

from core.models import Company
from ledger.models import Ledger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sequence counter — one row per (company, financial_year)
# ---------------------------------------------------------------------------
class VoucherSequence(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    financial_year = models.CharField(max_length=10, help_text="e.g. 2425")
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("company", "financial_year")

    def __str__(self):
        return f"{self.company.short_code}{self.financial_year}-{self.last_number:05d}"


# ---------------------------------------------------------------------------
# Voucher
# ---------------------------------------------------------------------------
class Voucher(models.Model):
    VOUCHER_TYPE_CHOICES = [
        ("Payment",  "Payment"),
        ("Receipt",  "Receipt"),
        ("Sales",    "Sales"),
        ("Purchase", "Purchase"),
        ("Sales Return", "Sales Return"),
        ("Purchase Return", "Purchase Return"),
        ("Contra",   "Contra"),
        ("Journal",  "Journal"),
        ("Stock Transfer", "Stock Transfer"),
    ]

    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('PENDING', 'Pending Approval'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected')
    ]

    # Indian state codes for place of supply (GST)
    STATE_CHOICES = [
        ("01", "01 - Jammu & Kashmir"),      ("02", "02 - Himachal Pradesh"),
        ("03", "03 - Punjab"),               ("04", "04 - Chandigarh"),
        ("05", "05 - Uttarakhand"),          ("06", "06 - Haryana"),
        ("07", "07 - Delhi"),                ("08", "08 - Rajasthan"),
        ("09", "09 - Uttar Pradesh"),        ("10", "10 - Bihar"),
        ("11", "11 - Sikkim"),               ("12", "12 - Arunachal Pradesh"),
        ("13", "13 - Nagaland"),             ("14", "14 - Manipur"),
        ("15", "15 - Mizoram"),              ("16", "16 - Tripura"),
        ("17", "17 - Meghalaya"),            ("18", "18 - Assam"),
        ("19", "19 - West Bengal"),          ("20", "20 - Jharkhand"),
        ("21", "21 - Odisha"),               ("22", "22 - Chhattisgarh"),
        ("23", "23 - Madhya Pradesh"),       ("24", "24 - Gujarat"),
        ("26", "26 - Dadra & Nagar Haveli and Daman & Diu"),
        ("27", "27 - Maharashtra"),          ("28", "28 - Andhra Pradesh"),
        ("29", "29 - Karnataka"),            ("30", "30 - Goa"),
        ("31", "31 - Lakshadweep"),          ("32", "32 - Kerala"),
        ("33", "33 - Tamil Nadu"),           ("34", "34 - Puducherry"),
        ("35", "35 - Andaman & Nicobar Islands"),
        ("36", "36 - Telangana"),            ("37", "37 - Andhra Pradesh (New)"),
        ("38", "38 - Ladakh"),
        ("97", "97 - Other Territory"),      ("99", "99 - Centre Jurisdiction"),
    ]

    TRANSPORT_MODE_CHOICES = [
        ("1", "Road"),
        ("2", "Rail"),
        ("3", "Air"),
        ("4", "Ship"),
    ]

    VEHICLE_TYPE_CHOICES = [
        ("R", "Regular"),
        ("O", "Over Dimensional Cargo"),
    ]

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="vouchers"
    )
    number = models.CharField(max_length=30, blank=True, editable=False)
    date = models.DateField(default=timezone.now)
    due_date = models.DateField(
        null=True, blank=True,
        help_text="Payment due date — used for Receivables Aging report.",
    )
    voucher_type = models.CharField(max_length=20, choices=VOUCHER_TYPE_CHOICES)
    narration = models.TextField(blank=True, help_text="Brief description / memo")

    # Forex
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=4, default=1.0)

    # Maker-Checker Fields
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="verified_vouchers")
    verified_at = models.DateTimeField(null=True, blank=True)

    # GST fields
    place_of_supply = models.CharField(
        max_length=2, blank=True, null=True,
        choices=STATE_CHOICES,
        verbose_name="Place of Supply",
        help_text="2-digit state code for GST (e.g. 24 for Gujarat). Required for GSTR-1.",
    )
    voucher_class = models.ForeignKey(
        'VoucherClass', on_delete=models.SET_NULL, null=True, blank=True,
        related_name="vouchers",
        help_text="Pre-defined class for auto-tax or default behavior."
    )
    reverse_charge = models.BooleanField(
        default=False,
        verbose_name="Reverse Charge",
        help_text="Is this transaction under the Reverse Charge Mechanism (RCM)?",
    )

    # GST Summary fields
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_tax = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    is_itc_claimed = models.BooleanField(
        default=False,
        help_text="For Purchases: Has ITC been realized via GSTR-2B matching?"
    )
    compliance_notes = models.TextField(null=True, blank=True, help_text="System-generated compliance alerts and audit notes.")
    e_invoice_irn = models.CharField(max_length=100, blank=True, help_text="IRN returned by the e-invoice provider.")
    e_invoice_ack_no = models.CharField(max_length=50, blank=True, help_text="E-invoice acknowledgement number.")
    e_invoice_ack_date = models.DateTimeField(null=True, blank=True, help_text="E-invoice acknowledgement date/time.")
    e_invoice_status = models.CharField(max_length=30, blank=True, help_text="Latest e-invoice status returned by the provider/IRP.")
    e_invoice_signed_invoice = models.JSONField(null=True, blank=True, help_text="Signed e-invoice JSON returned by IRP/GSP.")
    e_invoice_signed_qr_code = models.TextField(blank=True, help_text="Signed QR code payload returned by IRP/GSP.")
    e_way_bill_no = models.CharField(max_length=30, blank=True, help_text="E-way bill number returned by the provider.")
    e_way_bill_date = models.DateTimeField(null=True, blank=True, help_text="E-way bill generation date/time.")
    e_way_bill_status = models.CharField(max_length=30, blank=True, help_text="Latest e-way bill status returned by the provider.")
    e_way_bill_valid_until = models.DateTimeField(null=True, blank=True, help_text="E-way bill validity expiry returned by the provider.")
    dispatch_pincode = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Dispatch pincode for e-invoice/e-way bill payloads.",
    )
    ship_to_pincode = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Ship-to pincode for e-invoice/e-way bill payloads.",
    )
    transport_mode = models.CharField(
        max_length=1,
        choices=TRANSPORT_MODE_CHOICES,
        blank=True,
        help_text="E-way bill transport mode.",
    )
    transport_distance_km = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Approximate transport distance in kilometres for e-way bill.",
    )
    transporter_id = models.CharField(
        max_length=15,
        blank=True,
        help_text="Transporter GSTIN or enrolment ID for e-way bill.",
    )
    transporter_name = models.CharField(
        max_length=120,
        blank=True,
        help_text="Transporter name for e-way bill.",
    )
    transport_doc_no = models.CharField(
        max_length=30,
        blank=True,
        help_text="Transport document or LR/RR/AWB number.",
    )
    transport_doc_date = models.DateField(
        null=True,
        blank=True,
        help_text="Transport document date.",
    )
    vehicle_number = models.CharField(
        max_length=20,
        blank=True,
        help_text="Vehicle number for road e-way bill movement.",
    )
    vehicle_type = models.CharField(
        max_length=1,
        choices=VEHICLE_TYPE_CHOICES,
        blank=True,
        help_text="Vehicle type for e-way bill.",
    )

    # Bill-to-Bill fields
    reference_voucher = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="referenced_by",
        help_text="Link to another voucher (e.g. Payment link to Invoice)"
    )
    
    # PO Matching
    po_reference = models.ForeignKey(
        'orders.Order', on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fulfilled_vouchers",
        help_text="Reference Purchase Order for this invoice."
    )
    po_mismatch_qty = models.BooleanField(default=False, help_text="Flagged if OCR quantity differs from PO.")
    po_mismatch_rate = models.BooleanField(default=False, help_text="Flagged if OCR rate differs from PO.")

    outstanding_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0.00,
        help_text="Unsettled balance for this voucher."
    )
    document = models.FileField(
        upload_to='voucher_docs/', 
        null=True, 
        blank=True,
        help_text="Attach a scan/PDF of the physical invoice or supporting document."
    )
    source_system = models.CharField(max_length=30, blank=True, help_text="External source system, e.g. tally.")
    source_reference = models.CharField(max_length=120, blank=True, help_text="External voucher/reference number from the source system.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Voucher"
        verbose_name_plural = "Vouchers"
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["company", "source_system", "source_reference"], name="voucher_source_ref_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "number"],
                condition=~models.Q(number=""),
                name="uniq_voucher_number_per_company",
            )
        ]

    def __str__(self):
        return f"{self.number} | {self.voucher_type} | {self.date}"

    def approve(self, user):
        """Approve the voucher and set verification details."""
        self.check_tds_deduction()
        self.clean()
        self.status = 'APPROVED'
        self.verified_by = user
        self.verified_at = timezone.now()
        self.save(update_fields=['status', 'verified_by', 'verified_at'])
        self.sync_inventory()
        self.sync_referenced_vouchers()

    def unapprove(self, user=None):
        """Move an approved voucher back to pending so it can be edited."""
        if self.status != 'APPROVED':
            return
        self._allow_unapprove = True
        self.status = 'PENDING'
        self.verified_by = None
        self.verified_at = None
        self.save(update_fields=['status', 'verified_by', 'verified_at'])
        self.sync_inventory()
        self.sync_referenced_vouchers()

    def sync_referenced_vouchers(self):
        """Refresh invoice outstanding amounts affected by this voucher's lines."""
        referenced_ids = (
            self.items
            .exclude(reference_voucher__isnull=True)
            .values_list("reference_voucher_id", flat=True)
            .distinct()
        )
        for voucher in type(self).objects.filter(pk__in=referenced_ids):
            voucher.sync_outstanding()

    # ------------------------------------------------------------------
    # Thread-safe voucher number generation
    # ------------------------------------------------------------------
    @staticmethod
    def _financial_year_code(date):
        """Return 4-char code like '2425' for FY 2024-25."""
        year = date.year
        month = date.month
        if month >= 4:  # April onwards -> new FY
            fy_start = year
            fy_end = year + 1
        else:
            fy_start = year - 1
            fy_end = year
        return f"{str(fy_start)[2:]}{str(fy_end)[2:]}"

    def generate_number(self):
        """Generate the next voucher number for this company + FY. Thread-safe."""
        fy_code = self._financial_year_code(self.date)
        current_fy = self._financial_year_code(timezone.now().date())

        with transaction.atomic():
            # If back-dating into a previous FY, pre-initialize current FY sequence.
            if fy_code < current_fy:
                VoucherSequence.objects.get_or_create(
                    company=self.company,
                    financial_year=current_fy,
                    defaults={"last_number": 0},
                )

            seq, _ = VoucherSequence.objects.select_for_update().get_or_create(
                company=self.company,
                financial_year=fy_code,
                defaults={"last_number": 0},
            )
            seq.last_number += 1
            seq.save(update_fields=["last_number"])
            short = self.company.short_code or "VCH"
            return f"{short}{fy_code}-{seq.last_number:05d}"

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        is_new = self.pk is None
        
        old_instance = None
        if not is_new:
            try:
                old_instance = Voucher.objects.get(pk=self.pk)
            except Voucher.DoesNotExist:
                pass

        if is_new:
            # Voucher numbers are system assigned only. Ignore any supplied value.
            self.number = ""
        elif old_instance:
            if self.number != old_instance.number:
                raise ValidationError("Voucher number is system generated and cannot be changed.")

            if old_instance.status == 'APPROVED':
                update_set = set(update_fields or [])
                status_fields = {'status', 'verified_by', 'verified_at'}
                system_fields = {
                    'outstanding_amount',
                    'compliance_notes',
                    'is_itc_claimed',
                    'e_invoice_irn',
                    'e_invoice_ack_no',
                    'e_invoice_ack_date',
                    'e_invoice_status',
                    'e_invoice_signed_invoice',
                    'e_invoice_signed_qr_code',
                    'e_way_bill_no',
                    'e_way_bill_date',
                    'e_way_bill_status',
                    'e_way_bill_valid_until',
                    'updated_at',
                }

                if self.status != 'APPROVED':
                    if not getattr(self, '_allow_unapprove', False):
                        raise ValidationError(
                            "Approved vouchers are hard locked. Unapprove the voucher before editing it."
                        )
                    if update_set and not update_set.issubset(status_fields):
                        raise ValidationError(
                            "Approved vouchers can only be unapproved before other edits are made."
                        )
                else:
                    ignored_fields = status_fields | system_fields | {
                        'id', 'created_at', 'updated_at',
                    }
                    changed_locked_fields = [
                        field.name
                        for field in self._meta.concrete_fields
                        if field.name not in ignored_fields
                        and getattr(self, field.name) != getattr(old_instance, field.name)
                    ]
                    locked_update_requested = (
                        bool(update_set) and not update_set.issubset(status_fields | system_fields)
                    )
                    if changed_locked_fields or locked_update_requested:
                        raise ValidationError(
                            "Approved vouchers are hard locked. Unapprove the voucher before editing it."
                        )

        if not self.number and not is_new:
            raise ValidationError("Existing vouchers must retain their assigned voucher number.")

        # Smart Approval Routing
        if not update_fields:
            # Rule 1: High Value Check (> 50,000)
            # Rule 2: PO Mismatch Check
            if self.status != 'APPROVED': # Don't override manual approval
                is_risky = False
                
                # Check Amount (using total_debit or total_credit)
                if not is_new and self.total_debit() > Decimal("50000.00"):
                    is_risky = True
                
                # Check PO Mismatches
                if self.po_mismatch_qty or self.po_mismatch_rate:
                    is_risky = True
                
                if is_risky:
                    self.status = 'PENDING'

        if not update_fields:
            # If we have a PK and items exist, try to apply class logic BEFORE clean
            # so that tax lines are added before balance validation.
            if not is_new and self.voucher_class and self.voucher_class.tax_behavior == 'AUTO_GST':
                self.create_tax_lines()
            
            self.clean()

        if is_new:
            self.number = self.generate_number()
        
        super().save(*args, **kwargs)

        # Statutory / TDS Automation. Approval and system-only saves run their
        # own validation path, so they must not try to add accounting lines here.
        post_save_tds_fields = set(update_fields or [])
        skip_post_save_tds = (
            self.status == 'APPROVED'
            or (
                post_save_tds_fields
                and post_save_tds_fields.issubset(
                    {
                        'status', 'verified_by', 'verified_at',
                        'outstanding_amount', 'compliance_notes', 'is_itc_claimed',
                    }
                )
            )
        )
        if not skip_post_save_tds:
            self.check_tds_deduction()

        # WhatsApp Approval Request (Phase 8)
        if self.status == 'PENDING' and self.pk and self.items.exists() and not getattr(self, '_whatsapp_sent', False):
            from .whatsapp_utils import send_whatsapp_approval
            send_whatsapp_approval(self)
            self._whatsapp_sent = True

        # Update outstanding calculation for invoices after save
        if self.voucher_type in ["Sales", "Purchase"]:
            with transaction.atomic():
                # Lock for update to prevent concurrent payment mapping issues
                v = Voucher.objects.select_for_update().get(pk=self.pk)
                current_out = v.calculate_outstanding()
                if v.outstanding_amount != current_out:
                    v.outstanding_amount = current_out
                    v.save(update_fields=['outstanding_amount'])

        # Sync reference voucher if any (to reduce its outstanding)
        if self.reference_voucher:
            self.reference_voucher.sync_outstanding()

        # Increment subscription usage for new vouchers
        if is_new:
            try:
                sub = self.company.subscription
                sub.voucher_count_monthly += 1
                sub.save(update_fields=["voucher_count_monthly"])
            except AttributeError:
                pass

    def check_tds_deduction(self):
        """
        Automated TDS Deduction for Purchase Vouchers.
        Step 1: Check ledger has tds_rate and amount > threshold.
        Step 2: AUTO CREATE ENTRY: DR Expense (full), CR Party (net), CR TDS Payable (tds).
        Step 3: CALCULATION: tds = amount * tds_rate / 100.
        """
        if self.voucher_type != "Purchase" or not self.pk:
            return

        from decimal import Decimal
        from ledger.models import Ledger, AccountGroup

        # Avoid double deduction if already processed
        if self.items.filter(ledger__name__icontains="TDS Payable").exists():
            return

        for item in self.items.filter(entry_type='DR'):
            ledger = item.ledger
            party_line = self.items.filter(entry_type='CR').exclude(ledger__name__icontains="TDS Payable").first()
            party_ledger = party_line.ledger if party_line else None
            tds_config_ledger = party_ledger if party_ledger and party_ledger.tds_rate > 0 else ledger

            if tds_config_ledger.tds_rate > 0 and item.amount > tds_config_ledger.tds_threshold:
                tds_amt = (item.amount * tds_config_ledger.tds_rate / Decimal("100.00")).quantize(Decimal("0.01"))
                
                if tds_amt > 0:
                    # Find or create TDS Payable ledger
                    section = tds_config_ledger.tds_section or "General"
                    tds_ledger, _ = Ledger.objects.get_or_create(
                        company=self.company,
                        name=f"TDS Payable ({section})",
                        defaults={
                            "account_group": AccountGroup.objects.get_or_create(
                                company=self.company, name="Duties & Taxes", nature="Tax"
                            )[0]
                        }
                    )
                    
                    # Create TDS CR entry
                    VoucherItem.objects.create(
                        voucher=self,
                        ledger=tds_ledger,
                        entry_type='CR',
                        amount=tds_amt,
                        narration=f"Auto TDS @{tds_config_ledger.tds_rate}% on {item.amount}"
                    )
                    
                    # Adjust Party CR entry (usually the first CR line in a Purchase voucher)
                    if party_line:
                        if party_line.amount <= tds_amt:
                            raise ValidationError(
                                "Auto TDS could not safely adjust the party line. "
                                "Review the voucher amount and TDS setup."
                            )
                        party_line.amount -= tds_amt
                        party_line.save(update_fields=['amount'])

                    try:
                        from tds.models import TDSSection, TDSEntry
                        tds_section, _ = TDSSection.objects.get_or_create(
                            company=self.company,
                            section_code=section,
                            defaults={
                                "description": f"Auto-created for {section}",
                                "threshold": tds_config_ledger.tds_threshold,
                                "rate_company": tds_config_ledger.tds_rate,
                            },
                        )
                        TDSEntry.objects.get_or_create(
                            company=self.company,
                            voucher=self,
                            section=tds_section,
                            deductee_ledger=party_ledger or ledger,
                            defaults={
                                "tds_ledger": tds_ledger,
                                "transaction_date": self.date,
                                "deductee_type": "Company",
                                "deductible_amount": item.amount,
                                "rate_applied": tds_config_ledger.tds_rate,
                                "tds_amount": tds_amt,
                                "pan_number": (party_ledger.pan_number if party_ledger else ledger.pan_number) or "",
                                "notes": f"Auto-created from voucher {self.number}",
                            },
                        )
                    except Exception:
                        pass

                    logger.info(
                        "Auto TDS applied on voucher %s: amount=%s rate=%s base=%s",
                        self.number,
                        tds_amt,
                        tds_config_ledger.tds_rate,
                        item.amount,
                    )
                    # No need to call validate_balance here as it's balanced by the adjustment

    def check_statutory_thresholds(self):
        """
        Check if any ledger's account group has exceeded its statutory threshold limit.
        """
        if not self.pk:
            return

        def has_approved_override():
            override_user = getattr(self, '_override_user', None)
            override_reason = (
                getattr(self, '_statutory_override_reason', None)
                or getattr(self, '_override_reason', None)
                or f"Bypass Statutory Threshold for Voucher {self.number}"
            )
            if not override_user:
                return False

            from audit.models import LockOverrideRequest, OverrideLog

            approved_req = LockOverrideRequest.objects.filter(
                user=override_user,
                reason=override_reason,
                status='APPROVED',
            ).first()
            if approved_req:
                OverrideLog.objects.create(
                    user=override_user,
                    reason=override_reason,
                    action=f"USED: Statutory threshold override for Voucher {self.number}",
                )
                return True

            LockOverrideRequest.objects.get_or_create(
                user=override_user,
                reason=override_reason,
                defaults={'status': 'PENDING'},
            )
            return False

        has_tds_line = self.items.filter(ledger__name__icontains="TDS Payable").exists()
        violations = []

        if self.voucher_type == "Purchase" and not has_tds_line:
            party_line = self.items.filter(entry_type='CR').exclude(
                ledger__name__icontains="TDS Payable"
            ).first()
            party_ledger = party_line.ledger if party_line else None

            for item in self.items.filter(entry_type='DR').select_related("ledger"):
                ledger = item.ledger
                tds_config_ledger = (
                    party_ledger
                    if party_ledger and party_ledger.tds_rate > 0
                    else ledger
                )
                if (
                    tds_config_ledger.tds_rate > 0
                    and item.amount > tds_config_ledger.tds_threshold
                ):
                    violations.append(
                        f"TDS threshold breached for '{tds_config_ledger.name}'. "
                        f"Amount Rs.{item.amount:.2f} exceeds threshold "
                        f"Rs.{tds_config_ledger.tds_threshold:.2f}, but no TDS Payable line is present."
                    )

        groups_to_check = set()
        for item in self.items.select_related("ledger__account_group"):
            if item.ledger.account_group.threshold_limit > 0:
                groups_to_check.add(item.ledger.account_group)

        for group in groups_to_check:
            # Sum balances of all ledgers in this group
            total_bal = Decimal("0.00")
            for ledger in group.ledgers.all():
                # We use abs() because current_balance() might return negative for Dr/Cr
                # but threshold is usually on the total volume/balance.
                total_bal += abs(ledger.current_balance())

            if total_bal > group.threshold_limit and not has_tds_line:
                violations.append(
                    f"Group '{group.name}' threshold Rs.{group.threshold_limit:.2f} "
                    f"exceeded. Current balance is Rs.{total_bal:.2f}, but no "
                    "TDS Payable line or approved override is present."
                )

        if violations and not has_approved_override():
            raise ValidationError("Statutory threshold blocked: " + " ".join(violations))

    # ------------------------------------------------------------------
    # Double-entry validation
    # ------------------------------------------------------------------
    def clean(self):
        """
        Django admin calls clean() after saving the parent Voucher but before
        saving inline VoucherItems — so self.pk may exist but self.items may
        still be empty on first creation. We only validate when items exist.

        For programmatic creation (views, management commands), call
        validate_balance() explicitly AFTER saving all VoucherItems, inside
        an atomic transaction block, to enforce the double-entry rule.
        """
        # Period Locking Check
        from core.models import CompanySettings
        from audit.models import OverrideLog, LockOverrideRequest
        
        override_reason = getattr(self, '_override_reason', None)
        override_user = getattr(self, '_override_user', None)

        def check_override(action):
            if not override_reason or not override_user:
                return False
            
            # Step 2: Check if approved request exists
            approved_req = LockOverrideRequest.objects.filter(
                user=override_user,
                reason=override_reason,
                status='APPROVED'
            ).first()

            if approved_req:
                # Log in OverrideLog as per Step 4
                OverrideLog.objects.create(
                    user=override_user,
                    reason=override_reason,
                    action=f"USED: {action}"
                )
                return True
            
            # If no approved request, check for pending one
            pending_req = LockOverrideRequest.objects.filter(
                user=override_user,
                reason=override_reason,
                status='PENDING'
            ).first()

            if not pending_req:
                # WHEN user tries override: → create request (Step 2)
                LockOverrideRequest.objects.create(
                    user=override_user,
                    reason=override_reason,
                    status='PENDING'
                )
            
            return False

        try:
            settings = self.company.settings
            if settings.books_closed_until and self.date <= settings.books_closed_until:
                if not check_override(f"Bypass Period Lock for Voucher {self.number}"):
                    raise ValidationError(
                        f"Period Locked! Books are closed until {settings.books_closed_until}. "
                        f"An approval request has been created. Please contact an admin."
                    )
            
            # Inventory Locking
            is_inventory = self.voucher_type in ["Sales", "Purchase", "Sales Return", "Purchase Return", "Stock Transfer"]
            if is_inventory and settings.inventory_locked_until and self.date <= settings.inventory_locked_until:
                if not check_override(f"Bypass Inventory Lock for Voucher {self.number}"):
                    raise ValidationError(
                        f"Inventory Locked! Stock vouchers are locked until {settings.inventory_locked_until}. "
                        f"An approval request has been created. Please contact an admin."
                    )

            # Bank Locking
            is_bank = self.voucher_type in ["Payment", "Receipt", "Contra"]
            if is_bank and settings.bank_locked_until and self.date <= settings.bank_locked_until:
                if not check_override(f"Bypass Bank Lock for Voucher {self.number}"):
                    raise ValidationError(
                        f"Bank Locked! Bank vouchers are locked until {settings.bank_locked_until}. "
                        f"An approval request has been created. Please contact an admin."
                    )
        except ValidationError:
            raise
        except CompanySettings.DoesNotExist:
            pass
        except Exception:
            pass

        # MSME Overdue Check: Block Payments if any MSME invoice is > 44 days overdue
        if self.voucher_type == 'Payment' and self.status != 'REJECTED':
            today = timezone.now().date()
            overdue_limit = today - timedelta(days=44)
            
            overdue_msme_exists = Voucher.objects.filter(
                company=self.company,
                voucher_type='Purchase',
                date__lt=overdue_limit,
                outstanding_amount__gt=0,
                items__ledger__is_msme=True,
                status='APPROVED'
            ).exists()
            
            if overdue_msme_exists:
                raise ValidationError(
                    "Payment Blocked! One or more MSME invoices are overdue (> 44 days). "
                    "MSME regulations require payments within 45 days. "
                    "Please settle overdue MSME invoices before making further payments."
                )

        # --- REAL-TIME COMPLIANCE (AUTO-AUDITOR) ---
        alerts = []
        
        if self.pk:
            # 1. Cash Limit Rule (Section 40A(3))
            if self.voucher_type == 'Payment' and self.status != 'REJECTED':
                # Check if any item uses a Cash ledger and total > 10,000
                is_cash_payment = self.items.filter(
                    ledger__name__icontains='Cash',
                    entry_type='CR'
                ).exists()
                if is_cash_payment and self.total_debit() > Decimal("10000.00"):
                    msg = "BLOCK: Cash payment exceeds ₹10,000 limit (Section 40A(3) violation)."
                    self.compliance_notes = msg
                    raise ValidationError(msg)

            # 2. Blocked ITC Rule (OCR Keywords)
            if self.voucher_type in ['Purchase', 'Journal'] and self.narration:
                blocked_keywords = ['hotel', 'restaurant', 'food', 'car repair', 'motor vehicle']
                narration_lower = self.narration.lower()
                if any(k in narration_lower for k in blocked_keywords):
                    if self.total_tax > 0 and not self.reverse_charge:
                        alerts.append("ROUTING: Potential Blocked ITC detected due to keywords. GST routed to Expense.")

            # 3. TDS Consistency Rule
            if self.voucher_type == 'Purchase' and self.status != 'REJECTED':
                for item in self.items.filter(entry_type='DR'):
                    ledger = item.ledger
                    if ledger.tds_rate > 0:
                        from tds.models import TDSEntry
                        previous_tds = TDSEntry.objects.filter(
                            company=self.company,
                            deductee_ledger=ledger
                        ).order_by('-transaction_date').first()
                        
                        if previous_tds and previous_tds.rate_applied != ledger.tds_rate:
                            alerts.append(f"WARNING: TDS rate inconsistent for {ledger.name}. Previous: {previous_tds.rate_applied}%, Current: {ledger.tds_rate}%")

        # 4. PO Mismatch (3-Way Match)
        if self.po_mismatch_qty or self.po_mismatch_rate:
            alerts.append(f"WARNING: PO Mismatch detected (Qty: {self.po_mismatch_qty}, Rate: {self.po_mismatch_rate}).")

        if alerts:
            self.compliance_notes = "\n".join(alerts)

        # 3-Way Match Enforcement: Block Payment if Purchase has PO Mismatch
        if self.voucher_type == 'Payment' and self.status != 'REJECTED':
            # Check for direct reference link
            if self.reference_voucher and self.reference_voucher.voucher_type == 'Purchase':
                rv = self.reference_voucher
                if rv.po_mismatch_qty or rv.po_mismatch_rate:
                    # Allow override only if specifically flagged by an admin view
                    is_admin_override = getattr(self, '_is_admin_override', False)
                    if not is_admin_override:
                        raise ValidationError(
                            f"Payment Blocked due to PO Mismatch on Purchase Invoice {rv.number}! "
                            f"(Qty Mismatch: {rv.po_mismatch_qty}, Rate Mismatch: {rv.po_mismatch_rate}). "
                            f"Please verify invoice or obtain admin approval."
                        )

        # Credit Control Logic for Sales
        if self.voucher_type == "Sales" and self.pk and self.items.exists():
            # Identify the customer ledger (Usually the Asset/Debtor line in a Sales voucher)
            customer_line = self.items.filter(ledger__account_group__nature="Asset").first()
            if customer_line:
                customer = customer_line.ledger
                
                # 1. Check Credit Limit
                if customer.credit_limit is not None:
                    from django.db.models import Sum
                    # Calculate existing outstanding from other unpaid sales invoices
                    existing_outstanding = Voucher.objects.filter(
                        company=self.company,
                        voucher_type="Sales",
                        items__ledger=customer,
                        outstanding_amount__gt=0
                    ).exclude(pk=self.pk).aggregate(total=Sum('outstanding_amount'))['total'] or Decimal("0.00")
                    
                    # Include current voucher's total in the check
                    current_total = self.total_debit()
                    total_projected_outstanding = existing_outstanding + current_total
                    
                    if total_projected_outstanding > customer.credit_limit:
                        raise ValidationError(
                            f"Credit Limit Exceeded! Customer '{customer.name}' has existing outstanding of "
                            f"₹{existing_outstanding:.2f}. This invoice (₹{current_total:.2f}) brings total to "
                            f"₹{total_projected_outstanding:.2f}, exceeding the limit of ₹{customer.credit_limit:.2f}."
                        )
                
                # 2. Check Overdue Invoices
                if customer.credit_days is not None:
                    from datetime import date
                    overdue_limit = timezone.now().date() - timedelta(days=customer.credit_days)
                    
                    overdue_exists = Voucher.objects.filter(
                        company=self.company,
                        voucher_type='Sales',
                        date__lt=overdue_limit,
                        outstanding_amount__gt=0,
                        items__ledger=customer,
                        status='APPROVED'
                    ).exclude(pk=self.pk).exists()
                    
                    if overdue_exists:
                        raise ValidationError(
                            f"Credit Blocked! Customer '{customer.name}' has invoices overdue beyond "
                            f"{customer.credit_days} days. Please settle old bills before making new sales."
                        )

        if self.pk and self.items.exists():
            self.check_statutory_thresholds()
            self.validate_balance()

    def validate_balance(self):
        """
        Public double-entry enforcement method.
        """
        if not self.items.exists():
            raise ValidationError("A voucher must have at least one line item.")

        agg = self.items.aggregate(
            total_dr=models.Sum("amount", filter=models.Q(entry_type='DR')),
            total_cr=models.Sum("amount", filter=models.Q(entry_type='CR')),
        )
        total_dr = agg["total_dr"] or Decimal("0.00")
        total_cr = agg["total_cr"] or Decimal("0.00")

        if total_dr != total_cr:
            raise ValidationError(
                f"Voucher is not balanced: "
                f"Total Debit Rs.{total_dr:.2f} vs Total Credit Rs.{total_cr:.2f} "
                f"(Difference: Rs.{abs(total_dr - total_cr):.2f})"
            )

    def is_balanced(self):
        """Returns True if the voucher has lines and ΣDebit == ΣCredit."""
        if not self.items.exists():
            return False
        agg = self.items.aggregate(
            total_dr=models.Sum("amount", filter=models.Q(entry_type='DR')),
            total_cr=models.Sum("amount", filter=models.Q(entry_type='CR')),
        )
        return (
            (agg["total_dr"] or Decimal("0.00"))
            == (agg["total_cr"] or Decimal("0.00"))
        )

    def total_debit(self):
        """ΣDebit across all items. Uses DB aggregation."""
        return self.items.filter(entry_type='DR').aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    def total_credit(self):
        """ΣCredit across all items. Uses DB aggregation."""
        return self.items.filter(entry_type='CR').aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    def get_calculated_tax(self):
        """ΣTax across all items. Identifies tax ledgers by nature or name."""
        from reports.utils import GST_KEYWORDS
        tax_items = self.items.filter(
            models.Q(ledger__account_group__nature="Tax") |
            models.Q(ledger__name__regex=r"(?i)(" + "|".join(GST_KEYWORDS) + ")")
        )
        return tax_items.aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    def total_amount(self):
        """Total invoice amount (sum of all DR for Sales, sum of all CR for Purchase)."""
        if self.voucher_type == 'Sales':
            return self.total_debit()
        return self.total_credit()

    # ------------------------------------------------------------------
    # Bill-to-Bill helpers
    # ------------------------------------------------------------------
    def amount_settled(self, as_of_date=None, approved_only=True):
        """
        Total settled against this voucher via reference_voucher links.
        """
        settlement_entry_type = 'CR' if self.voucher_type == "Sales" else 'DR'
        qs = VoucherItem.objects.filter(
            reference_voucher=self,
            entry_type=settlement_entry_type
        )
        if as_of_date:
            qs = qs.filter(voucher__date__lte=as_of_date)
        if approved_only:
            qs = qs.filter(voucher__status='APPROVED')
        result = qs.aggregate(
            settled=models.Sum("amount")
        )["settled"] or Decimal("0.00")
        return result

    def calculate_outstanding(self, as_of_date=None, approved_only=True):
        """Invoice total minus what has been settled. Always >= 0."""
        # Settlements are items pointing back to this voucher as reference_voucher
        settled = self.amount_settled(
            as_of_date=as_of_date,
            approved_only=approved_only,
        )
        
        # This is a bit simplistic, but usually Invoice Dr settles with Payment Cr
        total = self.total_debit() if self.voucher_type == "Sales" else self.total_credit()
        outstanding = total - settled
        return max(outstanding, Decimal("0.00"))

    def sync_outstanding(self):
        """Update the cached outstanding_amount field with row-level locking."""
        with transaction.atomic():
            v = type(self).objects.select_for_update().get(pk=self.pk)
            v.outstanding_amount = v.calculate_outstanding()
            v.save(update_fields=['outstanding_amount'])

    def is_fully_settled(self):
        return self.calculate_outstanding() == Decimal("0.00")

    @staticmethod
    def _available_stock_quantity(stock_item, godown=None):
        """
        Return available stock scoped to a godown when one is selected.

        Opening quantity is item-level in the current model, so only unscoped
        stock checks include opening quantity. Godown-specific checks rely on
        recorded godown movements to avoid using another godown's stock.
        """
        if not godown:
            return stock_item.closing_quantity()

        from inventory.models import StockLedger

        moved = (
            StockLedger.objects
            .filter(stock_item=stock_item, godown=godown)
            .aggregate(total=models.Sum("quantity"))["total"]
            or Decimal("0.000")
        )
        return moved

    def sync_inventory(self):
        """
        Unified Inventory Synchronization Logic:
        Updates StockLedger entries, Batch quantities, and Valuation (StockValuationEntry) for:
        1. VoucherItem lines (Accounting-first flow)
        2. VoucherStockItem lines (Inventory-first flow)
        
        Handles Sales (Outward), Purchase (Inward), and Stock Transfer.
        """
        from inventory.models import StockLedger, Batch
        from inventory.valuation_utils import rebuild_valuation_for_items
        
        with transaction.atomic():
            previous_stock_item_ids = set(self.stock_movements.values_list('stock_item_id', flat=True))

            # 1. Revert previous batch quantities for idempotency
            for movement in self.stock_movements.all():
                if movement.batch:
                    movement.batch.quantity -= movement.quantity
                    movement.batch.save(update_fields=['quantity'])
            
            self.stock_movements.all().delete()
            self.valuation_entries.all().delete() # Clear valuation entries for re-sync

            if self.status != "APPROVED" or self.voucher_type not in ["Sales", "Purchase", "Sales Return", "Purchase Return", "Stock Transfer"]:
                rebuild_valuation_for_items(previous_stock_item_ids)
                return

            # 2. Collect stock lines from one source only. The detailed stock
            # section wins when present, preventing double stock movements.
            stock_lines = []
            detailed_stock_lines = list(self.voucher_stock_items.all())

            if detailed_stock_lines:
                for vsi in detailed_stock_lines:
                    stock_lines.append({
                        'stock_item': vsi.stock_item,
                        'godown': vsi.godown,
                        'batch': vsi.batch,
                        'quantity': vsi.quantity,
                        'rate': vsi.rate
                    })
            else:
                for item in self.items.filter(stock_item__isnull=False):
                    stock_lines.append({
                        'stock_item': item.stock_item,
                        'godown': item.godown,
                        'batch': item.batch,
                        'quantity': item.quantity,
                        'rate': item.rate
                    })

            # Landed Cost Allocation for Purchase
            landed_cost_map = {}
            if self.voucher_type == "Purchase" and hasattr(self, 'landed_cost'):
                lc = self.landed_cost
                total_extra = lc.total_extra_cost
                if total_extra > 0:
                    total_qty = sum(line['quantity'] for line in stock_lines)
                    line_count = len(stock_lines)
                    
                    if lc.allocation_method == 'EQUAL' and line_count > 0:
                        extra_per_line = (total_extra / Decimal(str(line_count))).quantize(Decimal("0.01"))
                        for i, line in enumerate(stock_lines):
                            landed_cost_map[i] = extra_per_line / line['quantity'] if line['quantity'] > 0 else 0
                    
                    elif lc.allocation_method == 'QUANTITY' and total_qty > 0:
                        for i, line in enumerate(stock_lines):
                            share = (total_extra * (line['quantity'] / total_qty)).quantize(Decimal("0.01"))
                            landed_cost_map[i] = share / line['quantity'] if line['quantity'] > 0 else 0

            # 3. Process each line
            affected_stock_items = set(previous_stock_item_ids)

            for i, line in enumerate(stock_lines):
                qty = line['quantity']
                if qty == 0: continue

                # Apply Landed Cost to effective rate
                effective_rate = line['rate'] + landed_cost_map.get(i, Decimal("0.00"))

                # Determine direction
                if self.voucher_type in ["Sales", "Purchase Return"]:
                    qty = -qty
                # Purchase and Sales Return keep original qty (positive/inward)

                stock_item = line['stock_item']
                affected_stock_items.add(stock_item.pk)
                godown = line['godown']
                batch_to_use = line['batch']
                
                # ... (rest of the batch selection logic remains same)
                if qty < 0 and not batch_to_use:
                    batch_to_use = Batch.objects.filter(
                        stock_item=stock_item,
                        godown=godown,
                        quantity__gt=0
                    ).order_by(models.F('expiry_date').asc(nulls_last=True), 'created_at').first()

                # Validation
                if qty < 0:
                    if batch_to_use and batch_to_use.is_expired:
                        raise ValidationError(f"Cannot sell expired batch {batch_to_use.batch_number} for {stock_item.name}.")

                    try:
                        company_prevents_negative = self.company.inventory_settings.prevent_negative_stock
                    except Exception:
                        company_prevents_negative = False

                    if stock_item.prevent_negative_stock or company_prevents_negative:
                        available_qty = (
                            batch_to_use.quantity
                            if batch_to_use
                            else self._available_stock_quantity(stock_item, godown)
                        )
                        if available_qty + qty < 0:
                            raise ValidationError(
                                f"Insufficient stock for {stock_item.name}. "
                                f"Required: {abs(qty)}, Available: {available_qty}"
                            )

                # Create Movement (Legacy Ledger)
                StockLedger.objects.create(
                    stock_item=stock_item,
                    voucher=self,
                    godown=godown,
                    batch=batch_to_use,
                    date=self.date,
                    quantity=qty,
                    rate=effective_rate
                )

                # Update Batch
                if batch_to_use:
                    batch_to_use.quantity += qty
                    batch_to_use.save(update_fields=['quantity'])

            rebuild_valuation_for_items(affected_stock_items)

    def create_tax_lines(self):
        """
        Automatically calculate and append GST lines (CGST/SGST or IGST).
        Updates summary fields on the voucher and adjusts the 'Party' ledger line.
        """
        if self.voucher_type not in ["Sales", "Purchase"]:
            return

        from decimal import Decimal
        from ledger.models import Ledger
        from core.gst_utils import calculate_gst

        # 1. Determine direction
        is_purchase = self.voucher_type == "Purchase"

        # --- BLOCKED ITC LOGIC ---
        is_blocked = False
        if is_purchase and self.narration:
            blocked_keywords = ['hotel', 'restaurant', 'food', 'car repair', 'motor vehicle']
            narration_lower = self.narration.lower()
            if any(k in narration_lower for k in blocked_keywords):
                is_blocked = True

        suffix = "Input" if (is_purchase and not is_blocked) else "Output"
        if is_blocked:
            # If blocked, we route to "Blocked GST Expense" or similar
            # For simplicity, we'll use the "Expense" nature of the group
            suffix = "Blocked Expense" 

        # 2. Identify State & GST Type
        company_state = self.company.gstin[:2] if (self.company.gstin and len(self.company.gstin) >= 2) else "27"
        voucher_state = self.place_of_supply or company_state
        
        # 3. Calculate Taxable Totals & Aggregate GST
        total_cgst = Decimal("0.00")
        total_sgst = Decimal("0.00")
        total_igst = Decimal("0.00")
        
        # We aggregate tax per item because different items might have different rates
        # though standard practice often has one rate per invoice.
        items_to_process = list(self.items.filter(stock_item__isnull=False)) + list(self.voucher_stock_items.all())
        
        for item in items_to_process:
            if item.stock_item.tax_rate:
                tax_data = calculate_gst(
                    company_state=company_state,
                    party_state=voucher_state,
                    taxable_amount=item.quantity * item.rate,
                    gst_rate=item.stock_item.tax_rate.rate
                )
                total_cgst += tax_data["cgst"]
                total_sgst += tax_data["sgst"]
                total_igst += tax_data["igst"]

        self.cgst_amount = total_cgst
        self.sgst_amount = total_sgst
        self.igst_amount = total_igst
        self.total_tax = total_cgst + total_sgst + total_igst
        self.save(update_fields=['cgst_amount', 'sgst_amount', 'igst_amount', 'total_tax'])

        if self.total_tax <= 0:
            return

        # 4. Remove only system-generated GST lines for idempotency.
        # Manual tax/TDS lines must remain untouched.
        self.items.filter(
            ledger__account_group__nature="Tax",
            narration__startswith="Auto-calculated",
        ).delete()

        # 5. Create Tax Lines in Ledger
        prefix = " Suspense" if is_purchase else ""
        
        if total_igst > 0:
            from ledger.models import AccountGroup
            ledger_igst, _ = Ledger.objects.get_or_create(
                company=self.company, name=f"IGST {suffix}{prefix}", 
                defaults={"account_group": AccountGroup.objects.get_or_create(company=self.company, name="Tax", nature="Tax")[0]}
            )
            VoucherItem.objects.create(
                voucher=self, ledger=ledger_igst,
                entry_type='DR' if is_purchase else 'CR',
                amount=total_igst,
                narration=f"Auto-calculated IGST{prefix}"
            )
        else:
            from ledger.models import AccountGroup
            ledger_cgst, _ = Ledger.objects.get_or_create(
                company=self.company, name=f"CGST {suffix}{prefix}",
                defaults={"account_group": AccountGroup.objects.get_or_create(company=self.company, name="Tax", nature="Tax")[0]}
            )
            ledger_sgst, _ = Ledger.objects.get_or_create(
                company=self.company, name=f"SGST {suffix}{prefix}",
                defaults={"account_group": AccountGroup.objects.get_or_create(company=self.company, name="Tax", nature="Tax")[0]}
            )
            VoucherItem.objects.create(
                voucher=self, ledger=ledger_cgst,
                entry_type='DR' if is_purchase else 'CR',
                amount=total_cgst,
                narration=f"Auto-calculated CGST{prefix}"
            )
            VoucherItem.objects.create(
                voucher=self, ledger=ledger_sgst,
                entry_type='DR' if is_purchase else 'CR',
                amount=total_sgst,
                narration=f"Auto-calculated SGST{prefix}"
            )

        # 6. Balance the Voucher by adjusting the Party line
        party_item = self.items.filter(ledger__account_group__nature__in=["Asset", "Liability"]).first()
        if not party_item:
            party_item = self.items.exclude(ledger__account_group__nature__in=["Income", "Expense", "Tax"]).first()
        
        if party_item:
            from django.db.models import Sum
            agg = self.items.aggregate(
                dr=Sum('amount', filter=models.Q(entry_type='DR')), 
                cr=Sum('amount', filter=models.Q(entry_type='CR'))
            )
            dr_val = agg['dr'] or Decimal("0.00")
            cr_val = agg['cr'] or Decimal("0.00")
            diff = cr_val - dr_val
            
            if diff != 0:
                if party_item.entry_type == 'DR':
                    party_item.amount += diff
                else:
                    party_item.amount -= diff
                
                if party_item.amount <= 0:
                    raise ValidationError(
                        "Auto GST could not safely adjust the party line. "
                        "Review the voucher party, taxable amount, and manual tax lines."
                    )
                
                party_item.save()


# ---------------------------------------------------------------------------
# VoucherItem
# ---------------------------------------------------------------------------
class VoucherItem(models.Model):
    voucher = models.ForeignKey(
        Voucher, on_delete=models.CASCADE, related_name="items"
    )
    ledger = models.ForeignKey(
        Ledger, on_delete=models.PROTECT, related_name="voucher_items"
    )
    # New fields (Task 2)
    entry_type = models.CharField(
        max_length=2,
        choices=[('DR', 'Dr'), ('CR', 'Cr')]
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2
    )
    narration = models.CharField(max_length=255, blank=True)

    # Foundational Inventory Integration (Step 2)
    stock_item = models.ForeignKey(
        "inventory.StockItem",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="voucher_items",
        help_text="Optional: link this accounting line to a stock item for inventory tracking.",
    )
    godown = models.ForeignKey(
        "inventory.Godown",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="voucher_items",
        help_text="The warehouse location for this stock movement.",
    )
    batch = models.ForeignKey(
        "inventory.Batch",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="voucher_items",
        help_text="The specific batch/lot for this stock movement.",
    )
    quantity = models.DecimalField(
        max_digits=15, decimal_places=3, default=Decimal("0.000"),
        help_text="Quantity for inventory movement.",
    )
    rate = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0.00"),
        help_text="Rate per unit for inventory movement.",
    )

    # Cost Center tagging — optional allocation to a department / project
    cost_center = models.ForeignKey(
        "costcenter.CostCenter",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="voucher_items",
        help_text="Tag this line to a specific cost center (Project/Department).",
    )

    # Bill-to-Bill: optionally link this line to the invoice it is settling.
    reference_voucher = models.ForeignKey(
        Voucher,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="settlements",
        help_text=(
            "The Sales/Purchase invoice this line is settling. "
            "Leave blank if not applicable."
        ),
    )

    class Meta:
        verbose_name = "Voucher Item"
        verbose_name_plural = "Voucher Items"

    def __str__(self):
        amt = self.amount or Decimal("0.00")
        try:
            ledger_name = self.ledger.name if self.ledger_id else "Unknown Ledger"
        except (Ledger.DoesNotExist, ObjectDoesNotExist, Exception):
            ledger_name = "Unknown Ledger"
        return f"{ledger_name} | {self.entry_type} {amt:.2f}"

    def _assert_parent_editable(self):
        if (
            self.voucher_id
            and self.voucher.status == 'APPROVED'
            and not getattr(self.voucher, '_allow_locked_child_edit', False)
        ):
            raise ValidationError(
                "Approved vouchers are hard locked. Unapprove the voucher before editing its lines."
            )

    def clean(self):
        """
        Per-line validation rules:
          1. Cannot save a zero line (no accounting meaning).
          2. Prevent negative cash (Asset balance must stay Debit/Zero).
          3. Prevent negative stock (if prevent_negative_stock is enabled).
        """
        # If no ledger is selected, this is likely a blank row in a formset.
        # We let it pass so Django's formset can ignore it if it's truly empty.
        try:
            if not self.ledger_id:
                return
            ledger = self.ledger
        except (ObjectDoesNotExist, Exception):
            return

        if self.amount is None or self.amount <= Decimal("0.00"):
            raise ValidationError(
                "A voucher line must carry a positive amount."
            )
        
        # Negative Cash Check (Identifies by ledger name for simplicity in this dev environment)
        if ledger and "Cash" in ledger.name and self.entry_type == 'CR':
            current_bal = ledger.current_balance()
            # If it's an update, we should exclude the existing instance's amount
            if self.pk:
                old_item = VoucherItem.objects.get(pk=self.pk)
                if old_item.entry_type == 'CR':
                    current_bal -= old_item.amount
                elif old_item.entry_type == 'DR':
                    current_bal += old_item.amount

            # If new balance > 0, it means Credit > Debit (Negative Cash for an Asset)
            if current_bal + self.amount > 0:
                raise ValidationError(
                    f"Insufficient Cash! Proposed transaction results in negative balance. "
                    f"Current balance: Rs.{abs(current_bal):.2f}"
                )

        # Negative Stock Check
        if self.stock_item:
            try:
                company_prevents_negative = self.voucher.company.inventory_settings.prevent_negative_stock
            except Exception:
                company_prevents_negative = False
        else:
            company_prevents_negative = False

        if self.stock_item and (self.stock_item.prevent_negative_stock or company_prevents_negative):
            # Only check for outward movements (Sales or Stock Transfer out)
            if self.voucher.voucher_type == "Sales" and self.entry_type == 'CR':
                closing_qty = self.voucher._available_stock_quantity(self.stock_item, self.godown)
                if self.pk:
                    old_item = VoucherItem.objects.get(pk=self.pk)
                    if old_item.stock_item == self.stock_item and old_item.entry_type == 'CR':
                        closing_qty += old_item.quantity
                
                if closing_qty - self.quantity < 0:
                    raise ValidationError(
                        f"Insufficient Stock for {self.stock_item.name}! "
                        f"Available: {closing_qty} {self.stock_item.unit}"
                    )

        # Budget Control Check (Expense/Purchase only)
        if self.cost_center and self.voucher.voucher_type in ["Purchase", "Payment", "Journal"]:
            # Only check if the ledger is an Expense or Assets (for CAPEX budget if needed)
            # but usually budget is for Expenses.
            if self.ledger.account_group.nature in ["Expense", "Asset"]:
                from costcenter.models import Budget
                from django.db.models import Sum
                
                v_date = self.voucher.date
                budget = Budget.objects.filter(
                    cost_center=self.cost_center,
                    year=v_date.year,
                    month=v_date.month
                ).first()
                
                if budget:
                    # Calculate total spent for this cost center in this month
                    spent_qs = VoucherItem.objects.filter(
                        cost_center=self.cost_center,
                        voucher__date__year=v_date.year,
                        voucher__date__month=v_date.month,
                        ledger__account_group__nature__in=["Expense", "Asset"],
                        entry_type='DR' # Spending is usually a Debit
                    )
                    if self.pk:
                        spent_qs = spent_qs.exclude(pk=self.pk)
                    
                    total_spent = spent_qs.aggregate(total=Sum('amount'))['total'] or Decimal("0.00")
                    
                    if total_spent + self.amount > budget.monthly_limit:
                        raise ValidationError(
                            f"Budget Exceeded for Cost Center '{self.cost_center.name}'! "
                            f"Monthly limit: ₹{budget.monthly_limit:,.2f}. "
                            f"Spent so far: ₹{total_spent:,.2f}. "
                            f"This line: ₹{self.amount:,.2f}."
                        )

    def save(self, *args, **kwargs):
        self._assert_parent_editable()
        self.clean()
        is_new = self.pk is None
        super().save(*args, **kwargs)
        
        if self.reference_voucher:
            self.reference_voucher.sync_outstanding()
            
            # Forex Gain/Loss Automation
            invoice_rate = self.reference_voucher.exchange_rate
            payment_rate = self.voucher.exchange_rate
            
            if invoice_rate != payment_rate:
                diff_rate = invoice_rate - payment_rate
                forex_diff = diff_rate * self.amount
                
                if forex_diff != 0:
                    with transaction.atomic():
                        # Create a Journal Voucher for Forex Adjustment
                        narration = f"Forex Adj for {self.reference_voucher.number} (Rate: {invoice_rate} -> {payment_rate})"
                        jv = Voucher.objects.create(
                            company=self.voucher.company,
                            voucher_type='Journal',
                            date=self.voucher.date,
                            narration=narration,
                        )
                        
                        from ledger.models import AccountGroup
                        
                        if forex_diff > 0:
                            # Forex Loss (Rate dropped, e.g. 80 -> 75)
                            loss_grp, _ = AccountGroup.objects.get_or_create(company=self.voucher.company, name='Indirect Expenses', nature='Expense')
                            loss_ledger, _ = Ledger.objects.get_or_create(company=self.voucher.company, name='Forex Loss', account_group=loss_grp)
                            
                            VoucherItem.objects.create(voucher=jv, ledger=loss_ledger, amount=abs(forex_diff), entry_type='DR')
                            VoucherItem.objects.create(voucher=jv, ledger=self.ledger, amount=abs(forex_diff), entry_type='CR')
                        else:
                            # Forex Gain (Rate rose, e.g. 80 -> 85)
                            gain_grp, _ = AccountGroup.objects.get_or_create(company=self.voucher.company, name='Indirect Income', nature='Income')
                            gain_ledger, _ = Ledger.objects.get_or_create(company=self.voucher.company, name='Forex Gain', account_group=gain_grp)
                            
                            VoucherItem.objects.create(voucher=jv, ledger=self.ledger, amount=abs(forex_diff), entry_type='DR')
                            VoucherItem.objects.create(voucher=jv, ledger=gain_ledger, amount=abs(forex_diff), entry_type='CR')
                        jv.approve(None)

    def delete(self, *args, **kwargs):
        self._assert_parent_editable()
        return super().delete(*args, **kwargs)


class VoucherClass(models.Model):
    TAX_BEHAVIOR_CHOICES = [
        ('AUTO_GST', 'Automatic GST'),
        ('NONE', 'Manual'),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="voucher_classes")
    name = models.CharField(max_length=100)
    voucher_type = models.CharField(max_length=20, choices=Voucher.VOUCHER_TYPE_CHOICES)
    default_ledgers = models.ManyToManyField(Ledger, blank=True, help_text="Ledgers to auto-add to voucher")
    tax_behavior = models.CharField(max_length=20, choices=TAX_BEHAVIOR_CHOICES, default='AUTO_GST')

    class Meta:
        verbose_name = "Voucher Class"
        verbose_name_plural = "Voucher Classes"
        unique_together = ("company", "name")

    def __str__(self):
        return f"{self.name} ({self.voucher_type})"
