from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vouchers", "0025_voucher_number_unique_constraint"),
    ]

    operations = [
        migrations.AddField(
            model_name="voucher",
            name="e_invoice_ack_date",
            field=models.DateTimeField(blank=True, help_text="E-invoice acknowledgement date/time.", null=True),
        ),
        migrations.AddField(
            model_name="voucher",
            name="e_invoice_ack_no",
            field=models.CharField(blank=True, help_text="E-invoice acknowledgement number.", max_length=50),
        ),
        migrations.AddField(
            model_name="voucher",
            name="e_invoice_irn",
            field=models.CharField(blank=True, help_text="IRN returned by the e-invoice provider.", max_length=100),
        ),
        migrations.AddField(
            model_name="voucher",
            name="e_way_bill_date",
            field=models.DateTimeField(blank=True, help_text="E-way bill generation date/time.", null=True),
        ),
        migrations.AddField(
            model_name="voucher",
            name="e_way_bill_no",
            field=models.CharField(blank=True, help_text="E-way bill number returned by the provider.", max_length=30),
        ),
    ]
