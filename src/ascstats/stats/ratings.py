"""Ratings and reviews, and what each release did to them.

Three sources:

- **Star ratings**: App Store Connect's API has no rating totals, so they come from Apple's
  public iTunes Lookup (no key; the request carries only app IDs and a storefront). It only
  reports today's totals, so ``snapshot`` runs daily and history starts on the first run.
  The average is unrounded, so each snapshot's star sum is exact and the difference of two
  consecutive snapshots is the exact count and average of the ratings given in between.
- **Written reviews**: App Store Connect (GET only), filed under the version Apple says they
  were written for (``/appStoreVersions/{id}/customerReviews``).
- **Review summaries**: App Store Connect's AI-written summary per territory, when Apple has
  enough reviews to write one.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx
from django.conf import settings
from django.db import transaction
from django.db.models import Avg, Count, Max, Sum
from django.utils import timezone

from ascstats.asc.client import ASCClient, ReportNotReady

from . import synthetic
from .models import (
    ACTIVE_ROWS,
    DOWNLOAD_ROWS,
    IOS,
    UPDATE_ROWS,
    App,
    AppVersion,
    CustomerReview,
    RatingSnapshot,
    ReviewSummary,
    SalesRow,
    platform_rows,
)

log = logging.getLogger(__name__)

LOOKUP = "https://itunes.apple.com/lookup"
LOOKUP_PAUSE = 3.0  # seconds between storefronts: the public endpoint allows ~20 calls/min


# --- star-rating snapshots ---------------------------------------------------------------------


def storefronts(end: date) -> list[str]:
    """Every storefront with sales in the past year, plus the US."""
    codes = set(
        SalesRow.objects.filter(ACTIVE_ROWS, report_date__gt=end - timedelta(days=365))
        .values_list("country_code", flat=True)
        .distinct()
    )
    return sorted({c.lower() for c in codes if len(c) == 2} | {"us"})


def _lookup(ids: list[int], storefront: str, client: httpx.Client) -> list[dict]:
    for attempt in range(4):
        response = client.get(LOOKUP, params={"id": ",".join(map(str, ids)), "country": storefront})
        if response.status_code in (403, 429, 500, 502, 503) and attempt < 3:
            time.sleep(LOOKUP_PAUSE * 2 ** (attempt + 1))
            continue
        response.raise_for_status()
        return response.json().get("results", [])
    return []


def snapshot(
    day: date | None = None,
    source: str | None = None,
    transport: httpx.BaseTransport | None = None,
    pause: float = LOOKUP_PAUSE,
) -> int:
    """Record today's public rating totals for every active app. Idempotent per day."""
    day = day or timezone.localdate()
    source = source or settings.ASC_SOURCE
    apps = {a.apple_id: a for a in App.objects.filter(is_active=True)}
    rows = []
    if source == "synthetic":
        for apple_id, app in apps.items():
            try:
                syn = synthetic.app_by_apple_id(apple_id)
            except KeyError:
                continue
            version = synthetic.version_on(syn, day)[0]
            for front, (count, stars) in synthetic.rating_snapshot(syn, day).items():
                rows.append(RatingSnapshot(app=app, date=day, storefront=front, count=count,
                                           star_sum=stars, version=version))  # fmt: skip
    else:
        with httpx.Client(timeout=30, transport=transport) as client:
            for n, front in enumerate(storefronts(day)):
                if n and pause:
                    time.sleep(pause)
                for r in _lookup(list(apps), front, client):
                    app = apps.get(r.get("trackId"))
                    count = r.get("userRatingCount") or 0
                    if app is None:
                        continue
                    rows.append(RatingSnapshot(
                        app=app, date=day, storefront=front, count=count,
                        star_sum=round(count * (r.get("averageUserRating") or 0)),
                        version=r.get("version") or "",
                    ))  # fmt: skip
    with transaction.atomic():
        RatingSnapshot.objects.filter(date=day, app__in=apps.values()).delete()
        RatingSnapshot.objects.bulk_create(rows)
    return len(rows)


# --- written reviews and summaries -------------------------------------------------------------


def _midnight(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, datetime.min.time()))


def import_reviews(app: App, client: ASCClient | None, day: date | None = None) -> int:
    """Upsert the app's written reviews, each tagged with the version Apple files it under.
    Synthetic mode generates the reviews for ``day`` instead."""
    if client is None:
        syn = synthetic.app_by_apple_id(app.apple_id)
        found = synthetic.reviews(syn, day or timezone.localdate())
        for r in found:
            CustomerReview.objects.update_or_create(
                asc_id=r["id"],
                defaults={
                    "app": app, "rating": r["rating"], "title": r["title"], "body": r["body"],
                    "territory": r["territory"], "platform": IOS, "version": r["version"],
                    "created": _midnight(r["created"]),
                },
            )  # fmt: skip
        return len(found)
    try:
        reviews = client.customer_reviews(app.apple_id)
    except ReportNotReady:
        log.warning("app %s is not visible to this API key; no reviews", app)
        return 0
    version_of = {}
    for v in AppVersion.objects.filter(app=app).exclude(asc_id=""):
        for review_id in client.version_review_ids(v.asc_id):
            version_of[review_id] = (v.version, v.platform)
    with transaction.atomic():
        for r in reviews:
            attrs = r["attributes"]
            version, platform = version_of.get(r["id"], ("", ""))
            CustomerReview.objects.update_or_create(
                asc_id=r["id"],
                defaults={
                    "app": app, "rating": attrs["rating"], "title": attrs.get("title") or "",
                    "body": attrs.get("body") or "", "territory": attrs.get("territory") or "",
                    "created": datetime.fromisoformat(attrs["createdDate"]),
                    "version": version, "platform": platform,
                },
            )  # fmt: skip
    return len(reviews)


