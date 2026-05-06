from django.shortcuts import redirect
from django.urls import reverse
from django.contrib import messages
from django.utils import timezone
from django.core.exceptions import ObjectDoesNotExist
from .models import ClientSubscription

from django.conf import settings

class SubscriptionMiddleware:
    """
    Ensures the user's current company has an active subscription.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated:
            return self.get_response(request)

        # Bypass for development
        if settings.DEBUG:
            request.subscription = getattr(getattr(request, "current_company", None), "subscription", None)
            return self.get_response(request)

        # Exempt paths
        exempt_paths = [
            reverse("accounts:logout"),
            reverse("core:select_company"),
            "/admin/",
            "/clients/payment/",  # Placeholder for payment portal
            "/api/",
            "/personal-finance/",
        ]
        
        # Check if current path starts with any exempt path
        if any(request.path.startswith(path) for path in exempt_paths):
            return self.get_response(request)

        company = getattr(request, "current_company", None)
        if not company:
            return self.get_response(request)

        try:
            # Using hasattr or a safer access to avoid RelatedObjectDoesNotExist during assignment
            if not hasattr(company, 'subscription'):
                raise ObjectDoesNotExist("Company has no subscription.")
                
            subscription = company.subscription
            request.subscription = subscription
            
            # Check for expiry or suspension
            if not subscription.is_active():
                if request.path != reverse("clients:payment_portal"):
                    messages.warning(request, "Your subscription has expired or is suspended. Please renew to continue.")
                    return redirect("clients:payment_portal")
            
            # Reset monthly voucher count if month has changed
            now = timezone.now()
            if subscription.last_reset_date.month != now.month or subscription.last_reset_date.year != now.year:
                subscription.voucher_count_monthly = 0
                subscription.last_reset_date = now
                subscription.save(update_fields=["voucher_count_monthly", "last_reset_date"])

        except (AttributeError, ObjectDoesNotExist):
            # Company has no subscription record.
            # 1. Superusers can always bypass this.
            if request.user.is_superuser:
                # Suppress warning for personal finance section
                if not request.path.startswith("/personal-finance/"):
                    messages.info(request, "Superuser access: No subscription record found for this company.")
                request.subscription = None
                return self.get_response(request)
            
            # 2. For normal users, we MUST have a subscription.
            messages.error(request, "This company does not have an active subscription record. Please contact support.")
            if request.path != reverse("core:select_company"):
                return redirect("core:select_company")

        return self.get_response(request)
