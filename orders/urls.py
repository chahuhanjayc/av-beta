"""
orders/urls.py — Purchase & Sales Orders URL configuration
"""

from django.urls import path
from . import views

app_name = "orders"

urlpatterns = [
    # ── List & create ────────────────────────────────────────────────────────
    path("", views.order_list, name="order_list"),
    path("create/", views.order_create, name="order_create"),

    # ── Detail, edit, status actions ────────────────────────────────────────
    path("<int:pk>/", views.order_detail, name="order_detail"),
    path("<int:pk>/edit/", views.order_edit, name="order_edit"),
    path("<int:pk>/confirm/", views.order_confirm, name="order_confirm"),
    path("<int:pk>/cancel/", views.order_cancel, name="order_cancel"),

    # ── Convert to voucher ───────────────────────────────────────────────────
    path("<int:pk>/convert/", views.order_convert, name="order_convert"),

    # ── Reports ──────────────────────────────────────────────────────────────
    path("open/", views.open_orders, name="open_orders"),
]
