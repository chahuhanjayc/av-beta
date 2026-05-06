from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0005_clientdocumentrequest_last_reminded_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="clientdocumentrequest",
            name="recipient_email",
            field=models.EmailField(
                blank=True,
                help_text="Email address used for document request reminders.",
                max_length=254,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="clientdocumentrequest",
            name="recipient_whatsapp_number",
            field=models.CharField(
                blank=True,
                help_text="Client WhatsApp number used for direct reminder links.",
                max_length=20,
                null=True,
            ),
        ),
    ]
