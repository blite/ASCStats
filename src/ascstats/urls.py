from django.contrib import admin
from django.urls import path

from ascstats.api import api
from ascstats.stats import views
from ascstats.stats.charts import ALL

urlpatterns = [
    # No query strings anywhere on the site, so every page and fragment is a plain path
    path("", views.dashboard, name="dashboard"),
    path("<int:days>d/", views.dashboard, name="dashboard_period"),
    path("all/", views.dashboard, {"days": ALL}, name="dashboard_all"),
    path("app/<int:apple_id>/", views.app_page, name="app"),
    path("chart/<int:apple_id>/<int:days>/", views.chart, name="chart"),
    path("revenue/", views.revenue, name="revenue"),
    path("revenue/<int:days>d/", views.revenue, name="revenue_period"),
    path("revenue/all/", views.revenue, {"days": ALL}, name="revenue_all"),
    path("reviews/", views.reviews, name="reviews"),
    path("reviews/<slug:state>/", views.reviews, name="reviews_state"),
    path("reviews/app/<int:apple_id>/", views.reviews, name="reviews_app"),
    path("reviews/<slug:state>/app/<int:apple_id>/", views.reviews, name="reviews_state_app"),
    path("funnel/", views.funnel, name="funnel"),
    path("funnel/app/<int:apple_id>/", views.funnel_app, name="funnel_app"),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
