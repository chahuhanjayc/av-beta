
from django import template
from datetime import date

register = template.Library()

@register.filter
def subtract_dates(d1, d2):
    """
    Returns (d1 - d2).days.
    Used for calculating overdue days.
    """
    if not d1 or not d2:
        return 0
    
    # Ensure they are date objects, not datetimes
    if hasattr(d1, 'date'): d1 = d1.date()
    if hasattr(d2, 'date'): d2 = d2.date()
    
    return (d1 - d2).days

@register.filter
def absolute_value(value):
    """Returns absolute value of a number."""
    try:
        return abs(value)
    except:
        return value

@register.filter
def multiply(value, arg):
    """Multiplies value by arg."""
    try:
        return float(value) * float(arg)
    except:
        return 0
