from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_company_e_invoice_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="payment_reminder_email_body",
            field=models.TextField(
                blank=True,
                default=(
                    "Dear {client_name},\n\n"
                    "This is a payment reminder for invoice {voucher_number} from {company_name}.\n"
                    "Outstanding amount: {outstanding}\n"
                    "Due date: {due_date}\n"
                    "{aging_line}\n\n"
                    "Please ignore this message if payment has already been made.\n\n"
                    "Regards,\n{company_name}"
                ),
                help_text=(
                    "Available placeholders: {voucher_number}, {company_name}, {client_name}, "
                    "{amount}, {outstanding}, {due_date}, {aging_line}."
                ),
                verbose_name="Payment Reminder Email Body",
            ),
        ),
        migrations.AddField(
            model_name="company",
            name="payment_reminder_email_subject",
            field=models.CharField(
                blank=True,
                default="Payment reminder: Invoice {voucher_number} from {company_name}",
                help_text=(
                    "Available placeholders: {voucher_number}, {company_name}, {client_name}, "
                    "{amount}, {outstanding}, {due_date}, {aging_line}."
                ),
                max_length=180,
                verbose_name="Payment Reminder Email Subject",
            ),
        ),
    ]
