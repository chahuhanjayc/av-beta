from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver
from .utils.audit import audit_log, get_model_data
import threading

# Use thread local to track nested saves if needed, but here we mainly need it for UPDATE tracking
_audit_state = threading.local()

# List of models to audit
AUDITED_MODELS = [
    'voucher',
    'voucheritem',
    'voucherstockitem',
    'ledger',
    'company',
    'stockitem',
    'godown',
    'batch',
    'order',
    'orderitem',
    'employee',
    'payrollrun',
    'payslip',
    'fixedasset',
    'assetdepreciation',
    'tdsentry',
    'costcenter',
    'bankstatement',
    'bankstatementrow',
]


def _audit_key(sender, instance):
    return f"old_{sender._meta.label_lower}_{instance.pk}".replace(".", "_")

def is_audited(sender):
    return sender._meta.model_name in AUDITED_MODELS

@receiver(pre_save)
def audit_pre_save(sender, instance, **kwargs):
    if not is_audited(sender):
        return
    
    if instance.pk:
        try:
            old_instance = sender.objects.get(pk=instance.pk)
            setattr(_audit_state, _audit_key(sender, instance), get_model_data(old_instance))
        except sender.DoesNotExist:
            pass

@receiver(post_save)
def audit_post_save(sender, instance, created, **kwargs):
    if not is_audited(sender):
        return
    
    new_data = get_model_data(instance)
    
    if created:
        audit_log('create', instance, old_data={}, new_data=new_data)
    else:
        key = _audit_key(sender, instance)
        old_data = getattr(_audit_state, key, {})
        # Only log if there are changes
        if old_data != new_data:
            audit_log('update', instance, old_data=old_data, new_data=new_data)
        
        # Cleanup
        if hasattr(_audit_state, key):
            delattr(_audit_state, key)

@receiver(post_delete)
def audit_post_delete(sender, instance, **kwargs):
    if not is_audited(sender):
        return
    
    audit_log('delete', instance, old_data=get_model_data(instance), new_data={})
