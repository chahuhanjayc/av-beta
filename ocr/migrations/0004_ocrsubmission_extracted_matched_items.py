"""
ocr/migrations/0004 — add extracted_items and matched_items JSONFields to OCRSubmission
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ocr", "0003_ocr_duplicate_of_fk"),
    ]

    operations = [
        migrations.AddField(
            model_name="ocrsubmission",
            name="extracted_items",
            field=models.JSONField(
                blank=True,
                null=True,
                help_text=(
                    "Raw line items extracted from OCR "
                    "(list of {name, qty, rate, amount, hsn, tax_rate})."
                ),
            ),
        ),
        migrations.AddField(
            model_name="ocrsubmission",
            name="matched_items",
            field=models.JSONField(
                blank=True,
                null=True,
                help_text=(
                    "Matched/confirmed line items after user review "
                    "(list of {stock_item_id, name, qty, rate, amount, hsn, tax_rate})."
                ),
            ),
        ),
    ]
