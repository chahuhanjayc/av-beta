from django.urls import path
from . import views

app_name = 'gstr2b'

urlpatterns = [
    path('upload/', views.upload_gstr2b, name='upload'),
    path('results/', views.reconciliation_results, name='results'),
    path('vendor-register/', views.vendor_compliance_register, name='vendor_register'),
    path('vendor-register/follow-up/', views.vendor_followup, name='vendor_followup'),
    path('vendor-register/create-tasks/', views.bulk_create_vendor_tasks, name='vendor_tasks'),
    path('create-voucher/<int:pk>/', views.create_voucher_from_2b, name='create_voucher'),
    path('mark-action/<int:pk>/', views.mark_2b_action, name='mark_action'),
    path('bulk-action/', views.bulk_mark_2b_action, name='bulk_action'),
    path('create-tasks/', views.bulk_create_ims_tasks, name='create_tasks'),
    path('bulk-create/', views.bulk_create_vouchers_from_2b, name='bulk_create'),
]
