from django.urls import path, re_path

from . import views

urlpatterns = [
    path("app/<int:apple_id>/competitors/", views.app_competitors, name="app_competitors"),
    path(
        "app/<int:apple_id>/competitors/manage/",
        views.app_competitors_manage,
        name="app_competitors_manage",
    ),
    path("app/<int:apple_id>/keywords/", views.app_keywords, name="app_keywords"),
    path(
        "app/<int:apple_id>/keywords/manage/",
        views.app_keywords_manage,
        name="app_keywords_manage",
    ),
    path(
        "app/<int:apple_id>/keywords/delete/",
        views.app_keywords_delete,
        name="app_keywords_delete",
    ),
    re_path(  # a storefront's tab; two letters, so never "manage" or a keyword's id
        r"^app/(?P<apple_id>[0-9]+)/keywords/(?P<storefront>[a-z]{2})/$",
        views.app_keywords,
        name="app_keywords_front",
    ),
    path(
        "app/<int:apple_id>/keywords/<int:keyword_id>/",
        views.app_keyword,
        name="app_keyword",
    ),
]
