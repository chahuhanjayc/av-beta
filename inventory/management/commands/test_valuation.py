from decimal import Decimal
from datetime import date
from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import Company
from inventory.models import StockItem, CompanySettings, StockValuationEntry
from inventory.valuation_utils import handle_stock_inward, handle_stock_outward, get_valuation_method

class Command(BaseCommand):
    help = 'Test inventory valuation logic (FIFO + Weighted Average)'

    def handle(self, *args, **options):
        # 1. Setup test data
        self.stdout.write("Setting up test data...")
        
        # Get or create a company for testing
        company, _ = Company.objects.get_or_create(
            short_code="TEST_CO",
            defaults={"name": "Test Company"}
        )
        
        # Create a special stock item for testing
        test_item_name = "Test Item - Valuation"
        StockItem.objects.filter(company=company, name=test_item_name).delete()
        test_item = StockItem.objects.create(
            company=company,
            name=test_item_name,
            unit="Nos",
            valuation_method="WAC",
            opening_quantity=Decimal("0.000"),
            purchase_price=Decimal("0.00"),
            selling_price=Decimal("100.00")
        )
        
        # Set CompanySettings
        settings, _ = CompanySettings.objects.get_or_create(company=company)
        
        # ----------------------------------------------------------------------
        # === FIFO TEST ===
        # ----------------------------------------------------------------------
        self.stdout.write("\n=== FIFO TEST ===")
        with transaction.atomic():
            settings.valuation_method = 'FIFO'
            settings.save()
            
            # Purchase 1: Qty = 10, Rate = 100
            handle_stock_inward(test_item, Decimal("10"), Decimal("100"), date.today())
            
            # Purchase 2: Qty = 10, Rate = 200
            handle_stock_inward(test_item, Decimal("10"), Decimal("200"), date.today())
            
            # Sell: Qty = 5
            handle_stock_outward(test_item, Decimal("5"), date.today())
            
            # Validation
            lots = StockValuationEntry.objects.filter(item=test_item).order_by('date', 'id')
            lot1 = lots[0]
            lot2 = lots[1]
            
            self.stdout.write(f"Lot 1 (Rate 100) remaining: {lot1.remaining_quantity}")
            self.stdout.write(f"Lot 2 (Rate 200) remaining: {lot2.remaining_quantity}")
            
            if lot1.remaining_quantity == Decimal("5") and lot2.remaining_quantity == Decimal("10"):
                self.stdout.write(self.style.SUCCESS("PASS"))
            else:
                self.stdout.write(self.style.ERROR("FAIL"))
            
            # Revert FIFO entries for AVG Test
            lots.delete()
            test_item.purchase_price = Decimal("0.00")
            test_item.save()

        # ----------------------------------------------------------------------
        # === AVG TEST ===
        # ----------------------------------------------------------------------
        self.stdout.write("\n=== AVG TEST ===")
        with transaction.atomic():
            settings.valuation_method = 'AVG'
            settings.save()
            
            # Purchase 1: Qty = 10, Rate = 100
            handle_stock_inward(test_item, Decimal("10"), Decimal("100"), date.today())
            
            # Purchase 2: Qty = 10, Rate = 200
            handle_stock_inward(test_item, Decimal("10"), Decimal("200"), date.today())
            
            # Sell: Qty = 5 (AVG logic does not consume specific lots)
            handle_stock_outward(test_item, Decimal("5"), date.today())
            
            # Re-fetch item to see updated purchase_price (running average)
            test_item.refresh_from_db()
            expected_avg = Decimal("150.00")
            
            self.stdout.write(f"Average rate: {test_item.purchase_price}")
            
            if test_item.purchase_price == expected_avg:
                self.stdout.write(self.style.SUCCESS("PASS"))
            else:
                self.stdout.write(self.style.ERROR("FAIL"))

        # Cleanup
        # test_item.delete() # Commented out to allow inspection
        self.stdout.write("\nTests complete. Use 'python manage.py test_valuation' to rerun.")
