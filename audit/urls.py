from django.urls import path
from .views import audit_diff_view

app_name = 'audit'

urlpatterns = [
    path('voucher/<str:object_id>/', audit_diff_view, name='voucher_diff'),
]