def import_summaries(app: App, client: ASCClient | None) -> int:
    if client is None:
        syn = synthetic.app_by_apple_id(app.apple_id)
        ReviewSummary.objects.update_or_create(
            app=app, platform=IOS, territory="USA",
            defaults={
                "locale": "en-US", "created": timezone.now(),
                "text": synthetic.review_summary(syn),
            },
        )  # fmt: skip
        return 1
    written = 0
    for platform in ("IOS", "MAC_OS"):
        try:
            found = client.review_summaries(app.apple_id, platform)
        except ReportNotReady:
            return written
        for s in found:
            attrs = s["attributes"]
            territory = (
                ((s.get("relationships") or {}).get("territory") or {}).get("data") or {}
            ).get("id", "")
            created = attrs.get("createdDate")
            ReviewSummary.objects.update_or_create(
                app=app, platform=platform, territory=territory,
                defaults={
                    "locale": attrs.get("locale") or "", "text": attrs.get("text") or "",
                    "created": datetime.fromisoformat(created) if created else None,
                },
            )  # fmt: skip
            written += 1
    return written


# --- metrics ---------------------------------------------------------------------------------


def daily_new_ratings(app: App) -> dict[date, tuple[int, int]]:
    """{day: (new ratings, their total stars)} from consecutive snapshots, per storefront and
    then summed, so a storefront appearing for the first time does not count as new ratings."""
    by_front = defaultdict(dict)
    for front, day, count, stars in RatingSnapshot.objects.filter(app=app).values_list(
        "storefront", "date", "count", "star_sum"
    ):
        by_front[front][day] = (count, stars)
    out: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    for days in by_front.values():
        for day, (count, stars) in days.items():
            prev = days.get(day - timedelta(days=1))
            if prev is not None:
                out[day][0] += count - prev[0]
                out[day][1] += stars - prev[1]
    return {d: (c, s) for d, (c, s) in out.items()}


@dataclass
class VersionRatings:
    version: str
    released_on: date
    days_observed: int  # days with two consecutive snapshots while this version was live
    new_ratings: int
    ratings_per_day: float | None
    new_average: float | None  # stars of only the ratings given while this version was live
    per_1k_active: float | None  # new ratings per 1,000 downloads + updates on those days
    reviews: int
    review_average: float | None


def version_ratings(app: App) -> list[VersionRatings]:
    """Rating velocity and quality per iOS version. Snapshots only start when ``snapshot``
    first runs, so older versions have written reviews but no rating velocity."""
    daily = daily_new_ratings(app)
    versions = list(
        AppVersion.objects.filter(app=app, platform=IOS, first_seen__isnull=False).order_by(
            "first_seen"
        )
    )
    if not versions:
        return []
    last = max([*daily, versions[-1].first_seen])
    rows = platform_rows(IOS)
    active = dict(
        SalesRow.objects.filter((DOWNLOAD_ROWS | UPDATE_ROWS) & rows, app=app)
        .values_list("report_date")
        .annotate(Sum("units"))
    )
    reviews = {
        r["version"]: r
        for r in CustomerReview.objects.filter(app=app)
        .exclude(platform="MAC_OS")
        .values("version")
        .annotate(n=Count("id"), avg=Avg("rating"))
    }
    out = []
    for i, v in enumerate(versions):
        until = versions[i + 1].first_seen - timedelta(days=1) if i + 1 < len(versions) else last
        days = [d for d in daily if v.first_seen <= d <= until]
        new = sum(daily[d][0] for d in days)
        stars = sum(daily[d][1] for d in days)
        act = sum(active.get(d, 0) for d in days)
        rv = reviews.get(v.version, {})
        out.append(
            VersionRatings(
                version=v.version,
                released_on=v.first_seen,
                days_observed=len(days),
                new_ratings=new,
                ratings_per_day=round(new / len(days), 2) if days else None,
                new_average=round(stars / new, 2) if new > 0 else None,
                per_1k_active=round(1000 * new / act, 1) if act and days else None,
                reviews=rv.get("n", 0),
                review_average=round(rv["avg"], 2) if rv.get("avg") is not None else None,
            )
        )
    return out


def current(app: App) -> dict | None:
    """Latest totals across storefronts, the last 7 days of new ratings, recent reviews and
    the best available summary (US first)."""
    latest = RatingSnapshot.objects.filter(app=app).aggregate(d=Max("date"))["d"]
    summary = (
        ReviewSummary.objects.filter(app=app, territory="USA").first()
        or ReviewSummary.objects.filter(app=app).first()
    )
    recent = list(CustomerReview.objects.filter(app=app)[:5])
    if latest is None and not recent and summary is None:
        return None
    out = {"date": latest, "summary": summary, "recent": recent}
    if latest:
        snaps = RatingSnapshot.objects.filter(app=app, date=latest)
        agg = snaps.aggregate(count=Sum("count"), stars=Sum("star_sum"))
        us = snaps.filter(storefront="us").first()
        daily = daily_new_ratings(app)
        week = [daily[d] for d in daily if d > latest - timedelta(days=7)]
        out |= {
            "count": agg["count"] or 0,
            "average": (agg["stars"] / agg["count"]) if agg["count"] else None,
            "us_count": us.count if us else None,
            "us_average": us.average if us else None,
            "week_new": sum(c for c, _ in week) if week else None,
            "week_days": len(week),
        }
    return out
