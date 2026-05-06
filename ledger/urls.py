"""
ledger/urls.py
"""

from django.urls import path
from . import views

app_name = "ledger"

urlpatterns = [
    path("",                        views.ledger_list,        name="list"),
    path("create/",                 views.ledger_create,      name="create"),
    path("api/suggestions/",        views.ledger_suggestions, name="api_suggestions"),
    path("quick-add/",              views.ledger_quick_add,   name="quick_add"),    # AJAX
    path("<int:pk>/edit/",          views.ledger_edit,        name="edit"),
    path("<int:pk>/deactivate/",    views.ledger_deactivate,  name="deactivate"),
    path("<int:pk>/reactivate/",    views.ledger_reactivate,  name="reactivate"),
    path("<int:pk>/statement/",     views.ledger_statement,   name="statement"),
]
