"""Competitors: someone else's apps, followed next to yours from their public listing.

They ride in the same iTunes Lookup requests as your own apps' rating snapshot (``Rider``,
registered with ``stats.ratings.snapshot``), so following one costs no extra requests a day;
adding one asks Lookup once for its listing.

Each day's listing is compared with the one before, and every field that changed (name,
description, screenshots, price and the rest) is kept as a ``ListingChange``. Lookup leaves
out the subtitle, so ``read_subtitles`` reads it from the store's web page: once for every
competitor, again whenever it ships a version (a subtitle only changes with one), and once a
month in case.
Everything compared here is limited to the storefronts your apps are snapshotted in, so your
app and its competitors are always counted over the same storefronts.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx
from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from ascstats import store
from ascstats.stats.charts import days as _days
from ascstats.stats.charts import fmt_day, month_ticks, nice_axis
from ascstats.stats.models import IOS, App, AppVersion, RatingSnapshot
from ascstats.stats.rating_trends import countries_of, grade, snapshot_points

from . import synthetic
from .models import Competitor, CompetitorRelease, CompetitorSnapshot, ListingChange

log = logging.getLogger(__name__)

MAX_PER_APP = 100  # a whole market: one Lookup request per storefront carries 100
CHARTED = 4  # competitors in the charts, one color each: those with the most new ratings
RECENT = 30  # days of "new ratings" in the table
WEEKS = 12  # weeks in the new-ratings chart
TIMELINE = 182  # days in the release timeline
CHANGES_DAYS = 182  # days of listing changes on the Competitors tab
SUBTITLE_PAGES = 60  # store pages re-read a day for subtitles; a first read never waits
SUBTITLE_EVERY = 30  # days before a subtitle is read again with no new version
FAILURES_TO_STOP = 2  # pages failing in a row before the subtitle run stops for the day
# Your app is the first color; competitors take the rest in turn
COLORS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)",
          "var(--chart-5)", "var(--other)"]  # fmt: skip

STORE_LINK = re.compile(r"apps\.apple\.com/(?:([a-z]{2})/)?app/(?:[^/?#]+/)?id(\d+)", re.I)
BARE_ID = re.compile(r"(?:id)?(\d{6,12})", re.I)


class CompetitorError(Exception):
    """Something a person adding a competitor can fix; the message says what."""


def parse(text: str) -> tuple[int, str]:
    """(Apple ID, storefront) from an App Store link or a bare ID (US store)."""
    text = text.strip()
    if m := STORE_LINK.search(text):
        return int(m[2]), (m[1] or "us").lower()
    if m := BARE_ID.fullmatch(text):
        return int(m[1]), "us"
    raise CompetitorError("Paste an App Store link (apps.apple.com/…/id123…) or an app's ID.")


def lookup_listing(
    apple_id: int, storefront: str, transport: httpx.BaseTransport | None = None
) -> dict:
    """One app's Lookup result in ``storefront``; CompetitorError when the store has none."""
    if settings.ASC_SOURCE == "synthetic":
        try:
            rival = synthetic.rival_by_apple_id(apple_id)
        except KeyError:
            raise CompetitorError(f"No synthetic competitor has the ID {apple_id}.") from None
        return synthetic.rival_listing(rival, timezone.localdate())
    with store.client(transport) as client:
        try:
            results = store.lookup([apple_id], storefront, client)
        except store.StoreError as error:
            raise CompetitorError(f"The App Store did not answer ({error}); try again.") from None
    found = next((r for r in results if r.get("trackId") == apple_id), None)
    if found is None:
        raise CompetitorError(f"No app with the ID {apple_id} in the {storefront.upper()} store.")
    return found


