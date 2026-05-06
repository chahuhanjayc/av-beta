from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vouchers", "0027_voucher_source_tracking"),
    ]

    operations = [
        migrations.AddField(
            model_name="voucher",
            name="dispatch_pincode",
            field=models.PositiveIntegerField(blank=True, help_text="Dispatch pincode for e-invoice/e-way bill payloads.", null=True),
        ),
        migrations.AddField(
            model_name="voucher",
            name="ship_to_pincode",
            field=models.PositiveIntegerField(blank=True, help_text="Ship-to pincode for e-invoice/e-way bill payloads.", null=True),
        ),
        migrations.AddField(
            model_name="voucher",
            name="transport_distance_km",
            field=models.PositiveIntegerField(blank=True, help_text="Approximate transport distance in kilometres for e-way bill.", null=True),
        ),
        migrations.AddField(
            model_name="voucher",
            name="transport_doc_date",
            field=models.DateField(blank=True, help_text="Transport document date.", null=True),
        ),
        migrations.AddField(
            model_name="voucher",
            name="transport_doc_no",
            field=models.CharField(blank=True, help_text="Transport document or LR/RR/AWB number.", max_length=30),
        ),
        migrations.AddField(
            model_name="voucher",
            name="transport_mode",
            field=models.CharField(blank=True, choices=[("1", "Road"), ("2", "Rail"), ("3", "Air"), ("4", "Ship")], help_text="E-way bill transport mode.", max_length=1),
        ),
        migrations.AddField(
            model_name="voucher",
            name="transporter_id",
            field=models.CharField(blank=True, help_text="Transporter GSTIN or enrolment ID for e-way bill.", max_length=15),
        ),
        migrations.AddField(
            model_name="voucher",
            name="transporter_name",
            field=models.CharField(blank=True, help_text="Transporter name for e-way bill.", max_length=120),
        ),
        migrations.AddField(
            model_name="voucher",
            name="vehicle_number",
            field=models.CharField(blank=True, help_text="Vehicle number for road e-way bill movement.", max_length=20),
        ),
        migrations.AddField(
            model_name="voucher",
            name="vehicle_type",
            field=models.CharField(blank=True, choices=[("R", "Regular"), ("O", "Over Dimensional Cargo")], help_text="Vehicle type for e-way bill.", max_length=1),
        ),
    ]
