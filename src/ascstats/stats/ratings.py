"""Ratings and reviews, and what each release did to them.

Three sources:

- **Star ratings**: App Store Connect's API has no rating totals, so they come from Apple's
  public iTunes Lookup (no key; the request carries only app IDs and a storefront). It only
  reports today's totals, so ``snapshot`` runs daily and history starts on the first run.
  The average is unrounded, so each snapshot's star sum is exact and the difference of two
  consecutive snapshots is the exact count and average of the ratings given in between.
  One request covers every app in one storefront; quiet storefronts are only asked weekly
  (``due_storefronts``).
- **Written reviews**: App Store Connect (GET only), filed under the version Apple says they
  were written for (``/appStoreVersions/{id}/customerReviews``).
- **Review summaries**: App Store Connect's AI-written summary per territory, when Apple has
  enough reviews to write one.
"""

from __future__ import annotations

import logging
import time
import zlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx
from django.conf import settings
from django.db import transaction
from django.db.models import Avg, Count, Max, Min, Q
from django.utils import timezone

from ascstats import store
from ascstats.asc.client import ASCClient, ReportNotReady

from . import extensions, synthetic
from .models import (
    ACTIVE_ROWS,
    IOS,
    MAC_OS,
    App,
    AppVersion,
    CustomerReview,
    RatingSnapshot,
    ReviewSummary,
    SalesDay,
    SalesRow,
)

log = logging.getLogger(__name__)

MISSING_RETRIES = 2  # re-asks when an app answered in that storefront last week is missing
FAILURES_TO_STOP = 2  # storefronts failing in a row before the run stops for the day
ACTIVE_DAYS = 30  # a storefront whose ratings moved within this many days is asked daily
WEEK = 7


# --- star-rating snapshots ---------------------------------------------------------------------


def storefronts(end: date) -> list[str]:
    """Every storefront with sales in the past year, plus the US."""
    codes = set(
        SalesRow.objects.filter(ACTIVE_ROWS, report_date__gt=end - timedelta(days=365))
        .values_list("country_code", flat=True)
        .distinct()
    )
    return sorted({c.lower() for c in codes if len(c) == 2} | {"us"})


def due_storefronts(day: date, candidates: list[str]) -> list[str]:
    """The storefronts to ask on ``day``: the US every day, any storefront whose ratings moved
    in the last ``ACTIVE_DAYS``, and the rest once a week, each on a weekday of its own so the
    load stays even. A storefront with no snapshot in over a week (new, or a missed run) is
    asked at once."""
    moved, last = set(), {}
    for r in (
        RatingSnapshot.objects.filter(date__gte=day - timedelta(days=ACTIVE_DAYS), date__lt=day)
        .values("app", "storefront")
        .annotate(lo=Min("count"), hi=Max("count"), last=Max("date"))
    ):
        front = r["storefront"]
        if r["hi"] != r["lo"]:
            moved.add(front)
        last[front] = max(last.get(front, r["last"]), r["last"])
    return [
        front
        for front in candidates
        if front == "us"
        or front in moved
        or zlib.crc32(front.encode()) % WEEK == day.toordinal() % WEEK
        or front not in last
        or (day - last[front]).days > WEEK
    ]