def add(
    app: App, text: str, transport: httpx.BaseTransport | None = None, listing: dict | None = None
) -> Competitor:
    """Follow the app ``text`` names as a competitor of ``app``. A competitor another app
    already follows is shared, history and all. ``listing`` is its Lookup result when the
    caller has it already."""
    apple_id, storefront = parse(text)
    if App.objects.filter(apple_id=apple_id).exists():
        raise CompetitorError("That is one of your own apps.")
    if app.competitors.filter(apple_id=apple_id).exists():
        raise CompetitorError(f"{app.name} already follows that app.")
    if app.competitors.count() >= MAX_PER_APP:
        raise CompetitorError(f"{app.name} already follows {MAX_PER_APP} competitors; remove one.")
    competitor = Competitor.objects.filter(apple_id=apple_id).first()
    if competitor is None:
        listing = listing or lookup_listing(apple_id, storefront, transport)
        competitor = Competitor.objects.create(
            apple_id=apple_id, name=listing.get("trackName") or str(apple_id)
        )
        store_listing(competitor, listing, timezone.localdate(), storefront)
    competitor.tracked_for.add(app)
    if competitor.subtitle_read_on is None or competitor.listing_seen_on is None:
        from . import jobs

        jobs.read_competitor.enqueue(competitor.pk)  # its whole listing now, not with the daily run
    return competitor


def add_many(
    app: App, texts: list[str], transport: httpx.BaseTransport | None = None
) -> tuple[list[Competitor], list[str]]:
    """``add`` for many at once, asking Lookup for the new ones in batches (``LOOKUP_IDS`` a
    request) rather than one by one. Returns those followed and a message for each refused."""
    added, refused, wanted = [], [], []
    for text in texts:
        try:
            wanted.append((text, *parse(text)))
        except CompetitorError as error:
            refused.append(f"{text}: {error}")
    known = {*Competitor.objects.values_list("apple_id", flat=True),
             *App.objects.values_list("apple_id", flat=True)}  # fmt: skip
    new: dict[str, list[int]] = defaultdict(list)
    for _, apple_id, front in wanted:
        if apple_id not in known:
            new[front].append(apple_id)
    listings: dict[int, dict] = {}
    if new and settings.ASC_SOURCE != "synthetic":
        with store.client(transport) as client:
            for front, ids in new.items():
                try:
                    found = store.lookup_all(ids, front, client, store.PAUSE)
                except store.StoreError:
                    continue  # each asks on its own below
                listings |= {r["trackId"]: r for r in found if r.get("trackId") in ids}
    for text, apple_id, _ in wanted:
        try:
            added.append(add(app, text, transport, listings.get(apple_id)))
        except CompetitorError as error:
            refused.append(f"{text}: {error}")
    return added, refused


def remove(app: App, competitor: Competitor) -> None:
    """Stop following ``competitor`` for ``app``. Its history is kept, so following it again
    picks up where it left off; one no app follows is no longer snapshotted."""
    competitor.tracked_for.remove(app)


# --- the daily snapshot ---------------------------------------------------------------------


def _day(value: str | None) -> date | None:
    return datetime.fromisoformat(value).date() if value else None


def _shot(url: str) -> str:
    """A screenshot's asset, without the size the URL asks for."""
    return url.rsplit("/", 1)[0]


# Listing fields compared day to day, and how a value is read from a Lookup result
WATCHED = {
    "name": lambda r: r.get("trackName") or "",
    "seller": lambda r: r.get("sellerName") or r.get("artistName") or "",
    "primary_genre": lambda r: r.get("primaryGenreName") or "",
    "price": lambda r: r.get("formattedPrice") or "",
    "description": lambda r: (r.get("description") or "").strip(),
    "screenshots": lambda r: list(r.get("screenshotUrls") or []),
    "languages": lambda r: sorted(r.get("languageCodesISO2A") or []),
}


def _same(field_name: str, old, new) -> bool:
    if field_name == "screenshots":
        return [_shot(u) for u in old] == [_shot(u) for u in new]
    return old == new


def _record(
    competitor: Competitor, day: date, field_name: str, before, after, storefront: str
) -> None:
    """Keep one change; a second change of the field the same day replaces the first's
    "after" and keeps its "before"."""
    found = ListingChange.objects.filter(competitor=competitor, date=day, field=field_name).first()
    if found:
        found.after, found.version = after, competitor.version
        if _same(field_name, found.before, after):
            found.delete()  # changed and changed back within the day
        else:
            found.save(update_fields=["after", "version"])
        return
    ListingChange.objects.create(
        competitor=competitor, date=day, field=field_name, before=before, after=after,
        storefront=storefront, version=competitor.version,
    )  # fmt: skip


