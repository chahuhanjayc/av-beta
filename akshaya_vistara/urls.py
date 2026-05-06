"""
akshaya_vistara/urls.py  — Root URL configuration
"""

from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView
from core.views import protected_media

from django.contrib.auth.decorators import user_passes_test

# Restrict Django Admin to Superusers Only
admin.site.login = user_passes_test(lambda u: u.is_superuser)(admin.site.login)

urlpatterns = [
    # Admin
    path("admin/", admin.site.urls),

    # Root redirect → company select (middleware handles auth)
    path("", RedirectView.as_view(url="/core/select-company/", permanent=False)),

    # Accounts (login / logout / register)
    path("accounts/", include("accounts.urls")),

    # Core (company select, dashboard)
    path("core/", include("core.urls")),

    # Ledger
    path("ledger/", include("ledger.urls")),

    # Vouchers
    path("vouchers/", include("vouchers.urls")),

    # OCR / Bill Automation
    path("ocr/", include("ocr.urls")),

    # Reports
    path("reports/", include("reports.urls")),

    # Inventory (Phase 4.1)
    path("inventory/", include("inventory.urls")),

    # Cost Centers & Budgeting (Phase 4)
    path("costcenter/", include("costcenter.urls")),

    # Purchase & Sales Orders (Phase 5)
    path("orders/", include("orders.urls")),

    # Payroll (Phase 6)
    path("payroll/", include("payroll.urls")),

    # Fixed Assets & Depreciation (Phase 7)
    path("assets/", include("fixedassets.urls")),

    # TDS / TCS (Phase 7)
    path("tds/", include("tds.urls")),

    # Multi-currency & Forex (Phase 8)
    path("forex/", include("forex.urls")),

    # Clients & Subscription
    path('clients/', include('clients.urls')),
    path('audit/', include('audit.urls')),
    path('reconciliation/', include('reconciliation.urls')),
    path('receivables/', include('receivables.urls')),
    path('portal/', include('portal.urls')),
    path('migration/', include('migration.urls')),
    path('gstr2b/', include('gstr2b.urls')),
    path('integrations/', include('integrations.urls')),
    ]
# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    # Production: serve through protected view to enforce company isolation
    # Note: re_path regex extracts the path portion after /media/
    urlpatterns += [
        re_path(r"^media/(?P<path>.*)$", protected_media),
    ]
