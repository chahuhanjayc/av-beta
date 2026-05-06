from django.core.management.base import BaseCommand
from core.models import Company
from reconciliation.utils import import_bank_from_csv, match_bank_entries
import os

class Command(BaseCommand):
    help = 'Import bank transactions from CSV and match with vouchers'

    def add_arguments(self, parser):
        parser.add_argument('company_id', type=int, help='Company ID')
        parser.add_argument('csv_path', type=str, help='Path to bank CSV file')

    def handle(self, *args, **options):
        company_id = options['company_id']
        csv_path = options['csv_path']

        try:
            company = Company.objects.get(pk=company_id)
        except Company.DoesNotExist:
            self.stderr.write(self.style.ERROR(f'Company with ID {company_id} does not exist'))
            return

        if not os.path.exists(csv_path):
            self.stderr.write(self.style.ERROR(f'File not found: {csv_path}'))
            return

        self.stdout.write(f'Importing bank entries for {company.name}...')
        count = import_bank_from_csv(company, csv_path)
        self.stdout.write(self.style.SUCCESS(f'Successfully imported {count} entries'))

        self.stdout.write('Matching with vouchers...')
        matches = match_bank_entries(company)
        self.stdout.write(self.style.SUCCESS(f'Successfully matched {matches} entries'))