def store_listing(competitor: Competitor, r: dict, day: date, storefront: str = "us") -> None:
    """Copy a Lookup result onto a competitor, note each watched field that changed since the
    last full listing from the same storefront, and record its version the first time one is
    seen, with Apple's release date and notes. A field the result leaves out keeps its value
    (a search result carries only the name, seller and icon)."""
    full = "description" in r  # a whole Lookup listing, not a search result's few fields
    found = {
        "name": r.get("trackName") or "",
        "seller": r.get("sellerName") or r.get("artistName") or "",
        "icon_url": r.get("artworkUrl512") or r.get("artworkUrl100") or "",
        "primary_genre": r.get("primaryGenreName") or "",
        "released_on": _day(r.get("releaseDate")),
        "price": r.get("formattedPrice") or "",
        "version": r.get("version") or "",
        "version_released_on": _day(r.get("currentVersionReleaseDate")),
    }
    if full:
        found |= {f: WATCHED[f](r) for f in ("description", "screenshots", "languages")}
    # Compare only like with like: two full listings from one storefront (a second look the
    # same day, after a failed run, folds into that day's change)
    compare = (
        full
        and competitor.listing_seen_on is not None
        and competitor.listing_seen_on <= day
        and competitor.listing_storefront == storefront
    )
    lists = ("screenshots", "languages")
    changed = [
        f for f, v in found.items()
        if (v or (full and f in lists)) and not _same(f, getattr(competitor, f), v)
    ]  # fmt: skip
    before = {f: getattr(competitor, f) for f in changed}
    for f in changed:
        setattr(competitor, f, found[f])
    if full:
        competitor.listing_seen_on, competitor.listing_storefront = day, storefront
        changed += ["listing_seen_on", "listing_storefront"]
    if changed:
        competitor.save(update_fields=changed)
    if compare:
        for f in WATCHED:
            if f in before and (before[f] or f in lists):
                _record(competitor, day, f, before[f], found[f], storefront)
    if found["version"]:
        CompetitorRelease.objects.get_or_create(
            competitor=competitor,
            version=found["version"],
            defaults={
                "released_on": found["version_released_on"],
                "notes": r.get("releaseNotes") or "",
                "first_seen": day,
            },
        )


def _due_for_subtitle(day: date) -> list[Competitor]:
    """Followed competitors whose subtitle is worth reading today: those that shipped a
    version since it was read, then those never read, then the month-old; none twice a day."""
    followed = Competitor.objects.filter(tracked_for__is_active=True).distinct()
    stale = day - timedelta(days=SUBTITLE_EVERY)
    out = []
    for c in followed.order_by("apple_id"):
        if c.subtitle_read_on == day:
            continue
        if c.subtitle_read_on is None:
            out.append((1, c))
        elif c.version and c.version != c.subtitle_version:
            out.append((0, c))
        elif c.subtitle_read_on <= stale:
            out.append((2, c))
    return [c for _, c in sorted(out, key=lambda o: (o[0], o[1].subtitle_read_on or day))]


def _keep_subtitle(competitor: Competitor, found: str, day: date, storefront: str) -> None:
    if (
        competitor.subtitle_read_on is not None
        and found != competitor.subtitle
        and storefront == competitor.listing_storefront
    ):
        _record(competitor, day, "subtitle", competitor.subtitle, found, storefront)
    competitor.subtitle, competitor.subtitle_read_on = found, day
    competitor.subtitle_version = competitor.version
    competitor.save(update_fields=["subtitle", "subtitle_read_on", "subtitle_version"])


