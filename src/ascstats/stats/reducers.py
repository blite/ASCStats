"""Map-reduce from parsed report lines to the aggregate tables the app reads.

Each report has an emitter: a small function that maps one parsed line to zero or more
``(table, key, counts)`` triples. ``reduce`` sums the counts per ``(table, key)`` in memory, and
the pipeline's load step writes the sums. The raw report files stay unchanged in the raw store,
so a question that needs a dimension not kept here means a new emitter plus a replay of the
load step over the raw files, never a wider table.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from decimal import Decimal

from .models import (
    FunnelDay,
    PurchaseDay,
    SalesDay,
    UsageDay,
    UtcDay,
    platform_of,
    sales_kind,
)
from .parsing import (
    CRASHES_REPORT,
    DISCOVERY_REPORT,
    DOWNLOADS_REPORT,
    PURCHASES_REPORT,
    SESSIONS_REPORT,
    AnalyticsLine,
    SalesLine,
)

Emitted = tuple[type, tuple, dict[str, int]]
Reduced = dict[tuple[type, tuple], Counter]


def _discovery(ln: AnalyticsLine) -> Iterator[Emitted]:
    field = {"Impression": "impressions", "Page view": "page_views"}.get(ln.event)
    if field:
        counts = {field: ln.counts, f"unique_{field}": ln.unique_counts or 0}
        yield FunnelDay, (ln.date, ln.source_type), counts


# App Downloads report Download Type -> UtcDay field. Apple's Trends counts restores with
# redownloads and both kinds of update as updates.
UTC_DOWNLOADS = {
    "First-time download": "downloads",
    "Redownload": "redownloads",
    "Restore": "redownloads",
    "Manual update": "updates",
    "Auto-update": "updates",
}


def _downloads(ln: AnalyticsLine) -> Iterator[Emitted]:
    field = {"First-time download": "first_time_downloads", "Redownload": "redownloads"}.get(
        ln.event
    )
    if field:
        yield FunnelDay, (ln.date, ln.source_type), {field: ln.counts}
    if utc := UTC_DOWNLOADS.get(ln.event):
        yield UtcDay, (ln.date, platform_of(ln.device)), {utc: ln.counts}


def cents(value: str | None) -> int:
    """'2.10' (US dollars, as Apple writes them) -> 210."""
    return int((Decimal(value or "0") * 100).to_integral_value())


def _purchases(ln: AnalyticsLine) -> Iterator[Emitted]:
    money = {
        "proceeds_cents": cents(ln.dimensions.get("Proceeds in USD")),
        "sales_cents": cents(ln.dimensions.get("Sales in USD")),
    }
    platform = platform_of(ln.device)
    units = {"iap_units": ln.counts} if ln.event == "In-app purchase" else {}
    yield UtcDay, (ln.date, platform), units | money
    content = ln.dimensions.get("Content Name", "")[:255]
    key = (ln.date, platform, ln.event[:40], content, ln.territory)
    yield PurchaseDay, key, {"purchases": ln.counts} | money


def _usage_key(ln: AnalyticsLine) -> tuple:
    return ln.date, platform_of(ln.device), ln.dimensions.get("App Version", "")


def _crashes(ln: AnalyticsLine) -> Iterator[Emitted]:
    yield UsageDay, _usage_key(ln), {"crashes": ln.counts, "crash_devices": ln.unique_counts or 0}


def _sessions(ln: AnalyticsLine) -> Iterator[Emitted]:
    seconds = int(float(ln.dimensions.get("Total Session Duration") or 0))
    counts = {"sessions": ln.counts, "session_seconds": seconds}
    yield UsageDay, _usage_key(ln), counts | {"session_devices": ln.unique_counts or 0}


EMITTERS: dict[str, Callable[[AnalyticsLine], Iterator[Emitted]]] = {
    DISCOVERY_REPORT: _discovery,
    DOWNLOADS_REPORT: _downloads,
    CRASHES_REPORT: _crashes,
    SESSIONS_REPORT: _sessions,
    PURCHASES_REPORT: _purchases,
}


def _sales(ln: SalesLine) -> Iterator[Emitted]:
    yield (
        SalesDay,
        (ln.begin_date, platform_of(ln.device)),
        {sales_kind(ln.product_type, ln.parent_identifier): ln.units},
    )


def reduce(emitted: Iterable[Emitted]) -> Reduced:
    out: Reduced = defaultdict(Counter)
    for model, key, counts in emitted:
        out[(model, key)].update(counts)
    return out


def reduce_analytics(lines_by_report: dict[str, list[AnalyticsLine]]) -> Reduced:
    return reduce(e for name, lines in lines_by_report.items() for ln in lines
                  for e in EMITTERS[name](ln))  # fmt: skip


def reduce_sales(lines: Iterable[SalesLine]) -> Reduced:
    return reduce(e for ln in lines for e in _sales(ln))


# Each table's key fields, in emitter key order.
KEY_FIELDS = {
    FunnelDay: ("date", "source_type"),
    UsageDay: ("date", "platform", "version"),
    SalesDay: ("date", "platform"),
    UtcDay: ("date", "platform"),
    PurchaseDay: ("date", "platform", "purchase_type", "content", "territory"),
}


def rows(reduced: Reduced, app) -> dict[type, list]:
    """Model instances per table for one app, ready to bulk_create."""
    out = defaultdict(list)
    for (model, key), counts in reduced.items():
        keys = dict(zip(KEY_FIELDS[model], key, strict=True))
        out[model].append(model(app=app, **keys, **counts))
    return out


def totals(reduced: Reduced) -> dict[type, Counter]:
    """Summed counts per table: what the quality step compares with the database."""
    out = defaultdict(Counter)
    for (model, _key), counts in reduced.items():
        out[model].update(counts)
    return out


def rebuild_sales_days(**filters) -> int:
    """Rebuild SalesDay from stored SalesRow lines (``filters`` narrow SalesRow, e.g. app=).
    The load step keeps SalesDay current; this is for repairs and hand-built test data."""
    from .models import SalesRow

    sales = SalesRow.objects.filter(**filters)
    counts = defaultdict(Counter)
    for app_id, day, ptype, parent, device, units in sales.values_list(
        "app_id", "report_date", "product_type", "parent_identifier", "device", "units"
    ):
        counts[(app_id, day, platform_of(device))][sales_kind(ptype, parent)] += units
    keys = {(a, d) for a, d, _ in counts}
    for app_id, day in keys:
        SalesDay.objects.filter(app_id=app_id, date=day).delete()
    SalesDay.objects.bulk_create(
        SalesDay(app_id=a, date=d, platform=p, **c) for (a, d, p), c in counts.items()
    )
    return len(counts)
