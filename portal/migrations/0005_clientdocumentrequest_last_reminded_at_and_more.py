from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0004_clientdocumentrequest"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientdocumentrequest",
            name="last_reminded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clientdocumentrequest",
            name="reminder_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