def read_subtitles(
    day: date | None = None,
    source: str | None = None,
    transport: httpx.BaseTransport | None = None,
    pause: float = store.PAUSE,
    limit: int = SUBTITLE_PAGES,
) -> int:
    """Read competitors' subtitles from their store pages (a daily store step, after the
    keyword searches): every one never read, then up to ``limit`` re-reads; returns the pages
    read. Following a competitor reads it at once (``read_new``), so the never-read are only
    those whose first read failed."""
    day = day or timezone.localdate()
    source = source or settings.ASC_SOURCE
    due = _due_for_subtitle(day)
    first = [c for c in due if c.subtitle_read_on is None]
    due = first + [c for c in due if c.subtitle_read_on is not None][:limit]
    if source == "synthetic":
        for c in due:
            try:
                rival = synthetic.rival_by_apple_id(c.apple_id)
            except KeyError:
                continue
            _keep_subtitle(c, synthetic.rival_listing(rival, day)["subtitle"], day, "us")
        return len(due)
    read = failures = 0
    with store.client(transport) as client:
        for c in due:
            if read + failures and pause:
                time.sleep(pause)
            front = c.listing_storefront or "us"
            try:
                found = store.subtitle(c.apple_id, front, client)
            except store.StoreError as error:
                log.warning("subtitle: %s", error)
                failures += 1
                if failures >= FAILURES_TO_STOP:
                    log.error("subtitles stopped: %d pages failed in a row", failures)
                    break
                continue
            failures = 0
            read += 1
            if found is None:
                log.warning("subtitle: no subtitle found on %s's page", c.apple_id)
                continue
            _keep_subtitle(c, found, day, front)
    log.info("subtitles: %d pages read of %d due", read, len(due))
    return read


def read_new(
    competitor: Competitor,
    day: date | None = None,
    transport: httpx.BaseTransport | None = None,
    pause: float = store.PAUSE,
) -> str:
    """A just-followed competitor's whole listing at once (the ``read_competitor`` job): its
    Lookup listing (description, screenshots, languages and the rest), unless one was kept
    already, and its subtitle from the store page. A store error leaves the rest to the
    daily run."""
    day = day or timezone.localdate()
    front = competitor.listing_storefront or "us"
    if settings.ASC_SOURCE == "synthetic":
        try:
            listing = synthetic.rival_listing(synthetic.rival_by_apple_id(competitor.apple_id), day)
        except KeyError:
            return "not synthetic"
        if competitor.listing_seen_on is None:
            store_listing(competitor, listing, day, front)
        if competitor.subtitle_read_on is None:
            _keep_subtitle(competitor, listing["subtitle"], day, front)
        return "read"
    with store.client(transport) as client:
        try:
            if competitor.listing_seen_on is None:
                found = store.lookup([competitor.apple_id], front, client)
                r = next((x for x in found if x.get("trackId") == competitor.apple_id), None)
                if r is not None:
                    store_listing(competitor, r, day, front)
                if pause:
                    time.sleep(pause)
            if competitor.subtitle_read_on is None:
                subtitle = store.subtitle(competitor.apple_id, front, client)
                if subtitle is not None:
                    _keep_subtitle(competitor, subtitle, day, front)
        except store.StoreError as error:
            log.warning("reading %s: %s", competitor.apple_id, error)
            return "store error"
    if pause:
        time.sleep(pause)  # jobs run back to back: keep the store's pace between them
    return "read"


def seed_subtitles(day: date) -> None:
    read_subtitles(day, source="synthetic")


