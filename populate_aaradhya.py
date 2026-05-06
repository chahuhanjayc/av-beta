
import os
import django
from decimal import Decimal
from datetime import date

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'akshaya_vistara.settings')
django.setup()

from core.models import Company, UserCompanyAccess
from ledger.models import Ledger, AccountGroup
from inventory.models import StockItem, Godown, Batch
from costcenter.models import CostCenter
from django.contrib.auth import get_user_model

User = get_user_model()

def populate_aaradhya():
    company = Company.objects.get(id=2)
    admin_user = User.objects.filter(is_superuser=True).first()
    
    # 1. Access
    UserCompanyAccess.objects.get_or_create(user=admin_user, company=company, defaults={"role": "Admin"})

    # 2. Account Groups & Ledgers
    groups = [
        ("Cash-in-hand", "Asset"),
        ("Bank Accounts", "Asset"),
        ("Sales Accounts", "Income"),
        ("Purchase Accounts", "Expense"),
        ("Duties & Taxes", "Tax"),
        ("Sundry Debtors", "Asset"),
        ("Sundry Creditors", "Liability"),
    ]
    for gname, nature in groups:
        grp, _ = AccountGroup.objects.get_or_create(company=company, name=gname, nature=nature)
        
    # Standard Ledgers
    Ledger.objects.get_or_create(company=company, name="Cash", account_group=AccountGroup.objects.get(company=company, name="Cash-in-hand"))
    Ledger.objects.get_or_create(company=company, name="SBI Bank", account_group=AccountGroup.objects.get(company=company, name="Bank Accounts"))
    Ledger.objects.get_or_create(company=company, name="Sales", account_group=AccountGroup.objects.get(company=company, name="Sales Accounts"))
    Ledger.objects.get_or_create(company=company, name="Purchases", account_group=AccountGroup.objects.get(company=company, name="Purchase Accounts"))
    Ledger.objects.get_or_create(company=company, name="GST 18%", account_group=AccountGroup.objects.get(company=company, name="Duties & Taxes"))
    Ledger.objects.get_or_create(company=company, name="Customer A", account_group=AccountGroup.objects.get(company=company, name="Sundry Debtors"))
    Ledger.objects.get_or_create(company=company, name="Vendor X", account_group=AccountGroup.objects.get(company=company, name="Sundry Creditors"))

    # 3. Cost Centers
    CostCenter.objects.get_or_create(company=company, name="Mumbai Branch")
    CostCenter.objects.get_or_create(company=company, name="Delhi Branch")

    # 4. Inventory
    godown, _ = Godown.objects.get_or_create(company=company, name="Warehouse 1")
    si, _ = StockItem.objects.get_or_create(
        company=company, name="Industrial Pump",
        defaults={'unit': 'Nos', 'purchase_price': Decimal('5000'), 'selling_price': Decimal('7500'), 'opening_quantity': Decimal('10')}
    )
    Batch.objects.get_or_create(stock_item=si, batch_number="LOT-001", godown=godown)

    print("Populated Aaradhya & Companies successfully.")

if __name__ == "__main__":
    populate_aaradhya()
