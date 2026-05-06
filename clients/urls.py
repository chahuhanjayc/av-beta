from django.urls import path
from . import views

app_name = "clients"

urlpatterns = [
    path("payment/", views.payment_portal, name="payment_portal"),
    path("api/subscription/status/", views.api_subscription_status, name="api_status"),
    path("api/subscription/renew/", views.api_renew_placeholder, name="api_renew"),
    path("api/usage/", views.api_usage, name="api_usage"),
]
