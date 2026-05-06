"""
ocr/models.py

OCRSubmission: stores an uploaded bill/invoice image or PDF,
the raw extracted text, parsed JSON fields, and links to
the confirmed Purchase Voucher once approved.

STATUS FLOW (with async Celery OCR):
  Pending     → file uploaded, Celery task dispatched
  Processing  → Celery worker has picked up the task
  Pending     → task finished successfully (ready for human review)
  Confirmed   → user approved and Purchase Voucher was created
  Rejected    → user rejected the submission
  Error       → OCR failed (user can still fill fields manually)

task_id stores the Celery task UUID so the verify page can poll status.
"""

import os
from django.db import models
from django.urls import reverse
from core.models import Company
from vouchers.models import Voucher


def ocr_upload_path(instance, filename):
    """Upload to media/ocr/<company_id>/<filename>"""
    return os.path.join("ocr", str(instance.company_id), filename)


class OCRSubmission(models.Model):
    STATUS_PENDING    = "Pending"
    STATUS_PROCESSING = "Processing"
    STATUS_CONFIRMED  = "Confirmed"
    STATUS_REJECTED   = "Rejected"
    STATUS_ERROR      = "Error"

    STATUS_CHOICES = [
        (STATUS_PENDING,    "Pending Review"),
        (STATUS_PROCESSING, "Processing (OCR running…)"),
        (STATUS_CONFIRMED,  "Confirmed"),
        (STATUS_REJECTED,   "Rejected"),
        (STATUS_ERROR,      "Error"),
    ]

    SOURCE_WEB      = "web"
    SOURCE_WHATSAPP = "whatsapp"
    SOURCE_CHOICES = [
        (SOURCE_WEB,      "Web Upload"),
        (SOURCE_WHATSAPP, "WhatsApp"),
    ]

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="ocr_submissions",
    )
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_WEB,
        help_text="Origin of the document upload.",
    )
    file = models.FileField(
        upload_to=ocr_upload_path,
        help_text="Upload a bill/invoice image (JPG, PNG) or PDF.",
    )
    file_hash = models.CharField(
        max_length=64,
        blank=True,
        help_text="SHA-256 hash of the uploaded file to prevent duplicates.",
    )
    extracted_text = models.TextField(
        blank=True,
        help_text="Raw text extracted from the document.",
    )
    parsed_json = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Parsed fields: vendor_name, gstin, date, "
            "total_amount, vendor_ledger_id, duplicate_warning."
        ),
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    linked_voucher = models.OneToOneField(
        Voucher,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ocr_source",
        help_text="The Purchase Voucher created on Confirm.",
    )
    ocr_error = models.TextField(
        blank=True,
        help_text="Error message if OCR failed (file still saved).",
    )

    # ── Async OCR tracking ────────────────────────────────────────────────────
    task_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Celery task UUID for tracking async OCR progress.",
    )

    # ── Inventory line items ─────────────────────────────────────────────────
    extracted_items = models.JSONField(
        null=True, blank=True,
        help_text="Raw line items extracted from OCR (list of {name, qty, rate, amount, hsn, tax_rate}).",
    )
    matched_items = models.JSONField(
        null=True, blank=True,
        help_text=(
            "Matched/confirmed line items after user review "
            "(list of {stock_item_id, name, qty, rate, amount, hsn, tax_rate})."
        ),
    )

    # ── Duplicate tracking ───────────────────────────────────────────────────
    duplicate_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="duplicates",
        help_text="Points to the earlier submission this one appears to duplicate.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "OCR Submission"
        verbose_name_plural = "OCR Submissions"
        ordering = ["-created_at"]
        unique_together = ("company", "file_hash")

    def __str__(self):
        return f"OCR #{self.pk} [{self.status}] — {self.company.name}"

    def get_absolute_url(self):
        return reverse("ocr:verify", kwargs={"pk": self.pk})

    def filename(self):
        return os.path.basename(self.file.name)

    def is_pdf(self):
        return self.file.name.lower().endswith(".pdf")

    def is_image(self):
        return self.file.name.lower().endswith((".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp"))

    def is_ready_for_review(self):
        """True if OCR has completed and the submission is awaiting human review."""
        return self.status == self.STATUS_PENDING and bool(self.parsed_json)

    def is_processing(self):
        return self.status == self.STATUS_PROCESSING
