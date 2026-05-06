from django.urls import path
from . import views

app_name = 'reconciliation'

urlpatterns = [
    path('delinquent-vendors/', views.delinquent_vendors_report, name='delinquent_vendors'),
]
