from django.contrib import admin
from django.urls import path

from ascstats.api import api
from ascstats.stats import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("chart/", views.chart, name="chart"),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
