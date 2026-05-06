from django.urls import path
from .views import bulk_settlement_view

app_name = 'receivables'

urlpatterns = [
    path('bulk-settlement/', bulk_settlement_view, name='bulk_settlement'),
]
