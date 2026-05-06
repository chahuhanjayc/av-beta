from django.core.management.base import BaseCommand
from django.core.exceptions import ValidationError
from django.db import transaction
from decimal import Decimal
from datetime import date, timedelta
from core.models import AuditLog, Company, CompanySettings as CoreSettings
from ledger.models import AccountGroup, Ledger
from inventory.models import StockItem, Godown, StockValuationEntry, Batch, TaxRate, CompanySettings as InventorySettings
from vouchers.models import Voucher, VoucherItem

class Command(BaseCommand):
    help = 'Run a full system audit from a CA/QA perspective.'

    def handle(self, *args, **options):
        self.stdout.write("Running setup...")
        company = self.setup_test_environment()
        
        results = {
            "Accounting Integrity": "FAIL",
            "Audit Trail": "FAIL",
            "Cash Control": "FAIL",
            "Stock Control": "FAIL",
            "GST Compliance": "FAIL",
            "Inventory Valuation": "FAIL",
            "Period Locking": "FAIL",
            "Bill Matching": "FAIL",
            "Returns Handling": "FAIL",
            "Drill-Down": "FAIL",
        }

        try:
            results["Accounting Integrity"] = self.test_accounting_integrity(company)
            results["Audit Trail"] = self.test_audit_trail(company)
            results["Cash Control"] = self.test_cash_control(company)
            results["Stock Control"] = self.test_stock_control(company)
            results["Inventory Valuation"] = self.test_inventory_valuation(company)
            results["GST Compliance"] = self.test_gst_compliance(company)
            results["Period Locking"] = self.test_period_locking(company)
            results["Bill Matching"] = self.test_bill_matching(company)
            results["Returns Handling"] = self.test_returns_handling(company)
            results["Drill-Down"] = self.test_drill_down(company)
        except Exception as e:
            self.stdout.write(f"Unexpected error during testing: {e}")

        # Print final report
        self.stdout.write("\n=== SYSTEM AUDIT REPORT ===")
        for test, result in results.items():
            if test != "Drill-Down":  
                pass

        print(f"Accounting Integrity: {results['Accounting Integrity']}")
        print(f"Audit Trail: {results['Audit Trail']}")
        print(f"Cash Control: {results['Cash Control']}")
        print(f"Stock Control: {results['Stock Control']}")
        print(f"GST Compliance: {results['GST Compliance']}")
        print(f"Inventory Valuation: {results['Inventory Valuation']}")
        print(f"Period Locking: {results['Period Locking']}")
        print(f"Bill Matching: {results['Bill Matching']}")
        print(f"Returns Handling: {results['Returns Handling']}")

        all_passed = all(res == "PASS" for k, res in results.items() if k != "Drill-Down") and results.get("Drill-Down", "PASS") == "PASS"
        print("\nFINAL STATUS:")
        if all_passed:
            print("SYSTEM SAFE")
        else:
            print("SYSTEM RISKY")

    def setup_test_environment(self):
        import uuid
        with transaction.atomic():
            company_name = f"AUDIT_TEST_{uuid.uuid4().hex[:8]}"
            
            company = Company.objects.create(
                name=company_name,
                gstin="27AAAAA0000A1Z5",  # Maharashtra
                short_code="ATC"
            )
            CoreSettings.objects.get_or_create(company=company)
            InventorySettings.objects.create(company=company, valuation_method="FIFO")
            
            # Create Groups
            asset_grp = AccountGroup.objects.create(company=company, name="Asset Group", nature="Asset")
            liab_grp = AccountGroup.objects.create(company=company, name="Liability Group", nature="Liability")
            inc_grp = AccountGroup.objects.create(company=company, name="Income Group", nature="Income")
            exp_grp = AccountGroup.objects.create(company=company, name="Expense Group", nature="Expense")
            tax_grp = AccountGroup.objects.create(company=company, name="Tax Group", nature="Tax")

            # Create Ledgers
            Ledger.objects.create(company=company, name="Cash-in-Hand", account_group=asset_grp, opening_balance=Decimal("-10000.00"))
            Ledger.objects.create(company=company, name="Sales", account_group=inc_grp)
            Ledger.objects.create(company=company, name="Purchase", account_group=exp_grp)
            Ledger.objects.create(company=company, name="Client A (Intra)", account_group=asset_grp) # Same state
            Ledger.objects.create(company=company, name="Client B (Inter)", account_group=asset_grp) # Diff state
            
            # Tax Ledgers
            Ledger.objects.create(company=company, name="CGST Output", account_group=tax_grp)
            Ledger.objects.create(company=company, name="SGST Output", account_group=tax_grp)
            Ledger.objects.create(company=company, name="IGST Output", account_group=tax_grp)

            # Godown & Tax Rate
            Godown.objects.create(company=company, name="Main Godown", is_primary=True)
            tax_rate = TaxRate.objects.filter(rate=Decimal("18.00"), description="GST 18%").first()
            if not tax_rate:
                TaxRate.objects.create(rate=Decimal("18.00"), description="GST 18%")
            
            return company

    def test_accounting_integrity(self, company):
        cash = Ledger.objects.get(name="Cash-in-Hand", company=company)
        sales = Ledger.objects.get(name="Sales", company=company)
        
        # Test unbalanced
        v = Voucher(company=company, voucher_type="Journal", narration="Test Unbalanced")
        v.save()
        VoucherItem.objects.create(voucher=v, ledger=cash, entry_type='DR', amount=Decimal("100"))
        VoucherItem.objects.create(voucher=v, ledger=sales, entry_type='CR', amount=Decimal("90"))
        try:
            v.validate_balance()
            v.delete()
            return "FAIL" # Should have raised
        except ValidationError:
            pass # Expected
        v.delete()
        
        # Test balanced
        v2 = Voucher(company=company, voucher_type="Journal", narration="Test Balanced")
        v2.save()
        VoucherItem.objects.create(voucher=v2, ledger=cash, entry_type='DR', amount=Decimal("100"))
        VoucherItem.objects.create(voucher=v2, ledger=sales, entry_type='CR', amount=Decimal("100"))
        try:
            v2.validate_balance()
        except ValidationError:
            v2.delete()
            return "FAIL"
            
        v2.delete()
        return "PASS"

    def test_audit_trail(self, company):
        cash = Ledger.objects.get(name="Cash-in-Hand", company=company)
        sales = Ledger.objects.get(name="Sales", company=company)
        
        v = Voucher.objects.create(company=company, voucher_type="Journal", narration="Audit Test 1")
        v_id = v.pk
        
        if not AuditLog.objects.filter(
            action=AuditLog.ACTION_CREATE,
            model_name="voucher",
            record_id=v_id,
        ).exists():
            return "FAIL"
            
        v.narration = "Audit Test 2"
        v.save()
        if not AuditLog.objects.filter(
            action=AuditLog.ACTION_UPDATE,
            model_name="voucher",
            record_id=v_id,
        ).exists():
            return "FAIL"
            
        v.delete()
        if not AuditLog.objects.filter(
            action=AuditLog.ACTION_DELETE,
            model_name="voucher",
            record_id=v_id,
        ).exists():
            return "FAIL"
            
        return "PASS"

    def test_cash_control(self, company):
        cash = Ledger.objects.get(name="Cash-in-Hand", company=company)
        # Cash has 10000 opening. Try spending 20000.
        v = Voucher.objects.create(company=company, voucher_type="Payment", narration="Negative Cash")
        try:
            VoucherItem.objects.create(voucher=v, ledger=cash, entry_type='CR', amount=Decimal("20000"))
            v.delete()
            return "FAIL" # Should have failed
        except ValidationError:
            v.delete()
            return "PASS"

    def test_stock_control(self, company):
        laptop = StockItem.objects.create(company=company, name="Laptop", prevent_negative_stock=True)
        sales = Ledger.objects.get(name="Sales", company=company)
        client = Ledger.objects.get(name="Client A (Intra)", company=company)
        
        v = Voucher.objects.create(company=company, voucher_type="Sales", narration="Negative Stock")
        VoucherItem.objects.create(voucher=v, ledger=client, entry_type='DR', amount=Decimal("1000"))
        try:
            VoucherItem.objects.create(
                voucher=v, ledger=sales, entry_type='CR', amount=Decimal("1000"),
                stock_item=laptop, quantity=Decimal("5"), rate=Decimal("200")
            )
            v.sync_inventory()
            v.delete()
            return "FAIL"
        except ValidationError:
            v.delete()
            return "PASS"

    def test_inventory_valuation(self, company):
        item = StockItem.objects.create(company=company, name="Test Item", unit="Nos")
        godown = Godown.objects.get(company=company)
        supplier = Ledger.objects.get(name="Cash-in-Hand", company=company)
        purchase_ac = Ledger.objects.get(name="Purchase", company=company)
        client = Ledger.objects.get(name="Client A (Intra)", company=company)
        sales_ac = Ledger.objects.get(name="Sales", company=company)

        # Purchase 1: 10 @ 100
        vp1 = Voucher.objects.create(company=company, voucher_type="Purchase")
        VoucherItem.objects.create(voucher=vp1, ledger=supplier, entry_type='CR', amount=Decimal("1000"))
        VoucherItem.objects.create(voucher=vp1, ledger=purchase_ac, entry_type='DR', amount=Decimal("1000"),
                                   stock_item=item, quantity=10, rate=100, godown=godown)
        vp1.approve(None)

        # Purchase 2: 10 @ 200
        vp2 = Voucher.objects.create(company=company, voucher_type="Purchase")
        VoucherItem.objects.create(voucher=vp2, ledger=supplier, entry_type='CR', amount=Decimal("2000"))
        VoucherItem.objects.create(voucher=vp2, ledger=purchase_ac, entry_type='DR', amount=Decimal("2000"),
                                   stock_item=item, quantity=10, rate=200, godown=godown)
        vp2.approve(None)

        item.refresh_from_db()
        avg_cost = item.purchase_price
        if avg_cost != Decimal("150.00"):
            return f"FAIL (AVG is {avg_cost})"

        # Sell 5
        vs1 = Voucher.objects.create(company=company, voucher_type="Sales")
        VoucherItem.objects.create(voucher=vs1, ledger=client, entry_type='DR', amount=Decimal("1500"))
        VoucherItem.objects.create(voucher=vs1, ledger=sales_ac, entry_type='CR', amount=Decimal("1500"),
                                   stock_item=item, quantity=5, rate=300, godown=godown)
        vs1.approve(None)

        # Check FIFO (first lot should have 5 remaining)
        lots = StockValuationEntry.objects.filter(item=item).order_by('id')
        if lots[0].remaining_quantity != 5 or lots[1].remaining_quantity != 10:
            return "FAIL (FIFO incorrect)"

        vp1.delete()
        vp2.delete()
        vs1.delete()
        return "PASS"

    def test_gst_compliance(self, company):
        tax_rate = TaxRate.objects.filter(description="GST 18%").first()
        item = StockItem.objects.create(company=company, name="GST Item", tax_rate=tax_rate)
        sales_ac = Ledger.objects.get(name="Sales", company=company)
        client_intra = Ledger.objects.get(name="Client A (Intra)", company=company)
        client_inter = Ledger.objects.get(name="Client B (Inter)", company=company)
        
        # Intra
        v_intra = Voucher.objects.create(company=company, voucher_type="Sales", place_of_supply="27") # MH to MH
        VoucherItem.objects.create(voucher=v_intra, ledger=client_intra, entry_type='DR', amount=Decimal("1180"))
        VoucherItem.objects.create(voucher=v_intra, ledger=sales_ac, entry_type='CR', amount=Decimal("1000"), stock_item=item, quantity=1, rate=1000)
        v_intra.create_tax_lines()
        
        if v_intra.cgst_amount != 90 or v_intra.sgst_amount != 90 or v_intra.igst_amount != 0:
            return "FAIL (Intra)"
            
        # Inter
        v_inter = Voucher.objects.create(company=company, voucher_type="Sales", place_of_supply="24") # MH to GJ
        VoucherItem.objects.create(voucher=v_inter, ledger=client_inter, entry_type='DR', amount=Decimal("1180"))
        VoucherItem.objects.create(voucher=v_inter, ledger=sales_ac, entry_type='CR', amount=Decimal("1000"), stock_item=item, quantity=1, rate=1000)
        v_inter.create_tax_lines()
        
        if v_inter.igst_amount != 180 or v_inter.cgst_amount != 0:
            return "FAIL (Inter)"
            
        v_intra.delete()
        v_inter.delete()
        return "PASS"

    def test_period_locking(self, company):
        settings = company.settings
        settings.books_closed_until = date.today()
        settings.save()
        
        v = Voucher(company=company, voucher_type="Journal", date=date.today())
        try:
            v.clean()
            return "FAIL" # Should block
        except ValidationError:
            pass
            
        settings.books_closed_until = None
        settings.save()
        return "PASS"

    def test_bill_matching(self, company):
        client = Ledger.objects.get(name="Client A (Intra)", company=company)
        sales = Ledger.objects.get(name="Sales", company=company)
        cash = Ledger.objects.get(name="Cash-in-Hand", company=company)
        
        inv1 = Voucher.objects.create(company=company, voucher_type="Sales")
        VoucherItem.objects.create(voucher=inv1, ledger=client, entry_type='DR', amount=Decimal("1000"))
        VoucherItem.objects.create(voucher=inv1, ledger=sales, entry_type='CR', amount=Decimal("1000"))
        inv1.approve(None)
        inv1.sync_outstanding()
        
        pmt = Voucher.objects.create(company=company, voucher_type="Receipt")
        VoucherItem.objects.create(voucher=pmt, ledger=cash, entry_type='DR', amount=Decimal("400"))
        VoucherItem.objects.create(voucher=pmt, ledger=client, entry_type='CR', amount=Decimal("400"), reference_voucher=inv1)
        pmt.approve(None)
        
        inv1.refresh_from_db()
        if inv1.outstanding_amount != Decimal("600.00"):
            return "FAIL"
            
        inv1.delete()
        pmt.delete()
        return "PASS"

    def test_returns_handling(self, company):
        item = StockItem.objects.create(company=company, name="Return Item")
        godown = Godown.objects.get(company=company)
        client = Ledger.objects.get(name="Client A (Intra)", company=company)
        sales_ac = Ledger.objects.get(name="Sales", company=company)
        supplier = Ledger.objects.get(name="Cash-in-Hand", company=company)
        purchase_ac = Ledger.objects.get(name="Purchase", company=company)
        
        # Initial Stock (Purchase 10)
        v_purch = Voucher.objects.create(company=company, voucher_type="Purchase")
        VoucherItem.objects.create(voucher=v_purch, ledger=supplier, entry_type='CR', amount=Decimal("1000"))
        VoucherItem.objects.create(voucher=v_purch, ledger=purchase_ac, entry_type='DR', amount=Decimal("1000"), stock_item=item, quantity=10, rate=100, godown=godown)
        v_purch.approve(None)
        
        batch = Batch.objects.filter(stock_item=item).first()
        
        # Sale 5
        v_sale = Voucher.objects.create(company=company, voucher_type="Sales")
        VoucherItem.objects.create(voucher=v_sale, ledger=client, entry_type='DR', amount=Decimal("1000"))
        VoucherItem.objects.create(voucher=v_sale, ledger=sales_ac, entry_type='CR', amount=Decimal("1000"), stock_item=item, quantity=5, rate=200, godown=godown, batch=batch)
        v_sale.approve(None)
        
        # Return 2
        v_ret = Voucher.objects.create(company=company, voucher_type="Sales Return")
        VoucherItem.objects.create(voucher=v_ret, ledger=client, entry_type='CR', amount=Decimal("400"))
        VoucherItem.objects.create(voucher=v_ret, ledger=sales_ac, entry_type='DR', amount=Decimal("400"), stock_item=item, quantity=2, rate=200, godown=godown, batch=batch)
        v_ret.approve(None)
        
        if item.closing_quantity() != 7:
            print(f"DEBUG: Returns Handling failed. closing_quantity={item.closing_quantity()}")
            return "FAIL"
            
        v_purch.delete()
        v_sale.delete()
        v_ret.delete()
        return "PASS"

    def test_drill_down(self, company):
        cash = Ledger.objects.get(name="Cash-in-Hand", company=company)
        asset_group = cash.account_group
        
        v = Voucher.objects.create(company=company, voucher_type="Receipt")
        VoucherItem.objects.create(voucher=v, ledger=cash, entry_type='DR', amount=Decimal("500"))
        sales = Ledger.objects.get(name="Sales", company=company)
        VoucherItem.objects.create(voucher=v, ledger=sales, entry_type='CR', amount=Decimal("500"))
        v.approve(None)
        
        bal = cash.current_balance()
        if bal != Decimal("-10500.00"): # -10000 opening (dr) + 500 DR
            return "FAIL"
            
        v.delete()
        return "PASS"
