"""
payroll/urls.py — Payroll URL configuration
"""

from django.urls import path
from . import views

app_name = "payroll"

urlpatterns = [
    # ── Employees ──────────────────────────────────────────────────────────
    path("employees/",                 views.employee_list,   name="employee_list"),
    path("employees/create/",          views.employee_create, name="employee_create"),
    path("employees/quick-add/",       views.quick_add,       name="quick_add"),
    path("employees/ocr-upload/",      views.employee_ocr_upload, name="employee_ocr_upload"),
    path("employees/<int:pk>/edit/",   views.employee_edit,   name="employee_edit"),
    path("employees/<int:pk>/delete/", views.employee_delete, name="employee_delete"),

    # ── Salary Structures ──────────────────────────────────────────────────
    path("structures/",                       views.salary_structure_list,   name="salary_structure_list"),
    path("structures/create/",                views.salary_structure_create, name="salary_structure_create"),
    path("structures/<int:pk>/edit/",         views.salary_structure_edit,   name="salary_structure_edit"),
    path("structures/<int:pk>/delete/",       views.salary_structure_delete, name="salary_structure_delete"),

    # ── Payroll Runs ───────────────────────────────────────────────────────
    path("runs/",                        views.payroll_run_list,     name="payroll_run_list"),
    path("runs/create/",                 views.payroll_run_create,   name="payroll_run_create"),
    path("runs/<int:pk>/",               views.payroll_run_detail,   name="payroll_run_detail"),
    path("runs/<int:pk>/process/",       views.payroll_run_process,  name="payroll_run_process"),
    path("runs/<int:pk>/finalize/",      views.payroll_run_finalize, name="payroll_run_finalize"),

    # ── Payslips ───────────────────────────────────────────────────────────
    path("payslips/<int:pk>/",       views.payslip_detail, name="payslip_detail"),
    path("payslips/<int:pk>/edit/",  views.payslip_edit,   name="payslip_edit"),

    # ── Summary Report ─────────────────────────────────────────────────────
    path("summary/", views.payroll_summary, name="payroll_summary"),
]
