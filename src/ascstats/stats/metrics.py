"""Derived tables, rebuilt idempotently from the loaded rows."""

from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q, Sum

from .models import DOWNLOAD_ROWS, AnalyticsRow, App, ConversionMetric, SalesRow
from .parsing import DISCOVERY_REPORT, DOWNLOADS_REPORT


def _counts(report_name: str, event: str) -> Sum:
    return Sum("counts", filter=Q(report_name=report_name, event=event))


def build_conversion_metrics(start: date, end: date) -> int:
    """Recompute ConversionMetric for every active app and every day in [start, end]."""
    written = 0
    with transaction.atomic():
        for app in App.objects.filter(is_active=True):
            day = start
            while day <= end:
                analytics = AnalyticsRow.objects.filter(app=app, report_date=day).aggregate(
                    impressions=_counts(DISCOVERY_REPORT, "Impression"),
                    page_views=_counts(DISCOVERY_REPORT, "Page view"),
                    first_time=_counts(DOWNLOADS_REPORT, "First-time download"),
                )
                units = SalesRow.objects.filter(DOWNLOAD_ROWS, app=app, report_date=day).aggregate(
                    units=Sum("units")
                )["units"]
                if any(v is not None for v in analytics.values()) or units is not None:
                    page_views = analytics["page_views"] or 0
                    first_time = analytics["first_time"] or 0
                    ConversionMetric.objects.update_or_create(
                        app=app,
                        date=day,
                        defaults={
                            "impressions": analytics["impressions"] or 0,
                            "page_views": page_views,
                            "first_time_downloads": first_time,
                            "units": units or 0,
                            "conversion_rate": first_time / page_views if page_views else None,
                        },
                    )
                    written += 1
                day += timedelta(days=1)
    return written
