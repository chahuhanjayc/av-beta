"""tds/urls.py"""

from django.urls import path
from . import views

app_name = "tds"

urlpatterns = [
    # Sections
    path("sections/",               views.section_list,   name="section_list"),
    path("sections/create/",        views.section_create, name="section_create"),
    path("sections/<int:pk>/edit/", views.section_edit,   name="section_edit"),

    # Entries
    path("",                        views.entry_list,    name="entry_list"),
    path("create/",                 views.entry_create,  name="entry_create"),
    path("<int:pk>/edit/",          views.entry_edit,    name="entry_edit"),
    path("<int:pk>/delete/",        views.entry_delete,  name="entry_delete"),
    path("<int:pk>/deposit/",       views.entry_deposit, name="entry_deposit"),

    # Reports
    path("return-workbench/",       views.return_workbench, name="return_workbench"),
    path("filing-pack/",            views.filing_pack, name="filing_pack"),
    path("filing-pack/download/<str:kind>/", views.filing_pack_download, name="filing_pack_download"),
    path("post-filing/",            views.post_filing_center, name="post_filing_center"),
    path("register/",               views.tds_register, name="tds_register"),
]
