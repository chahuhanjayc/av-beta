"""costcenter/urls.py"""
from django.urls import path
from . import views

app_name = "costcenter"

urlpatterns = [
    # ── Cost Centers ──────────────────────────────────────────────────────────
    path("",                         views.cost_center_list,   name="cost_center_list"),
    path("create/",                  views.cost_center_create, name="cost_center_create"),
    path("<int:pk>/edit/",           views.cost_center_edit,   name="cost_center_edit"),
    path("<int:pk>/delete/",         views.cost_center_delete, name="cost_center_delete"),

    # ── Budget Heads ─────────────────────────────────────────────────────────
    path("budgets/",                 views.budget_list,        name="budget_list"),
    path("budgets/create/",          views.budget_create,      name="budget_create"),
    path("budgets/<int:pk>/edit/",   views.budget_edit,        name="budget_edit"),
    path("budgets/<int:pk>/delete/", views.budget_delete,      name="budget_delete"),

    # ── Reports ───────────────────────────────────────────────────────────────
    path("reports/variance/",        views.budget_variance,    name="budget_variance"),
    path("reports/by-center/",       views.cost_center_report, name="cost_center_report"),

    # ── AJAX ─────────────────────────────────────────────────────────────────
    path("api/autocomplete/",        views.cc_autocomplete,    name="cc_autocomplete"),
    path("api/quick-add/",           views.quick_add,          name="quick_add"),
]
