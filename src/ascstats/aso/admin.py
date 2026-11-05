from django.contrib import admin

from .models import Competitor, CompetitorRelease, Keyword, KeywordScan, ListingChange


@admin.register(Competitor)
class CompetitorAdmin(admin.ModelAdmin):
    list_display = ["name", "apple_id", "seller", "version", "version_released_on", "price"]
    search_fields = ["name", "seller"]
    filter_horizontal = ["tracked_for"]


@admin.register(CompetitorRelease)
class CompetitorReleaseAdmin(admin.ModelAdmin):
    list_display = ["competitor", "version", "released_on", "first_seen"]
    list_filter = ["competitor"]


@admin.register(Keyword)
class KeywordAdmin(admin.ModelAdmin):
    list_display = ["term", "storefront", "created_at"]
    list_filter = ["storefront", "tracked_for"]
    search_fields = ["term"]
    filter_horizontal = ["tracked_for"]


@admin.register(ListingChange)
class ListingChangeAdmin(admin.ModelAdmin):
    list_display = ["competitor", "date", "field", "version", "storefront"]
    list_filter = ["field", "competitor"]


@admin.register(KeywordScan)
class KeywordScanAdmin(admin.ModelAdmin):
    list_display = ["competitor", "storefront", "status", "searched", "created_at", "finished_at"]
    list_filter = ["status", "storefront"]
