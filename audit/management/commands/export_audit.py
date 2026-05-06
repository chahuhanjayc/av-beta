import os
from datetime import datetime
from django.core.management.base import BaseCommand
from audit.export_utils import generate_audit_csv

class Command(BaseCommand):
    help = 'Export all Audit and Override logs to a CSV file.'

    def handle(self, *args, **options):
        """
        Step 4: Management Command implementation.
        Generates CSV content and saves to a file in the project root.
        """
        self.stdout.write("Generating Audit CSV Export...")
        
        csv_content = generate_audit_csv()
        
        # File name with timestamp
        filename = f"audit_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        with open(filename, 'w') as f:
            f.write(csv_content)
            
        self.stdout.write(self.style.SUCCESS(f"Successfully exported audit logs to '{filename}'"))
