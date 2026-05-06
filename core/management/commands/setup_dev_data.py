from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from core.models import Company, UserCompanyAccess
from ledger.models import Ledger
from inventory.models import StockItem
from decimal import Decimal

User = get_user_model()

class Command(BaseCommand):
    help = "Auto-create default company, ledgers, and stock item for development."

    def handle(self, *args, **options):
        # 1. Ensure a superuser exists
        admin_user = User.objects.filter(is_superuser=True).first()
        if not admin_user:
            self.stdout.write(self.style.WARNING("No superuser found. Please create one with 'python manage.py createsuperuser'."))
            return

        # 2. Create Default Company
        company, created = Company.objects.get_or_create(
            name="Dev Test Company",
            defaults={
                "gstin": "24AAAAA0000A1Z5",
                "short_code": "DEV",
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created company: {company.name}"))

        # 3. Grant Access
        UserCompanyAccess.objects.get_or_create(
            user=admin_user,
            company=company,
            defaults={"role": "Admin"}
        )

        # 4. Create Basic Ledgers
        from ledger.models import AccountGroup
        ledgers = [
            ("Cash in Hand", "Asset"),
            ("HDFC Bank", "Asset"),
            ("Sales A/c", "Income"),
            ("Purchase A/c", "Expense"),
            ("CGST", "Tax"),
            ("SGST", "Tax"),
            ("IGST", "Tax"),
        ]
        for name, nature in ledgers:
            group, _ = AccountGroup.objects.get_or_create(
                company=company, name=nature, nature=nature
            )
            l, created = Ledger.objects.get_or_create(
                company=company,
                name=name,
                defaults={"account_group": group}
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created ledger: {name} ({nature})"))

        # 5. Create one Stock Item
        stock_item, created = StockItem.objects.get_or_create(
            company=company,
            name="Basmati Rice 5kg",
            defaults={
                "unit": "Nos",
                "opening_quantity": Decimal("100"),
                "purchase_price": Decimal("450.00"),
                "selling_price": Decimal("550.00"),
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created stock item: {stock_item.name}"))

        self.stdout.write(self.style.SUCCESS("Dev setup complete! Run 'python manage.py runserver' and login."))
