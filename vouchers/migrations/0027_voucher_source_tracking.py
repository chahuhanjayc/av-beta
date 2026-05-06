from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vouchers", "0026_gst_integration_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="voucher",
            name="source_reference",
            field=models.CharField(blank=True, help_text="External voucher/reference number from the source system.", max_length=120),
        ),
        migrations.AddField(
            model_name="voucher",
            name="source_system",
            field=models.CharField(blank=True, help_text="External source system, e.g. tally.", max_length=30),
        ),
        migrations.AddIndex(
            model_name="voucher",
            index=models.Index(fields=["company", "source_system", "source_reference"], name="voucher_source_ref_idx"),
        ),
    ]
