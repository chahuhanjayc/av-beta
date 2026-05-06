from django.db import migrations
from decimal import Decimal

def migrate_dr_cr(apps, schema_editor):
    VoucherItem = apps.get_model('vouchers', 'VoucherItem')
    
    for item in VoucherItem.objects.all():
        if item.debit > Decimal("0.00"):
            item.entry_type = 'DR'
            item.amount = item.debit
        elif item.credit > Decimal("0.00"):
            item.entry_type = 'CR'
            item.amount = item.credit
        else:
            # For zero lines (invalid but might exist in some DB states)
            item.entry_type = 'DR'
            item.amount = Decimal("0.00")
        
        VoucherItem.objects.filter(pk=item.pk).update(entry_type=item.entry_type, amount=item.amount)

class Migration(migrations.Migration):

    dependencies = [
        ('vouchers', '0008_voucheritem_amount_voucheritem_entry_type'),
    ]

    operations = [
        migrations.RunPython(migrate_dr_cr),
    ]
