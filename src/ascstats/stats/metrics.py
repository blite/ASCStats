"""Derived tables, rebuilt idempotently from the loaded rows."""

from datetime import date

from django.db import transaction
from django.db.models import Q, Sum

from .models import DOWNLOAD_ROWS, IAP_ROWS, AnalyticsRow, ConversionMetric, SalesRow
from .parsing import DISCOVERY_REPORT, DOWNLOADS_REPORT


def _counts(report_name: str, event: str, field: str = "counts") -> Sum:
    return Sum(field, filter=Q(report_name=report_name, event=event))


FIELDS = [
    "impressions", "unique_impressions", "page_views", "unique_page_views",
    "first_time_downloads", "units", "iap_units",
]  # fmt: skip


def build_conversion_metrics(start: date, end: date) -> int:
    """Recompute ConversionMetric for every active app and every day in [start, end]: one
    grouped query per source, merged in memory, upserted in one statement."""
    window = {"app__is_active": True, "report_date__range": (start, end)}
    merged: dict[tuple[int, date], dict[str, int]] = {}
    for r in (
        AnalyticsRow.objects.filter(**window)
        .values("app_id", "report_date")
        .annotate(
            impressions=_counts(DISCOVERY_REPORT, "Impression"),
            unique_impressions=_counts(DISCOVERY_REPORT, "Impression", "unique_counts"),
            page_views=_counts(DISCOVERY_REPORT, "Page view"),
            unique_page_views=_counts(DISCOVERY_REPORT, "Page view", "unique_counts"),
            first_time_downloads=_counts(DOWNLOADS_REPORT, "First-time download"),
        )
    ):
        merged[(r.pop("app_id"), r.pop("report_date"))] = r
    for r in (
        SalesRow.objects.filter(**window)
        .values("app_id", "report_date")
        # aliases must not be "units": that would shadow the column for the second Sum
        .annotate(dl=Sum("units", filter=DOWNLOAD_ROWS), iap=Sum("units", filter=IAP_ROWS))
    ):
        key = (r["app_id"], r["report_date"])
        merged.setdefault(key, {}).update(units=r["dl"], iap_units=r["iap"])
    rows = [
        ConversionMetric(app_id=app_id, date=day, **{f: values.get(f) or 0 for f in FIELDS})
        for (app_id, day), values in merged.items()
        if any(v is not None for v in values.values())
    ]
    with transaction.atomic():
        ConversionMetric.objects.bulk_create(
            rows,
            batch_size=2000,
            update_conflicts=True,
            unique_fields=["app", "date"],
            update_fields=[*FIELDS, "computed_at"],
        )
    return len(rows)
