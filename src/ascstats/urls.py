from django.contrib import admin
from django.urls import path

from ascstats.api import api
from ascstats.stats import views

urlpatterns = [
    # No query strings anywhere on the site, so every page and fragment is a plain path
    path("", views.dashboard, name="dashboard"),
    path("<int:days>d/", views.dashboard, name="dashboard_period"),
    path("chart/<int:apple_id>/<int:days>/", views.chart, name="chart"),
    path("revenue/", views.revenue, name="revenue"),
    path("revenue/<int:days>d/", views.revenue, name="revenue_period"),
    path("funnel/", views.funnel, name="funnel"),
    path("funnel/app/<int:apple_id>/", views.funnel_app, name="funnel_app"),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