class Rider:
    """Competitors in the ratings snapshot: the IDs it adds to each storefront's request, and
    what it does with their answers (``stats.extensions.SnapshotRider``)."""

    def __init__(self) -> None:
        self.rivals = {
            c.apple_id: c for c in Competitor.objects.filter(tracked_for__is_active=True).distinct()
        }
        self.listings: dict[int, dict] = {}  # the US one when there is one
        self.fronts: dict[int, str] = {}  # the storefront each listing is from

    def ids(self) -> list[int]:
        return list(self.rivals)

    def save(self, day: date, storefront: str, answers: dict[int, dict]) -> int:
        rows = []
        for apple_id, r in answers.items():
            if storefront == "us" or apple_id not in self.listings:
                self.listings[apple_id], self.fronts[apple_id] = r, storefront
            count = r.get("userRatingCount") or 0
            rows.append(CompetitorSnapshot(
                competitor=self.rivals[apple_id], date=day, storefront=storefront, count=count,
                star_sum=round(count * (r.get("averageUserRating") or 0)),
                version=r.get("version") or "",
            ))  # fmt: skip
        CompetitorSnapshot.objects.filter(
            date=day, storefront=storefront, competitor__in=self.rivals.values()
        ).delete()
        CompetitorSnapshot.objects.bulk_create(rows)
        return len(rows)

    def finish(self, day: date) -> None:
        for apple_id, listing in self.listings.items():
            store_listing(self.rivals[apple_id], listing, day, self.fronts.get(apple_id, "us"))

    def synthetic(self, day: date) -> int:
        rows = []
        for apple_id, rival in self.rivals.items():
            try:
                syn = synthetic.rival_by_apple_id(apple_id)
            except KeyError:
                continue
            self.listings[apple_id] = listing = synthetic.rival_listing(syn, day)
            self.fronts[apple_id] = "us"
            for front, (count, stars) in synthetic.rival_rating_snapshot(syn, day).items():
                rows.append(CompetitorSnapshot(
                    competitor=rival, date=day, storefront=front, count=count, star_sum=stars,
                    version=listing["version"],
                ))  # fmt: skip
        CompetitorSnapshot.objects.filter(date=day, competitor__in=self.rivals.values()).delete()
        CompetitorSnapshot.objects.bulk_create(rows)
        self.finish(day)
        return len(rows)


def seed_synthetic() -> None:
    """The synthetic rivals, each followed for the synthetic apps it competes with."""
    for r in synthetic.RIVALS:
        rival, _ = Competitor.objects.update_or_create(
            apple_id=r.apple_id, defaults={"name": r.name, "seller": r.seller}
        )
        rival.tracked_for.add(*App.objects.filter(apple_id__in=r.rival_of))


# --- the Competitors tab --------------------------------------------------------------------


@dataclass
class Member:
    """Your app or one competitor, as one row of the comparison."""

    name: str
    apple_id: int
    icon_url: str
    seller: str
    color: str
    you: bool
    competitor: Competitor | None = None
    count: int | None = None  # ratings across the snapshotted storefronts, latest
    average: float | None = None
    new: int | None = None  # ratings given in the last RECENT days
    new_average: float | None = None
    version: str = ""
    version_on: date | None = None
    price: str = ""
    subtitle: str = ""
    weekly: list[int | None] = field(default_factory=list)
    releases: list[tuple[str, date, str]] = field(default_factory=list)  # (version, day, notes)

    @property
    def store_url(self) -> str:
        if self.competitor is not None:
            return self.competitor.store_url
        return f"https://apps.apple.com/us/app/id{self.apple_id}"

    @property
    def price_value(self) -> float | None:
        """The price as a number to sort by: "Free" is 0, "$4.99" is 4.99."""
        return price_value(self.price)

    @property
    def grade(self) -> str:
        return grade(self.average)

    @property
    def new_grade(self) -> str:
        return grade(self.new_average)

    @property
    def per_day(self) -> float | None:
        return self.new / RECENT if self.new is not None else None


def price_value(price: str) -> float | None:
    """A listing's price as a number to sort by: 0 for "Free" (in any language), 4.99 for
    "$4.99" or "4,99 €"; None when there is none."""
    if not price:
        return None
    digits = re.sub(r"[^\d.,]", "", price)
    if not re.search(r"\d", digits):
        return 0.0
    if m := re.fullmatch(r"(.*?)[.,](\d{1,2})", digits):
        whole = re.sub(r"\D", "", m[1]) or "0"
        return float(f"{whole}.{m[2]}")
    return float(re.sub(r"\D", "", digits))


