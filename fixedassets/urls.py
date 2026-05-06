"""fixedassets/urls.py"""

from django.urls import path
from . import views

app_name = "fixedassets"

urlpatterns = [
    # Asset Groups
    path("groups/",               views.asset_group_list,   name="asset_group_list"),
    path("groups/create/",        views.asset_group_create, name="asset_group_create"),
    path("groups/<int:pk>/edit/", views.asset_group_edit,   name="asset_group_edit"),

    # Fixed Assets
    path("",                      views.asset_list,   name="asset_list"),
    path("create/",               views.asset_create, name="asset_create"),
    path("<int:pk>/",             views.asset_detail, name="asset_detail"),
    path("<int:pk>/edit/",        views.asset_edit,   name="asset_edit"),
    path("<int:pk>/delete/",      views.asset_delete, name="asset_delete"),

    # Depreciation
    path("<int:pk>/depreciate/",  views.depreciation_post, name="depreciation_post"),

    # Register report
    path("register/",             views.asset_register, name="asset_register"),
]
