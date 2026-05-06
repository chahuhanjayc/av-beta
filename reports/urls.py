"""
reports/urls.py
"""

from django.urls import path
from . import views

app_name = "reports"

urlpatterns = [
    path("",                          views.reports_home,        name="home"),
    path("profit-loss/",              views.profit_loss,         name="profit_loss"),
    path("profit-loss/export/",       views.export_pl_excel,     name="export_pl_excel"),
    path("profit-loss/pdf/",          views.profit_loss_pdf,     name="profit_loss_pdf"),
    path("balance-sheet/",            views.balance_sheet,       name="balance_sheet"),
    path("balance-sheet/export/",     views.export_bs_excel,     name="export_bs_excel"),
    path("balance-sheet/pdf/",        views.balance_sheet_pdf,   name="balance_sheet_pdf"),
    path("trial-balance/",            views.trial_balance,       name="trial_balance"),
    path("trial-balance/export/",     views.export_tb_excel,     name="export_tb_excel"),
    path("trial-balance/pdf/",        views.trial_balance_pdf,   name="trial_balance_pdf"),
    path("receivables-aging/",        views.receivables_aging,   name="receivables_aging"),
    path("msme-overdue/",             views.msme_overdue_report, name="msme_overdue"),
    path("gst/", views.gst_report, name="gst_report"),
    path("project-pnl/<int:cost_center_id>/", views.project_pnl, name="project_pnl"),

    path("gst/gstr1-export/",         views.gstr1_export,        name="gstr1_export"),
    path("gst/gstr3b-export/",        views.gstr3b_export,       name="gstr3b_export"),
    path("cash-flow/",                views.cash_flow,            name="cash_flow"),
    path("cash-flow-forecast/",       views.cash_flow_forecast,   name="cash_flow_forecast"),

    # Simplified Reports (Phase 5)
    path("dashboard-financials/",     views.dashboard_financials, name="dashboard_financials"),
    path("profit-loss-simple/",  views.profit_loss_simple,  name="profit_loss_simple"),
    path("balance-sheet-simple/", views.balance_sheet_simple, name="balance_sheet_simple"),
    path("trial-balance-simple/", views.trial_balance_simple, name="trial_balance_simple"),
    path("day-book/",            views.day_book_view,            name="day_book"),

    # Drill-down Reports
    path("group/<str:group_name>/",   views.report_group_detail,  name="group_detail"),
    path("ledger/<int:ledger_id>/",   views.report_ledger_detail, name="ledger_detail"),
]
