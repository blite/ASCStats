"""Competitors: someone else's apps, followed next to yours from their public listing.

They ride in the same iTunes Lookup requests as your own apps' rating snapshot (``Rider``,
registered with ``stats.ratings.snapshot``), so following one costs no extra requests a day;
adding one asks Lookup once for its listing.
Everything compared here is limited to the storefronts your apps are snapshotted in, so your
app and its competitors are always counted over the same storefronts.
"""

from __future__ import annotations

import re
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
from .models import Competitor, CompetitorRelease, CompetitorSnapshot

MAX_PER_APP = 100  # a whole market: one Lookup request per storefront carries 100
CHARTED = 4  # competitors in the charts, one color each: those with the most new ratings
RECENT = 30  # days of "new ratings" in the table
WEEKS = 12  # weeks in the new-ratings chart
TIMELINE = 182  # days in the release timeline
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
        store_listing(competitor, listing, timezone.localdate())
    competitor.tracked_for.add(app)
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


def store_listing(competitor: Competitor, r: dict, day: date) -> None:
    """Copy a Lookup result onto a competitor and record its version the first time one is
    seen, with Apple's release date and notes. A field the result leaves out keeps its value."""
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
    changed = [f for f, v in found.items() if v and getattr(competitor, f) != v]
    for f in changed:
        setattr(competitor, f, found[f])
    if changed:
        competitor.save(update_fields=changed)
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


class Rider:
    """Competitors in the ratings snapshot: the IDs it adds to each storefront's request, and
    what it does with their answers (``stats.extensions.SnapshotRider``)."""

    def __init__(self) -> None:
        self.rivals = {
            c.apple_id: c for c in Competitor.objects.filter(tracked_for__is_active=True).distinct()
        }
        self.listings: dict[int, dict] = {}  # the US one when there is one

    def ids(self) -> list[int]:
        return list(self.rivals)

    def save(self, day: date, storefront: str, answers: dict[int, dict]) -> int:
        rows = []
        for apple_id, r in answers.items():
            if storefront == "us" or apple_id not in self.listings:
                self.listings[apple_id] = r
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
            store_listing(self.rivals[apple_id], listing, day)

    def synthetic(self, day: date) -> int:
        rows = []
        for apple_id, rival in self.rivals.items():
            try:
                syn = synthetic.rival_by_apple_id(apple_id)
            except KeyError:
                continue
            self.listings[apple_id] = listing = synthetic.rival_listing(syn, day)
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
    weekly: list[int | None] = field(default_factory=list)
    releases: list[tuple[str, date, str]] = field(default_factory=list)  # (version, day, notes)

    @property
    def store_url(self) -> str:
        return f"https://apps.apple.com/us/app/id{self.apple_id}"

    @property
    def grade(self) -> str:
        return grade(self.average)

    @property
    def new_grade(self) -> str:
        return grade(self.new_average)

    @property
    def per_day(self) -> float | None:
        return self.new / RECENT if self.new is not None else None


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
        ))  # fmt: skip
    if end is None:
        return {"members": members, "end": None, "max": MAX_PER_APP, "charted": members}

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
        "weeks": WEEKS,
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
