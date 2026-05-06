from django.urls import path
from . import views

app_name = "portal"

urlpatterns = [
    path("login/", views.portal_login, name="login"),
    path("logout/", views.portal_logout, name="logout"),
    path("request/<str:token>/", views.client_document_request_upload, name="document_request_upload"),
    path("dashboard/", views.portal_dashboard, name="dashboard"),
    path("confirm-balance/", views.portal_confirm_balance, name="confirm_balance"),
    path("download-pdf/", views.download_ledger_pdf, name="download_pdf"),
    
    # CA/Staff Dashboard
    path("client-requests/reminders/", views.client_request_reminders, name="client_request_reminders"),
    path("client-requests/campaign/", views.client_request_campaign, name="client_request_campaign"),
    path("client-requests/new/", views.client_request_create, name="client_request_create"),
    path("client-requests/", views.client_request_room, name="client_requests"),
    path("ca-dashboard/", views.ca_dashboard_view, name="ca_dashboard"),
    path("ca-view-ledger/<int:user_id>/", views.ca_view_user_ledger, name="ca_view_ledger"),
    path("ca-download-pdf/<int:user_id>/", views.ca_download_user_pdf, name="ca_download_pdf"),
]
