from django.urls import path, re_path

from ascstats.stats import converters  # noqa: F401  (registers <appid:…>)

from . import views

urlpatterns = [
    path("charts/", views.charts_home, name="charts_home"),
    path("charts/track/", views.charts_track, name="charts_track"),
    path("charts/<str:storefront>/<int:genre>/", views.charts, name="charts"),
    path("charts/<str:storefront>/<int:genre>/<int:days>d/", views.charts, name="charts_window"),
    path("charts/<str:storefront>/<int:genre>/<isodate:day>/", views.charts, name="charts_day"),
    path(
        "charts/<str:storefront>/<int:genre>/<isodate:day>/<int:days>d/",
        views.charts,
        name="charts_day_window",
    ),
    path("charts/<str:storefront>/<int:genre>/fetch/", views.charts_fetch, name="charts_fetch"),
    path("ideas/new/", views.idea_new, name="idea_new"),
    path("app/<appid:apple_id>/real/", views.app_make_real, name="app_make_real"),
    path("app/<appid:apple_id>/competitors/", views.app_competitors, name="app_competitors"),
    path(
        "app/<appid:apple_id>/competitors/<int:rival_id>/",
        views.app_competitor,
        name="app_competitor",
    ),
    path(
        "app/<appid:apple_id>/competitors/<int:rival_id>/stores/",
        views.app_competitor_stores,
        name="app_competitor_stores",
    ),
    path(
        "app/<appid:apple_id>/competitors/manage/",
        views.app_competitors_manage,
        name="app_competitors_manage",
    ),
    path(
        "app/<appid:apple_id>/competitors/follow/",
        views.app_competitors_follow,
        name="app_competitors_follow",
    ),
    path(
        "app/<appid:apple_id>/competitors/unfollow/",
        views.app_competitors_unfollow,
        name="app_competitors_unfollow",
    ),
    path("app/<appid:apple_id>/keywords/", views.app_keywords, name="app_keywords"),
    path(
        "app/<appid:apple_id>/keywords/manage/",
        views.app_keywords_manage,
        name="app_keywords_manage",
    ),
    path(
        "app/<appid:apple_id>/keywords/delete/",
        views.app_keywords_delete,
        name="app_keywords_delete",
    ),
    re_path(  # a storefront's tab; two letters, so never "manage" or a keyword's id
        r"^app/(?P<apple_id>-?[0-9]+)/keywords/(?P<storefront>[a-z]{2})/$",
        views.app_keywords,
        name="app_keywords_front",
    ),
    re_path(  # the Suggestions card under a storefront's tab, and its two forms
        r"^app/(?P<apple_id>-?[0-9]+)/keywords/(?P<storefront>[a-z]{2})/suggest/$",
        views.app_keyword_suggest,
        name="app_keyword_suggest",
    ),
    re_path(
        r"^app/(?P<apple_id>-?[0-9]+)/keywords/(?P<storefront>[a-z]{2})/suggest/look/$",
        views.app_keyword_suggest_look,
        name="app_keyword_suggest_look",
    ),
    re_path(
        r"^app/(?P<apple_id>-?[0-9]+)/keywords/(?P<storefront>[a-z]{2})/suggest/track/$",
        views.app_keyword_suggest_track,
        name="app_keyword_suggest_track",
    ),
    re_path(  # the scan form on the Competitors' keywords card
        r"^app/(?P<apple_id>-?[0-9]+)/keywords/(?P<storefront>[a-z]{2})/scan/$",
        views.app_keyword_scan,
        name="app_keyword_scan",
    ),
    path(
        "app/<appid:apple_id>/keywords/<int:keyword_id>/",
        views.app_keyword,
        name="app_keyword",
    ),
]
