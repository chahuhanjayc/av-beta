from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0008_alter_taxrate_rate"),
    ]

    operations = [
        migrations.AddField(
            model_name="companysettings",
            name="prevent_negative_stock",
            field=models.BooleanField(
                default=False,
                help_text="Block sales/returns that would take company stock below zero.",
            ),
        ),
    ]
