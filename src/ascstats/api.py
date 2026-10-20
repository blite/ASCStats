"""Read-only JSON API (Django Ninja). OpenAPI docs at /api/docs."""

from datetime import date, datetime, timedelta

from django.db.models import F, Sum
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Schema

from ascstats.stats.models import DOWNLOAD_ROWS, App, ConversionMetric, IngestionRun, SalesRow
from ascstats.stats.ratings import current, version_ratings
from ascstats.stats.versions import version_impact

api = NinjaAPI(title="ASCStats", version="1", description="App Store Connect stats, read-only.")


class AppOut(Schema):
    apple_id: int
    name: str
    sku: str
    bundle_id: str
    is_active: bool


class DailySales(Schema):
    date: date
    units: int
    proceeds: dict[str, float]  # currency -> developer proceeds; no FX conversion here


class Conversion(Schema):
    date: date
    impressions: int
    page_views: int
    first_time_downloads: int
    units: int
    conversion_rate: float | None


class DeltaOut(Schema):
    metric: str
    before: float | None
    after: float | None
    change_pct: float | None
    t_score: float | None  # Welch t on daily values; |t| >= 2 is unlikely to be noise
    significant: bool


class VersionImpactOut(Schema):
    platform: str  # IOS or MAC_OS: each platform has its own versions and rows
    version: str
    released_on: date  # first day the version appears in sales data
    before_days: int
    after_days: int
    updates: int
    deltas: list[DeltaOut]


class VersionRatingsOut(Schema):
    version: str
    released_on: date
    days_observed: int  # days with consecutive rating snapshots while this version was live
    new_ratings: int
    ratings_per_day: float | None
    new_average: float | None  # stars of only the ratings given while this version was live
    per_1k_active: float | None  # new ratings per 1,000 downloads + updates
    reviews: int  # written reviews Apple files under this version
    review_average: float | None


class ReviewOut(Schema):
    rating: int
    title: str
    body: str
    territory: str
    created: datetime
    version: str


class RatingsOut(Schema):
    snapshot_date: date | None
    count: int | None  # all storefronts
    average: float | None
    us_count: int | None
    us_average: float | None
    new_last_7_days: int | None
    summary: str | None  # Apple's AI-written review summary (US first)
    versions: list[VersionRatingsOut]
    recent_reviews: list[ReviewOut]


class RunOut(Schema):
    report: str
    report_date: date
    step: str
    source: str
    status: str
    rows_loaded: int | None
    error: str


@api.get("/health", tags=["ops"])
def health(request):
    return {"ok": True}


@api.get("/apps", response=list[AppOut], tags=["apps"])
def list_apps(request, include_inactive: bool = False):
    """Active apps (the ones every stat covers); ``include_inactive`` lists the rest too."""
    return App.objects.all() if include_inactive else App.objects.filter(is_active=True)


def _range(start: date | None, end: date | None) -> tuple[date, date]:
    end = end or date.today()
    return start or end - timedelta(days=29), end


@api.get("/apps/{apple_id}/sales/daily", response=list[DailySales], tags=["sales"])
def daily_sales(request, apple_id: int, start: date | None = None, end: date | None = None):
    """Units and proceeds per day. Report proceeds are per unit: total = units x proceeds."""
    app = get_object_or_404(App, apple_id=apple_id, is_active=True)
    start, end = _range(start, end)
    rows = (
        SalesRow.objects.filter(app=app, report_date__range=(start, end))
        .values("report_date", "currency_of_proceeds")
        .annotate(total=Sum(F("units") * F("developer_proceeds")))
        .order_by("report_date")
    )
    days: dict[date, DailySales] = {}
    for r in rows:
        day = days.setdefault(
            r["report_date"], DailySales(date=r["report_date"], units=0, proceeds={})
        )
        day.proceeds[r["currency_of_proceeds"]] = round(float(r["total"]), 2)
    # downloads only: in-app purchases and updates are not
    for r in (
        SalesRow.objects.filter(DOWNLOAD_ROWS, app=app, report_date__range=(start, end))
        .values("report_date")
        .annotate(units=Sum("units"))
    ):
        days[r["report_date"]].units = r["units"]
    return list(days.values())


@api.get("/apps/{apple_id}/conversion", response=list[Conversion], tags=["analytics"])
def conversion(request, apple_id: int, start: date | None = None, end: date | None = None):
    app = get_object_or_404(App, apple_id=apple_id, is_active=True)
    start, end = _range(start, end)
    return ConversionMetric.objects.filter(app=app, date__range=(start, end)).order_by("date")


@api.get("/apps/{apple_id}/versions", response=list[VersionImpactOut], tags=["versions"])
def versions(request, apple_id: int, window: int = 14, platform: str | None = None):
    """Each version's first ``window`` days live against the ``window`` days before it, per
    platform (``IOS`` or ``MAC_OS``; both by default)."""
    app = get_object_or_404(App, apple_id=apple_id, is_active=True)
    return version_impact(app, window=max(3, min(window, 90)), platform=platform)


@api.get("/apps/{apple_id}/ratings", response=RatingsOut, tags=["versions"])
def ratings(request, apple_id: int):
    """Star ratings (daily public snapshots), written reviews and Apple's summary, per iOS
    version. Rating velocity exists only from the first snapshot on."""
    app = get_object_or_404(App, apple_id=apple_id, is_active=True)
    now = current(app) or {}
    return {
        "snapshot_date": now.get("date"),
        "count": now.get("count"),
        "average": now.get("average"),
        "us_count": now.get("us_count"),
        "us_average": now.get("us_average"),
        "new_last_7_days": now.get("week_new"),
        "summary": now["summary"].text if now.get("summary") else None,
        "versions": version_ratings(app),
        "recent_reviews": now.get("recent", []),
    }


@api.get("/runs", response=list[RunOut], tags=["ops"])
def runs(request, limit: int = 50):
    return IngestionRun.objects.all()[: min(limit, 500)]
