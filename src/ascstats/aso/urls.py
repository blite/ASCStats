from django.urls import path, re_path

from ascstats.stats import converters  # noqa: F401  (registers <appid:…>)

from . import views

urlpatterns = [
    path("ideas/new/", views.idea_new, name="idea_new"),
    path("app/<appid:apple_id>/real/", views.app_make_real, name="app_make_real"),
    path("app/<appid:apple_id>/competitors/", views.app_competitors, name="app_competitors"),
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
        r"^app/(?P<apple_id>-?[0-9]+)/keywords/(?P<storefront>[a-z]{2})/ideas/$",
        views.app_keyword_ideas,
        name="app_keyword_ideas",
    ),
    re_path(
        r"^app/(?P<apple_id>-?[0-9]+)/keywords/(?P<storefront>[a-z]{2})/ideas/look/$",
        views.app_keyword_ideas_look,
        name="app_keyword_ideas_look",
    ),
    re_path(
        r"^app/(?P<apple_id>-?[0-9]+)/keywords/(?P<storefront>[a-z]{2})/ideas/track/$",
        views.app_keyword_ideas_track,
        name="app_keyword_ideas_track",
    ),
    path(
        "app/<appid:apple_id>/keywords/<int:keyword_id>/",
        views.app_keyword,
        name="app_keyword",
    ),
]
