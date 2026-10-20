"""Derived tables, rebuilt idempotently from the loaded rows."""

from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q, Sum

from .models import DOWNLOAD_ROWS, IAP_ROWS, AnalyticsRow, App, ConversionMetric, SalesRow
from .parsing import DISCOVERY_REPORT, DOWNLOADS_REPORT


def _counts(report_name: str, event: str, field: str = "counts") -> Sum:
    return Sum(field, filter=Q(report_name=report_name, event=event))


def build_conversion_metrics(start: date, end: date) -> int:
    """Recompute ConversionMetric for every active app and every day in [start, end]."""
    written = 0
    with transaction.atomic():
        for app in App.objects.filter(is_active=True):
            day = start
            while day <= end:
                analytics = AnalyticsRow.objects.filter(app=app, report_date=day).aggregate(
                    impressions=_counts(DISCOVERY_REPORT, "Impression"),
                    unique_impressions=_counts(DISCOVERY_REPORT, "Impression", "unique_counts"),
                    page_views=_counts(DISCOVERY_REPORT, "Page view"),
                    unique_page_views=_counts(DISCOVERY_REPORT, "Page view", "unique_counts"),
                    first_time=_counts(DOWNLOADS_REPORT, "First-time download"),
                )
                sales = SalesRow.objects.filter(app=app, report_date=day).aggregate(
                    downloads=Sum("units", filter=DOWNLOAD_ROWS),
                    purchases=Sum("units", filter=IAP_ROWS),
                )
                if any(v is not None for v in (*analytics.values(), *sales.values())):
                    ConversionMetric.objects.update_or_create(
                        app=app,
                        date=day,
                        defaults={
                            "impressions": analytics["impressions"] or 0,
                            "unique_impressions": analytics["unique_impressions"] or 0,
                            "page_views": analytics["page_views"] or 0,
                            "unique_page_views": analytics["unique_page_views"] or 0,
                            "first_time_downloads": analytics["first_time"] or 0,
                            "units": sales["downloads"] or 0,
                            "iap_units": sales["purchases"] or 0,
                        },
                    )
                    written += 1
                day += timedelta(days=1)
    return written
