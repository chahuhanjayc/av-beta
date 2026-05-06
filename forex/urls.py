"""forex/urls.py"""

from django.urls import path
from . import views

app_name = "forex"

urlpatterns = [
    # Currencies
    path("currencies/",               views.currency_list,   name="currency_list"),
    path("currencies/create/",        views.currency_create, name="currency_create"),
    path("currencies/<int:pk>/edit/", views.currency_edit,   name="currency_edit"),

    # Exchange Rates
    path("rates/",                    views.exchange_rate_list,   name="exchange_rate_list"),
    path("rates/create/",             views.exchange_rate_create, name="exchange_rate_create"),
    path("rates/<int:pk>/edit/",      views.exchange_rate_edit,   name="exchange_rate_edit"),
    path("rates/<int:pk>/delete/",    views.exchange_rate_delete, name="exchange_rate_delete"),

    # Position summary
    path("position/",                 views.forex_position, name="forex_position"),
]