def _ratings(member: Member, snaps, end: date) -> None:
    totals = countries_of(snaps)
    if totals is None:
        return
    member.count, member.average = totals["count"], totals["average"]
    points = snapshot_points(snaps)
    recent = [p for p in points if p.day > end - timedelta(days=RECENT) and p.new is not None]
    if recent:
        member.new = sum(p.new for p in recent)
        stars = sum(p.new_stars for p in recent)
        member.new_average = stars / member.new if member.new > 0 else None
    for w in range(WEEKS):
        hi = end - timedelta(days=7 * (WEEKS - 1 - w))
        lo = hi - timedelta(days=6)
        week = [p.new for p in points if lo <= p.day <= hi and p.new is not None]
        member.weekly.append(sum(week) if week else None)


def compare(app: App) -> dict:
    """Your app next to each competitor it follows: ratings, recent ratings, current version,
    weekly new ratings and releases over the last half year."""
    rivals = list(app.competitors.all())
    end = max(
        filter(None, [
            RatingSnapshot.objects.filter(app=app).aggregate(d=Max("date"))["d"],
            CompetitorSnapshot.objects.filter(competitor__in=rivals).aggregate(d=Max("date"))["d"],
        ]),
        default=None,
    )  # fmt: skip
    you = Member(app.name, app.apple_id, app.icon_url, "You", COLORS[0], True)
    latest = (
        AppVersion.objects.filter(app=app, platform=IOS, first_seen__isnull=False)
        .order_by("-first_seen")
        .first()
    )
    if latest:
        you.version, you.version_on = latest.version, latest.first_seen
    members = [you]
    for c in rivals:
        members.append(Member(
            c.name, c.apple_id, c.icon_url, c.seller, COLORS[-1], False,
            competitor=c, version=c.version, version_on=c.version_released_on, price=c.price,
            subtitle=c.subtitle,
        ))  # fmt: skip
    if end is None:
        return {"members": members, "end": None, "max": MAX_PER_APP, "charted": members,
                "changes": [], "new_label": f"New, {RECENT} days"}  # fmt: skip

    _ratings(you, RatingSnapshot.objects.filter(app=app), end)
    for m in members[1:]:
        _ratings(m, CompetitorSnapshot.objects.filter(competitor=m.competitor), end)
    # Busiest first: new ratings lately, then ratings in all; the top few are charted
    members[1:] = sorted(members[1:], key=lambda m: (-(m.new or 0), -(m.count or 0), m.name))
    charted = [you, *[m for m in members[1:] if m.count is not None][:CHARTED]]
    for i, m in enumerate(charted[1:], start=1):
        m.color = COLORS[i]

    since = end - timedelta(days=TIMELINE - 1)
    you.releases = [
        (v.version, v.first_seen, "")
        for v in AppVersion.objects.filter(app=app, platform=IOS, first_seen__gte=since)
    ]
    for m in charted[1:]:
        m.releases = [
            (r.version, r.released_on or r.first_seen, r.notes)
            for r in CompetitorRelease.objects.filter(competitor=m.competitor)
            if (r.released_on or r.first_seen) >= since
        ]
    return {
        "members": members,
        "end": end,
        "max": MAX_PER_APP,
        "charted": charted,
        "weekly": _weekly_chart(charted, end),
        "timeline": _timeline(charted, since, end),
        "recent": RECENT,
        "new_label": f"New, {RECENT} days",
        "weeks": WEEKS,
        "changes": listing_changes(rivals, end),
        "changes_days": CHANGES_DAYS,
    }


W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 220, 44, 12, 12, 22


def _weekly_chart(members: list[Member], end: date) -> dict:
    """New ratings per week, one line per app, on one shared axis."""
    peak = max([v or 0 for m in members for v in m.weekly] or [0])
    top, ticks = nice_axis(peak or 1, 4)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    base = PAD_T + plot_h

    def x(i: int) -> float:
        return round(PAD_L + plot_w * i / (WEEKS - 1), 1)

    def y(v: float) -> float:
        return round(base - plot_h * v / top, 1)

    starts = [end - timedelta(days=7 * (WEEKS - 1 - w) + 6) for w in range(WEEKS)]
    series = []
    for m in members:
        if not any(v is not None for v in m.weekly):
            continue
        pts = [(x(i), y(v), v, starts[i]) for i, v in enumerate(m.weekly) if v is not None]
        series.append({
            "member": m,
            "line": " ".join(f"{px},{py}" for px, py, _, _ in pts),
            "dots": [
                {"x": px, "y": py,
                 "tip": f"{m.name}\nWeek of {fmt_day(start)}: {v:,} new rating{'s' * (v != 1)}"}
                for px, py, v, start in pts
            ],
        })  # fmt: skip
    return {
        "series": series,
        "grid": [{"y": y(v), "label": f"{v:,.0f}"} for v in ticks],
        "xticks": [
            {"x": x(i), "label": f"{s:%b} {s.day}"} for i, s in enumerate(starts) if i % 2 == 0
        ],
        "width": W, "height": H, "pad_l": PAD_L, "plot_r": W - PAD_R,
    }  # fmt: skip


