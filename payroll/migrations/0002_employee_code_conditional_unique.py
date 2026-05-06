from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0001_initial"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="employee",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="employee",
            constraint=models.UniqueConstraint(
                fields=("company", "employee_code"),
                condition=~models.Q(employee_code=""),
                name="uniq_employee_code_per_company_when_set",
            ),
        ),
    ]
