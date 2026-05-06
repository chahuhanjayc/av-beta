import re
from decimal import Decimal
from django.db.models import Q, Count
from .models import VoucherItem
from ledger.models import Ledger

def parse_input(input_str):
    """
    Splits 'electricity 2500' into ('electricity', 2500.00)
    """
    if not input_str:
        return None, None
    
    # Extract numeric part (last number found)
    numbers = re.findall(r'\d+(?:\.\d+)?', input_str)
    amount = Decimal(numbers[-1]) if numbers else None
    
    # Extract text part (remove the amount)
    text_part = input_str
    if numbers:
        text_part = input_str.replace(numbers[-1], '').strip()
    
    return text_part, amount

def get_suggestions(company, query_str):
    """
    Priority:
    1. Exact keyword match
    2. Partial match (icontains)
    3. Most frequently used ledger (fallback)
    """
    keyword, amount = parse_input(query_str)
    
    # 1. Exact & Partial Matches
    ledgers = Ledger.objects.filter(company=company, is_active=True)
    
    suggestions = []
    
    if keyword:
        # Exact match
        exact = ledgers.filter(name__iexact=keyword).first()
        if exact:
            suggestions.append(format_suggestion(exact, amount, company))
        
        # Partial match
        partials = ledgers.filter(name__icontains=keyword).exclude(name__iexact=keyword)[:3]
        for p in partials:
            suggestions.append(format_suggestion(p, amount, company))

    # 3. Fallback: Most frequent (if no matches or to fill up)
    if len(suggestions) < 3:
        frequent_ids = (
            VoucherItem.objects.filter(voucher__company=company)
            .values('ledger_id')
            .annotate(count=Count('id'))
            .order_by('-count')[:5]
        )
        frequent_ledgers = Ledger.objects.filter(id__in=[f['ledger_id'] for f in frequent_ids])
        for fl in frequent_ledgers:
            if not any(s['id'] == fl.id for s in suggestions):
                suggestions.append(format_suggestion(fl, amount, company))
                if len(suggestions) >= 5:
                    break

    return suggestions[:5]

def format_suggestion(ledger, amount, company):
    """Format suggestion with last used amount if amount not provided."""
    final_amount = amount
    if not final_amount:
        last_item = (
            VoucherItem.objects.filter(voucher__company=company, ledger=ledger)
            .order_by('-voucher__date', '-id')
            .first()
        )
        if last_item:
            final_amount = last_item.amount
            
    return {
        'id': ledger.id,
        'name': ledger.name,
        'amount': float(final_amount) if final_amount else 0.0,
        'group': ledger.account_group.name,
        'nature': ledger.account_group.nature,
    }