LANE_H, LANE_X0 = 34, 220


def _timeline(members: list[Member], since: date, end: date) -> dict:
    """A lane per app with a dot per release over the last ``TIMELINE`` days."""
    span = max((end - since).days, 1)
    plot_w = W - LANE_X0 - PAD_R

    def x(day: date) -> float:
        return round(LANE_X0 + plot_w * (day - since).days / span, 1)

    lanes = []
    for i, m in enumerate(members):
        y = 10 + i * LANE_H + LANE_H / 2
        dots = []
        for version, day, notes in m.releases:
            note = (notes[:157] + "…") if len(notes) > 160 else notes
            dots.append({
                "x": x(day), "label": version,
                "tip": f"{m.name} {version}\n{fmt_day(day)}" + (f"\n{note}" if note else ""),
            })  # fmt: skip
        lanes.append({"member": m, "y": y, "dots": dots})
    every = _days(since, end)
    return {
        "lanes": lanes,
        "height": 10 + len(members) * LANE_H + 24,
        "x0": LANE_X0,
        "x1": W - PAD_R,
        "width": W,
        "ticks": month_ticks(every, lambda i: x(every[i]), 50),
    }


# --- listing changes ------------------------------------------------------------------------

CONTEXT = 8  # unchanged words kept on each side of a change in a long text
_TOKENS = re.compile(r"\n+|[^\s]+")


def word_diff(before: str, after: str, context: int = CONTEXT) -> list[dict]:
    """``before`` -> ``after`` as runs of words: kept ("eq"), added ("ins"), removed ("del"),
    and "gap" where a long unchanged stretch is cut to ``context`` words each side. A line
    break is its own token, "\n"."""
    old = [t if t[0] != "\n" else "\n" for t in _TOKENS.findall(before or "")]
    new = [t if t[0] != "\n" else "\n" for t in _TOKENS.findall(after or "")]
    runs: list[dict] = []
    ops = difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes()
    for n, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            words = old[i1:i2]
            head = context if n else 0  # nothing before the first change needs a lead-in
            tail = context if n < len(ops) - 1 else 0
            if len(words) > head + tail + 2:
                if head:
                    runs.append({"op": "eq", "words": words[:head]})
                runs.append({"op": "gap", "words": []})
                if tail:
                    runs.append({"op": "eq", "words": words[-tail:]})
            else:
                runs.append({"op": "eq", "words": words})
            continue
        if i2 > i1:
            runs.append({"op": "del", "words": old[i1:i2]})
        if j2 > j1:
            runs.append({"op": "ins", "words": new[j1:j2]})
    for r in runs:
        r["text"] = " ".join(r["words"]).replace(" \n ", "\n").replace("\n ", "\n")
    return runs


# Words that join a phrase but are never searched for: left out of "new words". "no" stays,
# since "no ads" is a search.
JOINERS = {"a", "an", "and", "by", "for", "in", "of", "on", "or", "the", "to", "vs", "with",
           "&", "+", "-"}  # fmt: skip


def _words(text: str) -> set[str]:
    return set(re.findall(r"[\w'&+-]+", (text or "").lower())) - JOINERS


