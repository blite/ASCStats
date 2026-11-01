from django.contrib import admin
from django.contrib.auth import views as auth_views
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
    path("app/<int:apple_id>/ratings/", views.app_ratings, name="app_ratings"),
    path(
        "app/<int:apple_id>/ratings/<slug:country>/<slug:window>/",
        views.app_ratings_chart,
        name="app_ratings_chart",
    ),
    path("app/<int:apple_id>/reviews/", views.app_reviews, name="app_reviews"),
    path("app/<int:apple_id>/reviews/<slug:state>/", views.app_reviews, name="app_reviews_state"),
    path("app/<int:apple_id>/keywords/", views.app_soon, {"tab": "keywords"}, name="app_keywords"),
    path(
        "app/<int:apple_id>/competitors/",
        views.app_soon,
        {"tab": "competitors"},
        name="app_competitors",
    ),
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
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="stats/login.html", redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("logout/", views.sign_out, name="logout"),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
