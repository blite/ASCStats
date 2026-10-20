from django.contrib import admin

from .models import AnalyticsRow, App, AppVersion, ConversionMetric, IngestionRun, SalesRow


@admin.register(App)
class AppAdmin(admin.ModelAdmin):
    list_display = ["name", "apple_id", "sku", "bundle_id", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "sku", "bundle_id"]


@admin.register(SalesRow)
class SalesRowAdmin(admin.ModelAdmin):
    list_display = [
        "report_date", "app", "sku", "product_type", "country_code", "device", "units",
        "developer_proceeds", "currency_of_proceeds",
    ]  # fmt: skip
    list_filter = ["app", "product_type", "device", "country_code"]
    date_hierarchy = "report_date"
    search_fields = ["sku", "title"]
    list_select_related = ["app"]


@admin.register(AnalyticsRow)
class AnalyticsRowAdmin(admin.ModelAdmin):
    list_display = [
        "report_date", "app", "report_name", "event", "source_type", "device", "territory",
        "counts", "unique_counts",
    ]  # fmt: skip
    list_filter = ["app", "report_name", "event", "source_type", "device"]
    date_hierarchy = "report_date"
    list_select_related = ["app"]


@admin.register(ConversionMetric)
class ConversionMetricAdmin(admin.ModelAdmin):
    list_display = [
        "date", "app", "impressions", "page_views", "first_time_downloads", "units",
        "conversion_rate",
    ]  # fmt: skip
    list_filter = ["app"]
    date_hierarchy = "date"


@admin.register(AppVersion)
class AppVersionAdmin(admin.ModelAdmin):
    list_display = ["app", "version", "first_seen", "platform", "asc_state", "asc_created"]
    list_filter = ["app", "asc_state"]
    list_select_related = ["app"]


@admin.register(IngestionRun)
class IngestionRunAdmin(admin.ModelAdmin):
    list_display = [
        "started_at", "report", "report_date", "app", "step", "source", "status", "rows_parsed",
        "rows_deleted", "rows_loaded",
    ]  # fmt: skip
    list_filter = ["status", "report", "step", "source"]
    date_hierarchy = "report_date"
    readonly_fields = [f.name for f in IngestionRun._meta.fields]
