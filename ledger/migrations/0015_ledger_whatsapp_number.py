from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0014_ledger_credit_days_ledger_credit_limit"),
    ]

    operations = [
        migrations.AddField(
            model_name="ledger",
            name="whatsapp_number",
            field=models.CharField(
                blank=True,
                help_text="Vendor/client WhatsApp number for statutory follow-ups.",
                max_length=20,
                null=True,
            ),
        ),
    ]
