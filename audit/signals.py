from django.db import models
from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver
from django.forms.models import model_to_dict
from vouchers.models import Voucher
from .models import AuditLog
import json
from decimal import Decimal

# Custom JSON encoder to handle Decimals and Dates
class DjangoJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        from django.db.models.fields.files import FieldFile
        if isinstance(obj, FieldFile):
            return obj.name if obj else None
        return super().default(obj)

def get_model_dict(instance):
    return model_to_dict(instance)

@receiver(pre_save, sender=Voucher)
def voucher_pre_save(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_instance = sender.objects.get(pk=instance.pk)
            instance._before_data = get_model_dict(old_instance)
        except sender.DoesNotExist:
            instance._before_data = None
    else:
        instance._before_data = None

@receiver(post_save, sender=Voucher)
def voucher_post_save(sender, instance, created, **kwargs):
    action = 'CREATE' if created else 'UPDATE'
    before_data = getattr(instance, '_before_data', None)
    after_data = get_model_dict(instance)
    
    # Simple check to avoid logging if nothing changed (optional)
    if not created and before_data == after_data:
        return

    AuditLog.objects.create(
        action=action,
        model_name='Voucher',
        object_id=str(instance.pk),
        before_data=json.loads(json.dumps(before_data, cls=DjangoJSONEncoder)) if before_data else None,
        after_data=json.loads(json.dumps(after_data, cls=DjangoJSONEncoder)),
        # Note: In a real app, user would be retrieved from request thread local or middleware
    )

@receiver(post_delete, sender=Voucher)
def voucher_post_delete(sender, instance, **kwargs):
    before_data = get_model_dict(instance)
    AuditLog.objects.create(
        action='DELETE',
        model_name='Voucher',
        object_id=str(instance.pk),
        before_data=json.loads(json.dumps(before_data, cls=DjangoJSONEncoder)),
        after_data=None,
    )
