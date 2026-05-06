from django.urls import path

from . import views


app_name = "integrations"

urlpatterns = [
    path("", views.integration_dashboard, name="dashboard"),
    path("control-room/", views.statutory_integration_control_room, name="statutory_control"),
    path("provider-readiness/", views.provider_go_live_readiness, name="provider_readiness"),
    path("provider-readiness/retry/<int:job_id>/", views.provider_retry_job_update, name="provider_retry_job_update"),
    path("bank-feed/", views.bank_feed_import, name="bank_feed_import"),
    path("connectors/<str:connector_type>/", views.connector_update, name="connector_update"),
    path("e-invoice/", views.e_invoice_cockpit, name="e_invoice_cockpit"),
    path("e-invoice/<int:voucher_id>/generate/", views.e_invoice_cockpit_generate, name="e_invoice_cockpit_generate"),
    path("e-way-bill/", views.e_way_bill_cockpit, name="e_way_bill_cockpit"),
    path("e-way-bill/<int:voucher_id>/generate/", views.e_way_bill_cockpit_generate, name="e_way_bill_cockpit_generate"),
    path("evidence/", views.evidence_center, name="evidence_center"),
    path("gst/results/import/", views.gst_result_import, name="gst_result_import"),
    path("traces/results/import/", views.traces_result_import, name="traces_result_import"),
    path("api/status/", views.integration_status_api, name="status_api"),
    path("api/gst/gstin/", views.gstin_lookup_api, name="gstin_lookup_api"),
    path("api/gst/e-invoice/<int:voucher_id>/payload/", views.e_invoice_payload_download, name="e_invoice_payload_download"),
    path("api/gst/e-invoice/<int:voucher_id>/", views.generate_e_invoice_api, name="generate_e_invoice_api"),
    path("api/gst/e-invoice/<int:voucher_id>/mark/", views.mark_e_invoice_status, name="mark_e_invoice_status"),
    path("api/gst/e-way-bill/<int:voucher_id>/payload/", views.e_way_bill_payload_download, name="e_way_bill_payload_download"),
    path("api/gst/e-way-bill/<int:voucher_id>/", views.generate_e_way_bill_api, name="generate_e_way_bill_api"),
    path("api/gst/e-way-bill/<int:voucher_id>/mark/", views.mark_e_way_bill_status, name="mark_e_way_bill_status"),
    path("webhook/whatsapp/document/", views.whatsapp_webhook, name="whatsapp_document_webhook"),
]
