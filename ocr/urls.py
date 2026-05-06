"""
ocr/urls.py
"""

from django.urls import path
from . import views

app_name = "ocr"

urlpatterns = [
    path("",                              views.ocr_list,               name="list"),
    path("upload/",                       views.ocr_upload,             name="upload"),
    path("<int:pk>/status/",              views.ocr_status,             name="status"),
    path("<int:pk>/verify/",              views.ocr_verify,             name="verify"),
    path("<int:pk>/confirm/",             views.ocr_confirm,            name="confirm"),
    path("<int:pk>/reject/",              views.ocr_reject,             name="reject"),
    # AJAX: quick-create stock item from line items table
    path("stock-item/quick-create/",      views.stock_item_quick_create, name="stock_item_quick_create"),
    path("gst-certificate/scan/",         views.gst_certificate_scan,    name="gst_certificate_scan"),
    path("scan/inline/",                  views.ocr_inline_scan,         name="inline_scan"),
    # Public (token-based) portal for client uploads
    path("portal/<str:token>/",           views.client_upload_portal,    name="client_portal"),
]