def describe(change: ListingChange) -> dict:
    """One change, ready to show: a word diff for text, both sets for screenshots, the codes
    added and removed for languages; and for the name and subtitle, the words that are new
    or gone, since those are the ones a search reads."""
    out = {"field": change.field, "label": change.get_field_display(), "change": change}
    if change.field == ListingChange.Field.SCREENSHOTS:
        return out | {"kind": "shots", "before": change.before[:6], "after": change.after[:6],
                      "count": (len(change.before), len(change.after))}  # fmt: skip
    if change.field == ListingChange.Field.LANGUAGES:
        old, new = set(change.before), set(change.after)
        return out | {"kind": "list", "added": sorted(new - old), "removed": sorted(old - new)}
    out |= {"kind": "text", "runs": word_diff(change.before, change.after)}
    if change.field in (ListingChange.Field.NAME, ListingChange.Field.SUBTITLE):
        old, new = _words(change.before), _words(change.after)
        out |= {"gained": sorted(new - old), "dropped": sorted(old - new)}
    if change.field == ListingChange.Field.DESCRIPTION:
        count = {op: sum(len(r["words"]) for r in out["runs"] if r["op"] == op)
                 for op in ("ins", "del")}  # fmt: skip
        out |= {"long": True, "added": count["ins"], "removed": count["del"]}
    return out


ORDER = [f.value for f in ListingChange.Field]


def listing_changes(rivals: list[Competitor], end: date, days: int = CHANGES_DAYS) -> list[dict]:
    """The listing changes of ``rivals`` over the ``days`` to ``end``, newest first, one entry
    per competitor and day."""
    since = end - timedelta(days=days - 1)
    return _grouped(
        ListingChange.objects.filter(competitor__in=rivals, date__gte=since, date__lte=end)
    )


def _grouped(found) -> list[dict]:
    groups: dict[tuple[date, int], dict] = {}
    for change in found.select_related("competitor"):
        key = (change.date, change.competitor_id)
        group = groups.setdefault(key, {
            "date": change.date, "competitor": change.competitor, "version": change.version,
            "items": [],
        })  # fmt: skip
        group["items"].append(describe(change))
    for g in groups.values():
        g["items"].sort(key=lambda i: ORDER.index(i["field"]))
    return sorted(groups.values(), key=lambda g: (g["date"], g["competitor"].name), reverse=True)


# --- one competitor's page ------------------------------------------------------------------


def history(competitor: Competitor) -> list[dict]:
    """Everything recorded about ``competitor``, newest first: each version it shipped (dated
    by Apple's release date, with its notes) and each day its listing changed."""
    events = [
        {"kind": "changes", "date": g["date"], "version": g["version"], "items": g["items"]}
        for g in _grouped(ListingChange.objects.filter(competitor=competitor))
    ]
    for r in CompetitorRelease.objects.filter(competitor=competitor):
        events.append({
            "kind": "release", "date": r.released_on or r.first_seen, "version": r.version,
            "notes": r.notes.strip(), "seen": r.first_seen,
        })  # fmt: skip
    # A release before the change it brought, on the same day
    return sorted(events, key=lambda e: (e["date"], e["kind"] == "changes"), reverse=True)


def profile(app: App, competitor: Competitor) -> dict:
    """One competitor of ``app``: its listing and ratings, the searches it comes up in, and
    the history of its versions and listing changes."""
    from . import rival_keywords

    member = Member(
        competitor.name, competitor.apple_id, competitor.icon_url, competitor.seller,
        COLORS[-1], False, competitor=competitor, version=competitor.version,
        version_on=competitor.version_released_on, price=competitor.price,
        subtitle=competitor.subtitle,
    )  # fmt: skip
    snaps = CompetitorSnapshot.objects.filter(competitor=competitor)
    end = snaps.aggregate(d=Max("date"))["d"]
    if end is not None:
        _ratings(member, snaps, end)
    events = history(competitor)
    seen = [e.get("seen") or e["date"] for e in events]
    return {
        "since": min([competitor.created_at.date(), *seen]),
        "rival": competitor,
        "member": member,
        "recent": RECENT,
        "history": events,
        "releases": sum(e["kind"] == "release" for e in events),
        "changed_days": sum(e["kind"] == "changes" for e in events),
        "ranks": rival_keywords.ranks(app, competitor),
    }
