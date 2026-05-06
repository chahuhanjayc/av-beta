from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0002_balanceconfirmation"),
    ]

    operations = [
        migrations.AddField(
            model_name="balanceconfirmation",
            name="remarks",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="balanceconfirmation",
            name="response_status",
            field=models.CharField(choices=[("confirmed", "Confirmed"), ("disputed", "Disputed")], default="confirmed", max_length=20),
        ),
    ]
