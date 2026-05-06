"""
accounts/urls.py
"""

from django.urls import path
from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("mfa/", views.mfa_verify_view, name="mfa_verify"),
    path("logout/", views.logout_view, name="logout"),
    path("register/", views.register_view, name="register"),
]