def snapshot(
    day: date | None = None,
    source: str | None = None,
    transport: httpx.BaseTransport | None = None,
    pause: float = store.PAUSE,
    every_storefront: bool = False,
) -> int:
    """Record today's public rating totals for every active app in the storefronts due today,
    or in all of them with ``every_storefront``, and return the rows written. Registered riders
    (``extensions.snapshot_riders``: competitors) are asked in the same requests.

    Each storefront is saved as soon as it answers and one already saved today is not asked
    again, so a rerun after a failure only asks what is missing. Two storefronts failing in a
    row stop the run for the day rather than keep asking a server that is refusing."""
    day = day or timezone.localdate()
    source = source or settings.ASC_SOURCE
    apps = {a.apple_id: a for a in App.objects.active()}
    riders = [make() for make in extensions.snapshot_riders]
    listings: dict[int, dict] = {}  # apple_id -> Lookup result, the US one when there is one
    if source == "synthetic":
        rows = []
        for apple_id, app in apps.items():
            try:
                syn = synthetic.app_by_apple_id(apple_id)
            except KeyError:
                continue
            listings[apple_id] = synthetic.lookup_listing(syn)
            version = synthetic.version_on(syn, day)[0]
            for front, (count, stars) in synthetic.rating_snapshot(syn, day).items():
                rows.append(RatingSnapshot(app=app, date=day, storefront=front, count=count,
                                           star_sum=stars, version=version))  # fmt: skip
        with transaction.atomic():
            RatingSnapshot.objects.filter(date=day, app__in=apps.values()).delete()
            RatingSnapshot.objects.bulk_create(rows)
            for apple_id, listing in listings.items():
                store_listing(apps[apple_id], listing)
            return len(rows) + sum(rider.synthetic(day) for rider in riders)

    today = RatingSnapshot.objects.filter(date=day, app__in=apps.values())
    done = set() if every_storefront else set(today.values_list("storefront", flat=True))
    recent: dict[str, set[int]] = defaultdict(set)  # storefront -> apps it answered last week
    for front, app_id in (
        RatingSnapshot.objects.filter(
            app__in=apps.values(), date__gte=day - timedelta(days=WEEK), date__lt=day
        )
        .values_list("storefront", "app_id")
        .distinct()
    ):
        recent[front].add(app_id)
    candidates = storefronts(day)
    due = candidates if every_storefront else due_storefronts(day, candidates)
    written = failures = 0

    rider_ids = {apple_id: rider for rider in riders for apple_id in rider.ids()}

    def ask(front: str, client: httpx.Client) -> int:
        """Ask one storefront for your apps and the riders', again while one of your apps it
        answered for last week is missing (an empty answer is more often a hiccup than a
        delisting), and save what it said."""
        answers: dict[int, tuple[App, dict]] = {}
        ridden: dict[int, dict] = {}
        for attempt in range(MISSING_RETRIES + 1):
            if attempt and pause:
                time.sleep(pause)
            for r in store.lookup_all([*apps, *rider_ids], front, client, pause):
                if (app := apps.get(r.get("trackId"))) is not None:
                    answers[app.pk] = (app, r)
                elif r.get("trackId") in rider_ids:
                    ridden[r["trackId"]] = r
            if not recent[front] - answers.keys():
                break
        rows = []
        for app, r in answers.values():
            # the US listing, or the first one for an app the US store does not sell
            if front == "us" or (app.apple_id not in listings and app.pk not in recent["us"]):
                listings[app.apple_id] = r
            count = r.get("userRatingCount") or 0
            rows.append(RatingSnapshot(
                app=app, date=day, storefront=front, count=count,
                star_sum=round(count * (r.get("averageUserRating") or 0)),
                version=r.get("version") or "",
            ))  # fmt: skip
        with transaction.atomic():
            today.filter(storefront=front).delete()
            RatingSnapshot.objects.bulk_create(rows)
            return len(rows) + sum(
                rider.save(day, front, {i: r for i, r in ridden.items() if rider_ids[i] is rider})
                for rider in riders
            )

    asked = 0
    with store.client(transport) as client:
        # the US first: its listing is the one the app's icon and category come from
        for front in ["us", *(f for f in due if f != "us")]:
            if front in done:
                continue
            if asked + failures and pause:
                time.sleep(pause)
            try:
                written += ask(front, client)
                asked += 1
                failures = 0
            except store.StoreError as error:
                log.warning("rating snapshot: %s", error)
                failures += 1
                if failures >= FAILURES_TO_STOP:
                    log.error(
                        "rating snapshot stopped: %d storefronts failed in a row; the rest "
                        "wait for tomorrow's run", failures,
                    )  # fmt: skip
                    break
    with transaction.atomic():
        for apple_id, listing in listings.items():
            store_listing(apps[apple_id], listing)
        for rider in riders:
            rider.finish(day)
    log.info(
        "rating snapshots: asked %d of %d due storefronts (%d already saved today)",
        asked, len(due), len(done),
    )  # fmt: skip
    return written


