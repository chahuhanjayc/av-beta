from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vouchers", "0028_voucher_transport_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="voucher",
            name="e_invoice_signed_invoice",
            field=models.JSONField(blank=True, help_text="Signed e-invoice JSON returned by IRP/GSP.", null=True),
        ),
        migrations.AddField(
            model_name="voucher",
            name="e_invoice_signed_qr_code",
            field=models.TextField(blank=True, help_text="Signed QR code payload returned by IRP/GSP."),
        ),
        migrations.AddField(
            model_name="voucher",
            name="e_invoice_status",
            field=models.CharField(blank=True, help_text="Latest e-invoice status returned by the provider/IRP.", max_length=30),
        ),
        migrations.AddField(
            model_name="voucher",
            name="e_way_bill_status",
            field=models.CharField(blank=True, help_text="Latest e-way bill status returned by the provider.", max_length=30),
        ),
        migrations.AddField(
            model_name="voucher",
            name="e_way_bill_valid_until",
            field=models.DateTimeField(blank=True, help_text="E-way bill validity expiry returned by the provider.", null=True),
        ),
    ]
