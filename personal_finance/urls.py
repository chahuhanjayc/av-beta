from django.urls import path

from . import views

app_name = "personal_finance"

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("expenses/", views.expense_list, name="expense_list"),
    path("expenses/add/", views.expense_create, name="expense_create"),
    path("expenses/<int:pk>/edit/", views.expense_edit, name="expense_edit"),
    path("expenses/<int:pk>/delete/", views.expense_delete, name="expense_delete"),
    path("income-budget/", views.income_and_budget, name="income_and_budget"),
    path("income/add/", views.income_create, name="income_create"),
    path("income/<int:pk>/delete/", views.income_delete, name="income_delete"),
    path("analytics/", views.analytics, name="analytics"),
    path("purchase-audit/", views.purchase_audit, name="purchase_audit"),
    path("templates/", views.template_manager, name="template_manager"),
    path("templates/<int:pk>/use/", views.template_use, name="template_use"),
    path("templates/<int:pk>/delete/", views.template_delete, name="template_delete"),
    path("settings/", views.settings_view, name="settings"),
    path("export/csv/", views.export_csv, name="export_csv"),
    path("export/pdf/", views.export_pdf, name="export_pdf"),
]
