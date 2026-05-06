from django.apps import AppConfig

class AuditConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'audit'

    def ready(self):
        # Voucher/accounting audit logging is centralized in core.signals and
        # core.models.AuditLog. The audit app still owns lock override models.
        pass
