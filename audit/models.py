from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError


class ImmutableAuditLogQuerySet(models.QuerySet):
    def delete(self):
        raise ValidationError("Immutable logs cannot be bulk deleted.")


class AuditLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=20)  # CREATE, UPDATE, DELETE
    model_name = models.CharField(max_length=100)
    object_id = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True)
    before_data = models.JSONField(null=True, blank=True)
    after_data = models.JSONField(null=True, blank=True)

    objects = ImmutableAuditLogQuerySet.as_manager()

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.action} - {self.model_name} ({self.object_id}) at {self.timestamp}"

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit logs cannot be deleted.")


class OverrideLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    reason = models.TextField()
    action = models.CharField(max_length=255)  # e.g., "Bypass Period Lock"
    timestamp = models.DateTimeField(auto_now_add=True)

    objects = ImmutableAuditLogQuerySet.as_manager()

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.user} - {self.action} at {self.timestamp}"

    def delete(self, *args, **kwargs):
        raise ValidationError("Override logs cannot be deleted.")


class LockOverrideRequest(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="lock_overrides")
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_overrides")
    approved_at = models.DateTimeField(null=True, blank=True)

    def approve(self, admin_user):
        self.status = 'APPROVED'
        self.approved_by = admin_user
        from django.utils import timezone
        self.approved_at = timezone.now()
        self.save()
        
        # Log in OverrideLog as per Step 4
        OverrideLog.objects.create(
            user=self.user,
            reason=self.reason,
            action=f"Lock Override Approved by {admin_user}"
        )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Request by {self.user} - {self.status}"
