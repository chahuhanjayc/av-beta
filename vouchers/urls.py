"""
vouchers/urls.py
"""

from django.urls import path
from . import views

app_name = "vouchers"

urlpatterns = [
    path("",                             views.voucher_list,          name="list"),
    path("create/",                      views.voucher_create,        name="create"),
    path("suggestion-api/",              views.voucher_suggestion_api,name="suggestion_api"),
    path("create-quick/",                views.create_voucher,        name="create_quick"),
    path("bulk/",                        views.bulk_action,           name="bulk_action"),
    path("quality/",                     views.voucher_quality,       name="quality"),
    path("outstanding/",                 views.outstanding_statement, name="outstanding"),
    path("outstanding/create-tasks/",    views.create_collection_tasks,name="collection_tasks"),
    path("<int:pk>/",                    views.voucher_detail,        name="detail"),
    path("<int:pk>/unapprove/",          views.voucher_unapprove,     name="unapprove"),
    path("<int:pk>/email-invoice/",       views.send_invoice_email,    name="email_invoice"),
    path("<int:pk>/payment-reminder/",    views.send_payment_reminder, name="payment_reminder"),
    path("<int:pk>/edit/",               views.voucher_edit,          name="edit"),
    path("<int:pk>/delete/",             views.voucher_delete,        name="delete"),
    path("<int:pk>/simulate-payment/",   views.simulate_payment,      name="simulate_payment"),
    path("<int:pk>/pdf/",                views.invoice_pdf,            name="invoice_pdf"),
    path("export/tally/",                views.export_to_tally,       name="export_tally"),
    path("webhook/whatsapp/reply/",      views.whatsapp_webhook,      name="whatsapp_webhook"),
]
