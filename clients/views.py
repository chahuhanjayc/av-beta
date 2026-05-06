from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from .models import ClientSubscription, PaymentTransaction

@login_required
def payment_portal(request):
    """
    Placeholder for a payment portal.
    In a real app, this would integrate with Razorpay, Stripe, etc.
    """
    company = request.current_company
    subscription = getattr(company, "subscription", None)
    
    payments = []
    if subscription:
        payments = subscription.payments.all().order_by("-payment_date")[:5]
        
    return render(request, "clients/payment_portal.html", {
        "subscription": subscription,
        "payments": payments,
    })

@login_required
def api_subscription_status(request):
    """Returns JSON with current company's subscription status."""
    subscription = getattr(request.current_company, "subscription", None)
    if not subscription:
        return JsonResponse({"error": "No subscription found"}, status=404)
        
    return JsonResponse({
        "plan": subscription.get_plan_display(),
        "status": subscription.get_status_display(),
        "expires": subscription.subscription_end.strftime("%Y-%m-%d"),
        "is_active": subscription.is_active(),
        "usage": {
            "current": subscription.voucher_count_monthly,
            "limit": subscription.get_voucher_limit(),
            "percent": subscription.usage_percentage()
        }
    })

@login_required
def api_usage(request):
    """Returns monthly voucher usage for current company."""
    subscription = getattr(request.current_company, "subscription", None)
    if not subscription:
        return JsonResponse({"error": "No subscription found"}, status=404)
        
    return JsonResponse({
        "vouchers_this_month": subscription.voucher_count_monthly,
        "limit": subscription.get_voucher_limit(),
        "percent": subscription.usage_percentage()
    })

@login_required
def api_renew_placeholder(request):
    """Placeholder for payment initiation."""
    return JsonResponse({
        "status": "pending",
        "message": "Payment gateway integration required. Please contact support for manual renewal.",
        "support_contact": "+91 00000 00000"
    })
