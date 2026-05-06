from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0002_employee_code_conditional_unique"),
        ("vouchers", "0025_voucher_number_unique_constraint"),
    ]

    operations = [
        migrations.AddField(
            model_name="payrollrun",
            name="posted_voucher",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="payroll_runs",
                to="vouchers.voucher",
            ),
        ),
    ]
