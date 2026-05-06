import threading
from django.forms.models import model_to_dict
from django.core.serializers.json import DjangoJSONEncoder
import json
from decimal import Decimal
from datetime import date, datetime

_thread_locals = threading.local()

def set_current_user(user):
    _thread_locals.user = user

def get_current_user():
    return getattr(_thread_locals, 'user', None)

def set_current_company(company):
    _thread_locals.company = company

def get_current_company():
    return getattr(_thread_locals, 'company', None)

def get_current_audit_user():
    from django.contrib.auth import get_user_model

    user = get_current_user()
    if not user or not getattr(user, "is_authenticated", False) or not user.pk:
        return None

    user_model = get_user_model()
    return user_model._default_manager.filter(pk=user.pk).first()

class AuditSerializer(DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, (Decimal, date, datetime)):
            return str(o)
        return super().default(o)

def audit_log(action, instance, old_data=None, new_data=None):
    from core.models import AuditLog
    user = get_current_audit_user()
    company = get_current_company() or getattr(instance, 'company', None)
    
    if not company and hasattr(instance, 'voucher'):
        company = getattr(instance.voucher, 'company', None)
    if not company and hasattr(instance, 'payroll_run'):
        company = getattr(instance.payroll_run, 'company', None)
    if not company and hasattr(instance, 'asset'):
        company = getattr(instance.asset, 'company', None)
    if not company and hasattr(instance, 'statement'):
        company = getattr(instance.statement, 'company', None)
    if not company and hasattr(instance, 'stock_item'):
        company = getattr(instance.stock_item, 'company', None)

    if not company:
        return

    # Avoid logging the logs
    if isinstance(instance, AuditLog):
        return

    AuditLog.objects.create(
        company=company,
        user=user,
        action=action,
        model_name=instance._meta.model_name,
        record_id=instance.pk,
        old_data=old_data,
        new_data=new_data,
        object_repr=str(instance)[:200]
    )

def get_model_data(instance):
    from django.db.models.fields.files import FieldFile
    opts = instance._meta
    data = {}
    for f in opts.concrete_fields:
        if not f.editable or f.name in ['id', 'created_at', 'updated_at']:
            continue
        value = f.value_from_object(instance)
        if isinstance(value, (Decimal, date, datetime)):
            value = str(value)
        elif isinstance(value, FieldFile):
            value = value.name if value else None
        data[f.name] = value
    return data
