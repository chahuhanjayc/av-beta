"""
inventory/urls.py
"""

from django.urls import path
from . import views

app_name = "inventory"

urlpatterns = [
    # ── Stock Items CRUD ──────────────────────────────────────────────────────
    path("",                         views.stock_item_list,       name="list"),
    path("create/",                  views.stock_item_create,     name="create"),
    path("bulk-create-ocr/",         views.stock_item_bulk_ocr,   name="bulk_create_ocr"),
    path("<int:pk>/edit/",           views.stock_item_edit,       name="edit"),
    path("<int:pk>/deactivate/",     views.stock_item_deactivate, name="deactivate"),

    # ── Reports ───────────────────────────────────────────────────────────────
    path("summary/",                 views.stock_summary,         name="summary"),
    path("valuation/",               views.stock_valuation,       name="valuation"),
    path("low-stock/",               views.low_stock_alert,       name="low_stock"),
    path("batch-summary/",           views.batch_summary,         name="batch_summary"),

    # ── Godowns ───────────────────────────────────────────────────────────────
    path("godowns/",                 views.godown_list,           name="godown_list"),
    path("godowns/quick-add/",       views.godown_quick_add,      name="godown_quick_add"),
    path("godowns/create/",          views.godown_create,         name="godown_create"),
    path("godowns/<int:pk>/edit/",   views.godown_edit,           name="godown_edit"),
    path("godowns/<int:pk>/delete/", views.godown_delete,         name="godown_delete"),

    # ── Batches ───────────────────────────────────────────────────────────────
    path("batches/",                 views.batch_list,            name="batch_list"),
    path("batches/quick-add/",       views.batch_quick_add,       name="batch_quick_add"),
    path("batches/create/",          views.batch_create,          name="batch_create"),
    path("batches/<int:pk>/edit/",   views.batch_edit,            name="batch_edit"),
    path("batches/<int:pk>/delete/", views.batch_delete,          name="batch_delete"),

    # ── AJAX ─────────────────────────────────────────────────────────────────
    path("api/item/<int:pk>/price/",        views.item_price_lookup, name="item_price"),
]
