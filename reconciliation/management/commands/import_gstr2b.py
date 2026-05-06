import json
import os
from decimal import Decimal
from datetime import datetime
from django.core.management.base import BaseCommand
from core.models import Company
from reconciliation.models import GSTR2BEntry

class Command(BaseCommand):
    help = 'Import GSTR-2B data from a JSON file'

    def add_arguments(self, parser):
        parser.add_argument('company_id', type=int)
        parser.add_argument('json_file', type=str)

    def handle(self, *args, **options):
        company_id = options['company_id']
        json_path = options['json_file']

        try:
            company = Company.objects.get(pk=company_id)
        except Company.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'Company with ID {company_id} not found.'))
            return

        if not os.path.exists(json_path):
            self.stdout.write(self.style.ERROR(f'File {json_path} not found.'))
            return

        with open(json_path, 'r') as f:
            data = json.load(f)

        count = 0
        # Expected format: {"invoices": [{"gstin": "...", "invoice_no": "...", "date": "...", "tax_amount": 100.00}]}
        for inv in data.get('invoices', []):
            try:
                GSTR2BEntry.objects.update_or_create(
                    company=company,
                    gstin=inv['gstin'],
                    invoice_number=inv['invoice_no'],
                    defaults={
                        'invoice_date': datetime.strptime(inv['date'], '%Y-%m-%d').date(),
                        'tax_amount': Decimal(str(inv['tax_amount'])),
                    }
                )
                count += 1
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Skipping row: {e}"))

        self.stdout.write(self.style.SUCCESS(f'Successfully imported {count} GSTR-2B entries for {company.name}'))
