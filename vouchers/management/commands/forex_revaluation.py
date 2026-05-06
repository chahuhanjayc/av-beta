from django.core.management.base import BaseCommand
from django.utils import timezone
from decimal import Decimal
from django.db import transaction
from vouchers.models import Voucher, VoucherItem
from ledger.models import Ledger, AccountGroup

class Command(BaseCommand):
    help = 'Performs Unrealized Forex Revaluation for open foreign invoices'

    def add_arguments(self, parser):
        parser.add_argument('--rate', type=float, help='Closing exchange rate', required=True)
        parser.add_argument('--company_id', type=int, help='Company ID', required=True)

    def handle(self, *args, **options):
        closing_rate = Decimal(str(options['rate']))
        company_id = options['company_id']
        
        # Find open foreign invoices (Sales or Purchase with outstanding > 0 and rate != 1)
        open_invoices = Voucher.objects.filter(
            company_id=company_id,
            outstanding_amount__gt=0,
            exchange_rate__gt=0
        ).exclude(exchange_rate=1.0)

        for inv in open_invoices:
            invoice_rate = inv.exchange_rate
            diff_rate = closing_rate - invoice_rate
            
            # Unrealized gain/loss on outstanding amount
            reval_amount = diff_rate * inv.outstanding_amount
            
            if reval_amount == 0:
                continue

            with transaction.atomic():
                jv = Voucher.objects.create(
                    company=inv.company,
                    voucher_type='Journal',
                    date=timezone.now().date(),
                    narration=f"Unrealized Forex Reval for {inv.number} (Inv Rate: {invoice_rate}, Closing: {closing_rate})",
                )

                # Identify the main party ledger (Debtor/Creditor) from the invoice
                # For Sales, party is usually DR. For Purchase, party is usually CR.
                if inv.voucher_type == 'Sales':
                    party_item = inv.items.filter(entry_type='DR').first()
                    is_debtor = True
                else:
                    party_item = inv.items.filter(entry_type='CR').first()
                    is_debtor = False

                if not party_item:
                    self.stdout.write(self.style.WARNING(f"No party item found for {inv.number}, skipping."))
                    continue

                party_ledger = party_item.ledger

                if reval_amount > 0:
                    # Rate increased
                    if is_debtor:
                        # Asset value increased -> Gain
                        # DR Debtor, CR Forex Gain
                        gain_grp, _ = AccountGroup.objects.get_or_create(company=inv.company, name='Indirect Income', nature='Income')
                        gain_ledger, _ = Ledger.objects.get_or_create(company=inv.company, name='Forex Gain', account_group=gain_grp)
                        
                        VoucherItem.objects.create(voucher=jv, ledger=party_ledger, amount=abs(reval_amount), entry_type='DR')
                        VoucherItem.objects.create(voucher=jv, ledger=gain_ledger, amount=abs(reval_amount), entry_type='CR')
                    else:
                        # Liability increased -> Loss
                        # DR Forex Loss, CR Creditor
                        loss_grp, _ = AccountGroup.objects.get_or_create(company=inv.company, name='Indirect Expenses', nature='Expense')
                        loss_ledger, _ = Ledger.objects.get_or_create(company=inv.company, name='Forex Loss', account_group=loss_grp)
                        
                        VoucherItem.objects.create(voucher=jv, ledger=loss_ledger, amount=abs(reval_amount), entry_type='DR')
                        VoucherItem.objects.create(voucher=jv, ledger=party_ledger, amount=abs(reval_amount), entry_type='CR')
                else:
                    # Rate decreased
                    if is_debtor:
                        # Asset value decreased -> Loss
                        # DR Forex Loss, CR Debtor
                        loss_grp, _ = AccountGroup.objects.get_or_create(company=inv.company, name='Indirect Expenses', nature='Expense')
                        loss_ledger, _ = Ledger.objects.get_or_create(company=inv.company, name='Forex Loss', account_group=loss_grp)
                        
                        VoucherItem.objects.create(voucher=jv, ledger=loss_ledger, amount=abs(reval_amount), entry_type='DR')
                        VoucherItem.objects.create(voucher=jv, ledger=party_ledger, amount=abs(reval_amount), entry_type='CR')
                    else:
                        # Liability decreased -> Gain
                        # DR Creditor, CR Forex Gain
                        gain_grp, _ = AccountGroup.objects.get_or_create(company=inv.company, name='Indirect Income', nature='Income')
                        gain_ledger, _ = Ledger.objects.get_or_create(company=inv.company, name='Forex Gain', account_group=gain_grp)
                        
                        VoucherItem.objects.create(voucher=jv, ledger=party_ledger, amount=abs(reval_amount), entry_type='DR')
                        VoucherItem.objects.create(voucher=jv, ledger=gain_ledger, amount=abs(reval_amount), entry_type='CR')

                jv.approve(None)
                self.stdout.write(self.style.SUCCESS(f"Created revaluation JV for {inv.number}: {jv.number}"))
