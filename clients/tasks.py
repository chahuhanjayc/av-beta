import logging
import os
from celery import shared_task
from django.utils import timezone
from django.db.models import Q
from .models import ClientSubscription

logger = logging.getLogger(__name__)

# WhatsApp Integration Placeholders
WHATSAPP_API_URL = os.environ.get("WHATSAPP_API_URL", "https://api.whatsapp.com/send")
WHATSAPP_API_TOKEN = os.environ.get("WHATSAPP_API_TOKEN", "dummy_token")

def send_whatsapp_message(to_number, message):
    """Placeholder for real WhatsApp API integration."""
    logger.info(f"WHATSAPP to {to_number}: {message}")
    print(f"DEBUG WHATSAPP to {to_number}: {message}")

def send_welcome_message(user, company, temp_password):
    message = (
        f"Welcome to Akshaya Vistara, {user.first_name}! "
        f"Your account for {company.name} has been created. "
        f"Login: {user.email} / Password: {temp_password}. "
        f"Please change your password after logging in."
    )
    send_whatsapp_message("+910000000000", message) # In reality, get from user profile

def send_payment_reminder(user, days_remaining):
    message = f"Reminder: Your subscription expires in {days_remaining} days. Please renew to avoid interruption."
    send_whatsapp_message("+910000000000", message)

def send_expiry_notice(user):
    message = "Your subscription has expired. Access to your company data has been restricted. Please renew."
    send_whatsapp_message("+910000000000", message)

@shared_task(name="clients.tasks.check_expiring_subscriptions")
def check_expiring_subscriptions():
    """Daily task to check for subscriptions ending in 7, 3, or 1 days."""
    now = timezone.now()
    check_days = [7, 3, 1]
    
    for days in check_days:
        target_date = (now + timezone.timedelta(days=days)).date()
        subs = ClientSubscription.objects.filter(
            subscription_end__date=target_date,
            status__in=[ClientSubscription.STATUS_ACTIVE, ClientSubscription.STATUS_TRIAL]
        )
        for sub in subs:
            send_payment_reminder(sub.primary_user, days)
            
    # Also check for newly expired
    expired_subs = ClientSubscription.objects.filter(
        subscription_end__lt=now,
        status__in=[ClientSubscription.STATUS_ACTIVE, ClientSubscription.STATUS_TRIAL]
    )
    for sub in expired_subs:
        sub.status = ClientSubscription.STATUS_EXPIRED
        sub.save(update_fields=["status"])
        send_expiry_notice(sub.primary_user)
