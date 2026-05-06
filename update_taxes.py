
import os
import django
from decimal import Decimal

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'akshaya_vistara.settings')
django.setup()

from vouchers.models import Voucher

def run():
    vouchers = Voucher.objects.all()
    count = 0
    total = vouchers.count()
    print(f"Updating taxes for {total} vouchers...")
    for v in vouchers:
        try:
            v.total_tax = v.get_calculated_tax()
            v.save(update_fields=['total_tax'])
            count += 1
        except Exception as e:
            print(f"Error updating voucher {v.id}: {e}")
    print(f"Successfully updated {count}/{total} vouchers.")

if __name__ == "__main__":
    run()
