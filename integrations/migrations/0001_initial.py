import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("core", "0011_checklistitem"),
        ("vouchers", "0026_gst_integration_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="IntegrationRequestLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("request_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("provider", models.CharField(max_length=50)),
                ("service", models.CharField(choices=[("gstin_lookup", "GSTIN Lookup"), ("e_invoice", "E-Invoice"), ("e_way_bill", "E-Way Bill")], max_length=30)),
                ("status", models.CharField(choices=[("success", "Success"), ("failed", "Failed"), ("config_error", "Configuration Error")], max_length=20)),
                ("request_digest", models.CharField(blank=True, max_length=64)),
                ("response_code", models.CharField(blank=True, max_length=30)),
                ("response_payload", models.JSONField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="integration_logs", to="core.company")),
                ("requested_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="integration_requests", to=settings.AUTH_USER_MODEL)),
                ("voucher", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="integration_logs", to="vouchers.voucher")),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="integrationrequestlog",
            index=models.Index(fields=["company", "service", "status"], name="integration_company_0df26f_idx"),
        ),
        migrations.AddIndex(
            model_name="integrationrequestlog",
            index=models.Index(fields=["voucher", "service"], name="integration_voucher_b06ed7_idx"),
        ),
    ]