def store_listing(app: App, r: dict) -> None:
    """Copy the release date, icon and category from a Lookup result onto the app. A field
    the result leaves out keeps its last value. The name is the caller's call: an imported
    app keeps the one App Store Connect gave it."""
    released = r.get("releaseDate")
    found = {
        "released_on": datetime.fromisoformat(released).date() if released else None,
        "icon_url": r.get("artworkUrl512") or r.get("artworkUrl100") or "",
        "primary_genre": r.get("primaryGenreName") or "",
    }
    changed = [f for f, v in found.items() if v and getattr(app, f) != v]
    for f in changed:
        setattr(app, f, found[f])
    if changed:
        app.save(update_fields=changed)


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
            replied = r["replied"]
            CustomerReview.objects.update_or_create(
                asc_id=r["id"],
                defaults={
                    "app": app, "rating": r["rating"], "title": r["title"], "body": r["body"],
                    "territory": r["territory"], "platform": IOS, "version": r["version"],
                    "created": _midnight(r["created"]), "reviewer_nickname": r["nickname"],
                    "response_body": r["reply"], "response_state": "PUBLISHED" if replied else "",
                    "responded_at": _midnight(replied) if replied else None,
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
            reply = (r.get("response") or {}).get("attributes") or {}
            replied = reply.get("lastModifiedDate")
            CustomerReview.objects.update_or_create(
                asc_id=r["id"],
                defaults={
                    "app": app, "rating": attrs["rating"], "title": attrs.get("title") or "",
                    "body": attrs.get("body") or "", "territory": attrs.get("territory") or "",
                    "created": datetime.fromisoformat(attrs["createdDate"]),
                    "version": version, "platform": platform,
                    "reviewer_nickname": attrs.get("reviewerNickname") or "",
                    "response_body": reply.get("responseBody") or "",
                    "responded_at": datetime.fromisoformat(replied) if replied else None,
                    "response_state": reply.get("state") or "",
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
    for platform in (IOS, MAC_OS):
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
    """{day: (new ratings, their total stars)}: each snapshot minus the storefront's previous
    one, summed over storefronts. A storefront asked weekly lands its week on the day it was
    asked; one appearing for the first time does not count as new ratings."""
    out: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    prev: dict[str, tuple[int, int]] = {}
    for front, day, count, stars in (
        RatingSnapshot.objects.filter(app=app)
        .order_by("date")
        .values_list("storefront", "date", "count", "star_sum")
    ):
        if front in prev:
            out[day][0] += count - prev[front][0]
            out[day][1] += stars - prev[front][1]
        prev[front] = (count, stars)
    return {d: (c, s) for d, (c, s) in out.items()}


def latest_totals(apps: list[App]) -> dict[int, tuple[int, int]]:
    """{app pk: (ratings, stars)} summed over each storefront's latest snapshot, since quiet
    storefronts are not asked every day. Looks back two weeks, so a storefront that stopped
    answering (the app left it) drops out."""
    latest = RatingSnapshot.objects.filter(app__in=apps).aggregate(d=Max("date"))["d"]
    if latest is None:
        return {}
    last: dict[tuple[int, str], tuple[int, int]] = {}
    for app_id, front, count, stars in (
        RatingSnapshot.objects.filter(app__in=apps, date__gt=latest - timedelta(days=2 * WEEK))
        .order_by("date")
        .values_list("app_id", "storefront", "count", "star_sum")
    ):
        last[app_id, front] = (count, stars)
    totals: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for (app_id, _), (count, stars) in last.items():
        totals[app_id][0] += count
        totals[app_id][1] += stars
    return {a: (c, s) for a, (c, s) in totals.items()}


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


def version_ratings(
    app: App, daily: dict[date, tuple[int, int]] | None = None
) -> list[VersionRatings]:
    """Rating velocity and quality per iOS version. Snapshots only start when ``snapshot``
    first runs, so older versions have written reviews but no rating velocity. Pass ``daily``
    (from ``daily_new_ratings``) to reuse it."""
    daily = daily_new_ratings(app) if daily is None else daily
    versions = list(
        AppVersion.objects.filter(app=app, platform=IOS, first_seen__isnull=False).order_by(
            "first_seen"
        )
    )
    if not versions:
        return []
    last = max([*daily, versions[-1].first_seen])
    active = {
        d: dl + up
        for d, dl, up in SalesDay.objects.filter(app=app, platform=IOS).values_list(
            "date", "downloads", "updates"
        )
    }
    reviews = {
        r["version"]: r
        for r in CustomerReview.objects.filter(app=app)
        .exclude(platform=MAC_OS)
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


def current(app: App, daily: dict[date, tuple[int, int]] | None = None) -> dict | None:
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
    unanswered = CustomerReview.objects.filter(app=app, response_body="").count()
    out = {"date": latest, "summary": summary, "recent": recent, "unanswered": unanswered}
    if latest:
        count, stars = latest_totals([app]).get(app.pk, (0, 0))
        us = RatingSnapshot.objects.filter(app=app, storefront="us").order_by("date").last()
        daily = daily_new_ratings(app) if daily is None else daily
        week = [daily[d] for d in daily if d > latest - timedelta(days=7)]
        out |= {
            "count": count,
            "average": stars / count if count else None,
            "us_count": us.count if us else None,
            "us_average": us.average if us else None,
            "week_new": sum(c for c, _ in week) if week else None,
            "week_days": len(week),
        }
    return out


@dataclass
class AppRatingChange:
    app: App
    count: int  # ratings across storefronts at the latest snapshot
    average: float
    new_ratings: int  # given in the window
    new_average: float | None  # stars of only those new ratings
    change: float | None  # overall average now minus before the window, in stars


def recent_activity(apps: list[App], end: date, days: int, limit: int = 8) -> dict:
    """What changed for the overview: new ratings per app over the last ``days`` days of
    snapshots (ending at the latest one), and the newest written reviews across apps since
    ``end`` minus ``days``. Apps without a snapshot are left out of the ratings."""
    latest = RatingSnapshot.objects.filter(app__in=apps).aggregate(d=Max("date"))["d"]
    changes, observed = [], 0
    if latest is not None:
        since = latest - timedelta(days=days)
        totals = latest_totals(apps)
        for app in apps:
            count, stars = totals.get(app.pk, (0, 0))
            if not count:
                continue
            window = [v for d, v in daily_new_ratings(app).items() if since < d <= latest]
            observed = max(observed, len(window))
            new, new_stars = sum(c for c, _ in window), sum(s for _, s in window)
            before = count - new
            changes.append(
                AppRatingChange(
                    app=app,
                    count=count,
                    average=stars / count,
                    new_ratings=new,
                    new_average=new_stars / new if new > 0 else None,
                    change=stars / count - (stars - new_stars) / before
                    if window and before > 0
                    else None,
                )
            )
        changes.sort(key=lambda c: (-c.new_ratings, -c.count))
    reviews = list(
        CustomerReview.objects.filter(
            app__in=apps, created__gte=_midnight(end - timedelta(days=days - 1))
        ).select_related("app")[:limit]
    )
    return {
        "latest": latest,
        "observed": observed,  # days of snapshot pairs in the window; 0 until the second snapshot
        "changes": changes,
        "new_total": sum(c.new_ratings for c in changes),
        "reviews": reviews,
    }


# --- the review queue ----------------------------------------------------------------------------

# Path segment -> label. "" (needs a reply) is the default, so the queue's own URL is the queue.
REVIEW_STATES = {"": "Needs reply", "replied": "Replied", "all": "All"}


def review_queue(state: str, app: App | None = None) -> dict:
    """Written reviews across active apps (or one), in one reply state, with the count in
    each state. Needs a reply is worked worst first: lowest rating, then newest; the other
    states read newest first. A reply Apple has not published yet counts as replied."""
    reviews = CustomerReview.objects.filter(app__is_active=True).select_related("app")
    if app is not None:
        reviews = reviews.filter(app=app)
    unanswered = Q(response_body="")
    counts = reviews.aggregate(
        needs=Count("id", filter=unanswered), replied=Count("id", filter=~unanswered)
    )
    counts = {"": counts["needs"], "replied": counts["replied"], "all": sum(counts.values())}
    if state == "":
        reviews = reviews.filter(unanswered).order_by("rating", "-created")
    elif state == "replied":
        reviews = reviews.exclude(unanswered).order_by("-created")
    else:
        reviews = reviews.order_by("-created")
    return {
        "reviews": list(reviews),
        "states": [(key, label, counts[key]) for key, label in REVIEW_STATES.items()],
    }
