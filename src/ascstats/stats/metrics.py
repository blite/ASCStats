"""Derived tables, rebuilt idempotently from the loaded rows."""

from datetime import date

from django.db import transaction
from django.db.models import Sum

from .models import ConversionMetric, FunnelDay, SalesDay

FUNNEL_FIELDS = [
    "impressions", "unique_impressions", "page_views", "unique_page_views",
    "first_time_downloads",
]  # fmt: skip
FIELDS = [*FUNNEL_FIELDS, "units", "iap_units"]


def build_conversion_metrics(start: date, end: date) -> int:
    """Recompute ConversionMetric for every active app and every day in [start, end] from the
    FunnelDay and SalesDay rollups: two grouped queries, one upsert."""
    window = {"app__is_active": True, "date__range": (start, end)}
    merged: dict[tuple[int, date], dict[str, int]] = {}
    for r in (
        FunnelDay.objects.filter(**window)
        .values("app_id", "date")
        .annotate(**{f: Sum(f) for f in FUNNEL_FIELDS})
    ):
        merged[(r.pop("app_id"), r.pop("date"))] = r
    for r in (
        SalesDay.objects.filter(**window)
        .values("app_id", "date")
        .annotate(dl=Sum("downloads"), iap=Sum("iap_units"))
    ):
        merged.setdefault((r["app_id"], r["date"]), {}).update(units=r["dl"], iap_units=r["iap"])
    rows = [
        ConversionMetric(app_id=app_id, date=day, **{f: values.get(f) or 0 for f in FIELDS})
        for (app_id, day), values in merged.items()
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
