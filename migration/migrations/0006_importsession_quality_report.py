from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("migration", "0005_importsession_ledger_mapping"),
    ]

    operations = [
        migrations.AddField(
            model_name="importsession",
            name="duplicate_voucher_count",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="importsession",
            name="unbalanced_voucher_count",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="importsession",
            name="validation_report",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
