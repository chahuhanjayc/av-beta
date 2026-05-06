
import os
import django
import random
from decimal import Decimal
from datetime import date, timedelta

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'akshaya_vistara.settings')
django.setup()

from django.contrib.auth import get_user_model
from core.models import Company
from ledger.models import Ledger, AccountGroup
from vouchers.models import Voucher, VoucherItem
from inventory.models import StockItem, Godown, Batch
from costcenter.models import CostCenter
from fixedassets.models import AssetGroup, FixedAsset
from orders.models import Order, OrderItem
from tds.models import TDSSection, TDSEntry
from personal_finance.models import PersonalCategory, PersonalExpense, PersonalIncome

User = get_user_model()

def run():
    company = Company.objects.get(id=1)
    admin_user = User.objects.filter(is_superuser=True).first()
    if not admin_user:
        admin_user = User.objects.create_superuser('admin', 'admin@example.com', 'admin123')
        
    print(f"Filling dummy data for: {company.name}")

    # 1. Cost Centers
    if CostCenter.objects.filter(company=company).count() < 5:
        centers = ["Marketing", "Sales", "R&D", "Support", "Operations"]
        for c in centers:
            CostCenter.objects.get_or_create(company=company, name=c)
        print("Added Cost Centers")

    # 2. Stock Items & Inventory
    godown, _ = Godown.objects.get_or_create(company=company, name="Main Warehouse")
    if StockItem.objects.filter(company=company).count() < 15:
        items = ["Laptop Pro 15", "Office Chair", "Monitor 4K", "Wireless Mouse", "Keyboard RGB", "USB-C Hub", "Desk Lamp"]
        for name in items:
            si, _ = StockItem.objects.get_or_create(
                company=company, name=name, 
                defaults={'unit': 'Nos', 'purchase_price': 500, 'selling_price': 800}
            )
            Batch.objects.get_or_create(stock_item=si, batch_number=f"B-{random.randint(100,999)}", godown=godown)
        print("Added Stock Items")

    # 3. Fixed Assets
    asset_group, _ = AssetGroup.objects.get_or_create(company=company, name="Office Equipment")
    if FixedAsset.objects.filter(company=company).count() < 5:
        assets = [("MacBook Air", 95000), ("Herman Miller Chair", 45000), ("Dell Server", 250000)]
        for name, price in assets:
            FixedAsset.objects.get_or_create(
                company=company, asset_group=asset_group, name=name,
                defaults={
                    'purchase_date': date.today() - timedelta(days=200), 
                    'purchase_value': Decimal(price), 
                    'depreciation_method': 'SLM', 
                    'wdv_rate': Decimal('15.00')
                }
            )
        print("Added Fixed Assets")

    # 4. Orders
    party = Ledger.objects.filter(company=company, account_group__name__icontains='Sundry').first()
    if party and Order.objects.filter(company=company).count() < 10:
        for i in range(5):
            o = Order.objects.create(
                company=company, order_type="Purchase", number=f"PO-SH-{100+i}",
                party_ledger=party, order_date=date.today() - timedelta(days=i),
                status="Confirmed"
            )
            item = StockItem.objects.filter(company=company).first()
            if item:
                OrderItem.objects.create(order=o, stock_item=item, quantity=Decimal('10'), rate=item.purchase_price)
        print("Added Orders")

    # 5. TDS Sections
    if TDSSection.objects.filter(company=company).count() < 3:
        sections = [("194C", "Contractors", 1.0), ("194J", "Professional Fees", 10.0), ("194I", "Rent", 10.0)]
        for code, desc, rate in sections:
            TDSSection.objects.get_or_create(company=company, section_code=code, defaults={'description': desc, 'rate_company': Decimal(rate)})
        print("Added TDS Sections")

    # 6. Personal Finance
    if PersonalCategory.objects.filter(user=admin_user).count() < 5:
        cats = ["Food", "Transport", "Rent", "Entertainment", "Health"]
        for c in cats:
            pc, _ = PersonalCategory.objects.get_or_create(name=c, user=admin_user)
            PersonalExpense.objects.create(user=admin_user, category=pc, amount=Decimal(random.randint(100, 2000)), date=date.today(), description="Dummy expense")
        print("Added Personal Finance Data")

    print("Dummy data population complete!")

if __name__ == "__main__":
    run()
