from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import models, transaction
from .models import StockValuationEntry, CompanySettings

def get_valuation_method(company):
    """Retrieve company valuation method, default to AVG."""
    settings, _ = CompanySettings.objects.get_or_create(company=company)
    return settings.valuation_method

def handle_stock_inward(stock_item, quantity, rate, date, voucher=None):
    """
    Handle Purchase / Inward movement for valuation.
    Creates a new valuation lot with row-level locking.
    """
    with transaction.atomic():
        # Lock the StockItem to prevent concurrent valuation updates
        locked_item = type(stock_item).objects.select_for_update().get(pk=stock_item.pk)
        
        StockValuationEntry.objects.create(
            item=locked_item,
            quantity=quantity,
            rate=rate,
            date=date,
            voucher=voucher,
            remaining_quantity=quantity
        )
        update_running_average(locked_item)

def handle_stock_outward(stock_item, quantity, date, voucher=None):
    """
    Handle Sales / Outward movement for valuation with row-level locking.
    """
    with transaction.atomic():
        # Lock the StockItem
        locked_item = type(stock_item).objects.select_for_update().get(pk=stock_item.pk)
        method = get_valuation_method(locked_item.company)
        
        if method == 'FIFO':
            consume_fifo(locked_item, quantity)
        else:
            consume_average(locked_item, quantity)
        update_running_average(locked_item)

def handle_sales_return(stock_item, quantity, rate, date, voucher=None):
    """
    Handle Sales Return with row-level locking.
    """
    with transaction.atomic():
        locked_item = type(stock_item).objects.select_for_update().get(pk=stock_item.pk)
        StockValuationEntry.objects.create(
            item=locked_item,
            quantity=quantity,
            rate=rate,
            date=date,
            voucher=voucher,
            remaining_quantity=quantity
        )
        update_running_average(locked_item)

def handle_purchase_return(stock_item, quantity, date, voucher=None):
    """
    Handle Purchase Return with row-level locking.
    """
    with transaction.atomic():
        locked_item = type(stock_item).objects.select_for_update().get(pk=stock_item.pk)
        consume_fifo(locked_item, quantity)
        update_running_average(locked_item)

def update_running_average(stock_item):
    """
    Recalculate weighted average price with row-level locking.
    """
    with transaction.atomic():
        # Ensure we work with a locked item if not already locked by caller
        locked_item = type(stock_item).objects.select_for_update().get(pk=stock_item.pk)
        
        active_lots = StockValuationEntry.objects.filter(
            item=locked_item, remaining_quantity__gt=0
        )
        total_qty = active_lots.aggregate(total=models.Sum('remaining_quantity'))['total'] or Decimal('0')
        total_value = sum(lot.remaining_quantity * lot.rate for lot in active_lots)

        if total_qty > 0:
            avg_rate = (total_value / total_qty).quantize(Decimal('0.01'))
            locked_item.purchase_price = avg_rate
            locked_item.save(update_fields=['purchase_price'])

def consume_fifo(stock_item, qty_to_consume):
    """
    FIFO: Consume the oldest available valuation lots.
    Uses select_for_update() to handle high-concurrency environments.
    """
    with transaction.atomic():
        available_lots = StockValuationEntry.objects.select_for_update().filter(
            item=stock_item, remaining_quantity__gt=0
        ).order_by('date', 'id')

        remaining = Decimal(str(qty_to_consume))
        for lot in available_lots:
            if remaining <= 0:
                break
            can_take = min(lot.remaining_quantity, remaining)
            lot.remaining_quantity -= can_take
            lot.save(update_fields=['remaining_quantity'])
            remaining -= can_take

        if remaining > 0:
            raise ValidationError(
                f"Insufficient valuation stock for {stock_item.name}. "
                f"Short by {remaining}."
            )


def consume_average(stock_item, qty_to_consume):
    """
    Weighted-average consumption: reduce all active valuation lots
    proportionally so the remaining average cost is stable.
    """
    with transaction.atomic():
        lots = list(
            StockValuationEntry.objects.select_for_update().filter(
                item=stock_item, remaining_quantity__gt=0
            ).order_by('date', 'id')
        )

        qty = Decimal(str(qty_to_consume))
        total_qty = sum((lot.remaining_quantity for lot in lots), Decimal("0.000"))

        if qty <= 0:
            return
        if total_qty < qty:
            raise ValidationError(
                f"Insufficient valuation stock for {stock_item.name}. "
                f"Required: {qty}, Available: {total_qty}."
            )

        remaining = qty
        quantum = Decimal("0.001")

        for idx, lot in enumerate(lots):
            if remaining <= 0:
                break
            if idx == len(lots) - 1:
                can_take = min(lot.remaining_quantity, remaining)
            else:
                proportional = (qty * lot.remaining_quantity / total_qty).quantize(quantum)
                can_take = min(lot.remaining_quantity, proportional, remaining)

            lot.remaining_quantity -= can_take
            lot.save(update_fields=['remaining_quantity'])
            remaining -= can_take

        if remaining > 0:
            # Rounding protection: consume any tiny residue from the last non-empty lot.
            lot = next((entry for entry in reversed(lots) if entry.remaining_quantity > 0), None)
            if lot and lot.remaining_quantity >= remaining:
                lot.remaining_quantity -= remaining
                lot.save(update_fields=['remaining_quantity'])
                remaining = Decimal("0.000")

        if remaining > 0:
            raise ValidationError(
                f"Unable to consume valuation stock for {stock_item.name}. "
                f"Short by {remaining}."
            )


def rebuild_valuation_for_items(stock_items):
    """
    Rebuild valuation lots from StockLedger movements for the given items.
    This keeps edits/deletes idempotent because sales consumption is derived
    from the movement log instead of incrementally mutating stale lots.
    """
    from .models import StockItem, StockLedger

    item_ids = {item.pk if hasattr(item, "pk") else item for item in stock_items if item}
    if not item_ids:
        return

    with transaction.atomic():
        locked_items = StockItem.objects.select_for_update().filter(pk__in=item_ids)
        for stock_item in locked_items:
            StockValuationEntry.objects.filter(item=stock_item).delete()
            movements = (
                StockLedger.objects.filter(stock_item=stock_item)
                .select_related("voucher")
                .order_by("date", "created_at", "id")
            )
            method = get_valuation_method(stock_item.company)

            for movement in movements:
                if movement.quantity > 0:
                    StockValuationEntry.objects.create(
                        item=stock_item,
                        quantity=movement.quantity,
                        rate=movement.rate,
                        date=movement.date,
                        voucher=movement.voucher,
                        remaining_quantity=movement.quantity,
                    )
                elif movement.quantity < 0:
                    qty = abs(movement.quantity)
                    if method == "FIFO":
                        consume_fifo(stock_item, qty)
                    else:
                        consume_average(stock_item, qty)

            update_running_average(stock_item)
