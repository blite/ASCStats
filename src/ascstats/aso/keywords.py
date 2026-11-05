"""Keyword ranks and scores: where your apps and their competitors come up in App Store search,
and how popular and how hard to rank for each term is (``scoring``, ``suggestions``).

Each tracked keyword (a term in one storefront) is searched once a day through iTunes Search
(``store.search``, the top 200 iPhone apps), and the whole result order is kept, so every
app's position, yours, a competitor's or one you start following later, comes from the same
saved searches. The public Search API's order follows the App Store's own closely but not
exactly, and Apple publishes search volume only through Apple Ads, so popularity is an
estimate from the results and the store's search suggestions.

Only a person adds keywords (``add``, from the Keywords tab or ``add_keyword``): nothing here
tracks a term on its own, not from the results, the suggestions or a competitor's listing.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ascstats import store
from ascstats.stats.charts import days as _days
from ascstats.stats.charts import fmt_day, month_ticks, nice_step
from ascstats.stats.models import App
from ascstats.stats.territories import flag, name

from . import scoring, synthetic
from .competitors import COLORS
from .models import Keyword, KeywordSnapshot, KeywordSuggestion

log = logging.getLogger(__name__)

MAX_PER_APP = 150  # one search a day each; 150 keywords is about 12 minutes at the store pace
FAILURES_TO_STOP = 2  # searches failing in a row before the run stops for the day
TOP = 25  # results kept with their names: the popularity model reads 25, the list shows 10
WEEK = 7
SPARK_DAYS = 30
FIRST_PAGE = 10  # results a keyword's menu lists, yours and your competitors' marked
HISTORY_DAYS = 90  # days in the rank chart


# Words too general to track on their own: nobody finds an app by searching just "ads". They
# still count inside a phrase ("euchre no ads").
FILLER = {
    "a", "ad", "ads", "an", "and", "app", "apps", "best", "big", "by", "card", "cards", "classic",
    "daily", "easy", "for", "free", "fun", "game", "games", "in", "large", "lite", "my", "new",
    "no", "of", "offline", "on", "online", "plus", "pro", "the", "to", "top", "with", "your",
}  # fmt: skip


class KeywordError(Exception):
    """Something a person adding a keyword can fix; the message says what."""


def normalize(term: str) -> str:
    return " ".join(term.lower().split())


def add(app: App, text: str, storefront: str = "us") -> list[Keyword]:
    """Track each term in ``text`` (one per line, or comma-separated) for ``app`` in one
    storefront. A term another app tracks there already is shared, history and all."""
    storefront = storefront.lower()
    if not re.fullmatch(r"[a-z]{2}", storefront):
        raise KeywordError("Pick a storefront.")
    terms = list(dict.fromkeys(normalize(t) for t in re.split(r"[\n,]", text) if t.strip()))
    if not terms:
        raise KeywordError("Type at least one search term.")
    if too_long := [t for t in terms if len(t) > 100]:
        raise KeywordError(f"Too long for a search term: {too_long[0][:40]}…")
    if filler := [t for t in terms if t in FILLER]:
        raise KeywordError(
            f"“{filler[0]}” is too general to track on its own; track it in a phrase, like "
            f"“{app.name.split(':')[0].split('•')[0].strip().lower()} {filler[0]}”."
        )
    tracked = set(app.keywords.filter(storefront=storefront).values_list("term", flat=True))
    new = [t for t in terms if t not in tracked]
    if not new:
        raise KeywordError(f"{app.name} already tracks {'that' if len(terms) == 1 else 'those'}.")
    if app.keywords.count() + len(new) > MAX_PER_APP:
        room = MAX_PER_APP - app.keywords.count()
        raise KeywordError(f"{app.name} can track {MAX_PER_APP} keywords; room for {room} more.")
    added = []
    for term in new:
        keyword, _ = Keyword.objects.get_or_create(term=term, storefront=storefront)
        keyword.tracked_for.add(app)
        added.append(keyword)
    return added


def remove(app: App, keyword: Keyword) -> None:
    """Stop tracking ``keyword`` for ``app``. Its saved searches are kept; a keyword no app
    tracks is no longer searched."""
    keyword.tracked_for.remove(app)


# --- the daily search ------------------------------------------------------------------------


def _save(keyword: Keyword, day: date, results: list[dict]) -> None:
    ids = [r["trackId"] for r in results if r.get("trackId")]
    top = [
        {
            "id": r.get("trackId"), "name": r.get("trackName") or "",
            "seller": r.get("sellerName") or r.get("artistName") or "",
            "icon": r.get("artworkUrl100") or r.get("artworkUrl60") or "",
            "ratings": r.get("userRatingCount") or 0, "average": r.get("averageUserRating"),
        }
        for r in results[:TOP]
    ]  # fmt: skip
    with transaction.atomic():  # replaced, not updated, so the cached pages see a new row
        KeywordSnapshot.objects.filter(keyword=keyword, date=day).delete()
        KeywordSnapshot.objects.create(keyword=keyword, date=day, ids=ids, top=top)


def as_listing(result: dict) -> dict:
    """A result ``_save`` kept, back in the Lookup shape ``competitors.store_listing`` reads."""
    return {
        "trackId": result["id"], "trackName": result["name"],
        "sellerName": result.get("seller", ""), "artworkUrl100": result.get("icon", ""),
    }  # fmt: skip


def tracked() -> list[Keyword]:
    """Keywords tracked for at least one active app: the ones searched."""
    return list(Keyword.objects.filter(tracked_for__is_active=True).distinct())


def snapshot(
    day: date | None = None,
    source: str | None = None,
    transport: httpx.BaseTransport | None = None,
    pause: float = store.PAUSE,
    only: list[Keyword] | None = None,
) -> int:
    """Search every tracked keyword (or ``only`` these) once and save the results; returns the
    searches saved.

    A keyword already searched today is not searched again, so a rerun after a failure only
    asks what is missing; two searches failing in a row stop the run for the day."""
    day = day or timezone.localdate()
    source = source or settings.ASC_SOURCE
    keywords = tracked() if only is None else only
    if source == "synthetic":
        for k in keywords:
            _save(k, day, synthetic.search_results(k.term, k.storefront, day))
        return len(keywords)
    done = set(KeywordSnapshot.objects.filter(date=day).values_list("keyword_id", flat=True))
    saved = failures = 0
    with store.client(transport) as client:
        for k in keywords:
            if k.pk in done:
                continue
            if saved + failures and pause:
                time.sleep(pause)
            try:
                _save(k, day, store.search(k.term, k.storefront, client))
                saved += 1
                failures = 0
            except store.StoreError as error:
                log.warning("keyword search: %s", error)
                failures += 1
                if failures >= FAILURES_TO_STOP:
                    log.error("keyword search stopped: %d failed in a row", failures)
                    break
    log.info("keyword searches: %d saved, %d already saved today", saved, len(done))
    return saved


def seed_synthetic() -> None:
    """The synthetic apps' keywords."""
    for app in App.objects.filter(apple_id__in=synthetic.KEYWORDS):
        for term, front in synthetic.KEYWORDS[app.apple_id]:
            Keyword.objects.get_or_create(term=term, storefront=front)[0].tracked_for.add(app)


def seed_day(day: date) -> None:
    snapshot(day, source="synthetic")


# --- the Keywords tab ------------------------------------------------------------------------


@dataclass
class Player:
    """Your app or a competitor, in the rank comparisons."""

    apple_id: int
    name: str
    color: str
    you: bool


def players(app: App) -> list[Player]:
    out = [Player(app.apple_id, app.name, COLORS[0], True)]
    for i, c in enumerate(app.competitors.all()):
        out.append(Player(c.apple_id, c.name, COLORS[min(i + 1, len(COLORS) - 1)], False))
    return out


def _spark(ranks: list[int | None], width: int = 100, height: int = 24) -> list[str]:
    """Polylines for a rank sparkline, best rank at the top, broken where the app was not in
    the results."""
    seen = [r for r in ranks if r is not None]
    worst = max([*seen, 10])
    step = width / max(len(ranks) - 1, 1)
    lines, current = [], []
    for i, r in enumerate(ranks):
        if r is None:
            if current:
                lines.append(current)
            current = []
            continue
        current.append(f"{i * step:.1f},{2 + (height - 4) * (r - 1) / (worst - 1):.1f}")
    if current:
        lines.append(current)
    return [" ".join(pts) for pts in lines if len(pts) > 1]


@dataclass
class Scores:
    """A keyword's difficulty, popularity and opportunity (0-100) and the evidence behind them."""

    difficulty: int | None = None
    popularity: int | None = None
    opportunity: int | None = None
    strength: float | None = None  # how strong the top 10 are, 0-1
    targeting: float | None = None  # share of the top 10 whose titles aim at the term
    suggestion: KeywordSuggestion | None = None  # None: not looked at yet

    @property
    def estimated(self) -> bool:
        """Popularity read from the results alone, before the suggestions were looked at."""
        return self.suggestion is None


def scores(
    keyword: Keyword, snap: KeywordSnapshot | None, look: KeywordSuggestion | None
) -> Scores:
    if snap is None:
        return Scores(suggestion=look)
    page = [scoring.Result(r.get("name") or "", r.get("ratings")) for r in snap.top]
    reach = (
        scoring.Reach(look.reach, look.prefix, look.position) if look else scoring.Reach("unknown")
    )
    difficulty = scoring.difficulty(page, keyword.term)
    popularity = scoring.popularity(
        page, keyword.term, len(snap.ids), reach, look.continuations if look else None
    )
    return Scores(
        difficulty=difficulty,
        popularity=popularity,
        opportunity=scoring.opportunity(popularity, difficulty),
        strength=scoring.page_strength(page[:10]) if page else None,
        targeting=scoring.targeting(page[:10], keyword.term) if page else None,
        suggestion=look,
    )


def latest_suggestions(keywords: list[Keyword]) -> dict[int, KeywordSuggestion]:
    from .suggestions import latest

    return latest(keywords)


@dataclass
class Row:
    keyword: Keyword
    date: date | None = None
    rank: int | None = None
    change: int | None = None  # positions gained since a week before (negative: lost)
    best: int | None = None  # best rank in the sparkline window
    rivals: list[tuple[int, str, int]] = field(default_factory=list)  # (rank, name, apple_id)
    top: list[dict] = field(default_factory=list)  # first results, "who": you, rival or None
    results: int = 0
    spark: list[str] = field(default_factory=list)
    scores: Scores = field(default_factory=Scores)

    @property
    def further(self) -> list[tuple[int, str, int]]:
        """Competitors placed today below the first page the menu lists."""
        return [r for r in self.rivals if r[0] > FIRST_PAGE]

    @property
    def flag(self) -> str:
        return flag(self.keyword.storefront)

    @property
    def country(self) -> str:
        return name(self.keyword.storefront)


@dataclass
class Storefront:
    """A storefront the app tracks keywords in: one tab above the keyword table."""

    code: str
    count: int

    @property
    def country(self) -> str:
        return name(self.code)


def storefronts(app: App) -> list[Storefront]:
    """The storefronts ``app`` tracks keywords in: the US first, then the most keywords first
    (ties by country name)."""
    counts: dict[str, int] = {}
    for code in app.keywords.values_list("storefront", flat=True):
        counts[code] = counts.get(code, 0) + 1
    fronts = [Storefront(code, n) for code, n in counts.items()]
    return sorted(fronts, key=lambda f: (f.code != "us", -f.count, f.country))


def board(app: App, storefront: str | None = None) -> dict:
    """One row per keyword the app tracks (in ``storefront``, or in every storefront): its rank
    today, the change since a week before, a 30-day sparkline, the competitors placed today
    (best first) and the first results, whoever they are."""
    you = app.apple_id
    rivals = {p.apple_id: p.name for p in players(app)[1:]}
    rows = []
    keywords = app.keywords.all()
    if storefront is not None:
        keywords = keywords.filter(storefront=storefront)
    keywords = list(keywords)
    looks = latest_suggestions(keywords)
    for k in keywords:
        snaps = list(k.snapshots.order_by("date"))
        row = Row(k, scores=scores(k, snaps[-1] if snaps else None, looks.get(k.pk)))
        if snaps:
            latest = snaps[-1]
            row.date, row.rank, row.results = latest.date, latest.rank(you), len(latest.ids)
            before = [s for s in snaps if s.date <= latest.date - timedelta(days=WEEK)]
            if before and row.rank and (old := before[-1].rank(you)):
                row.change = old - row.rank
            window = [s for s in snaps if s.date > latest.date - timedelta(days=SPARK_DAYS)]
            ranks = [s.rank(you) for s in window]
            row.best = min(filter(None, ranks), default=None)
            row.spark = _spark(ranks)
            row.rivals = sorted((r, n, i) for i, n in rivals.items() if (r := latest.rank(i)))
            who = {you: "you"} | dict.fromkeys(rivals, "rival")
            row.top = [
                r | {"rank": n, "who": who.get(r["id"])}
                for n, r in enumerate(latest.top[:FIRST_PAGE], 1)
            ]
        rows.append(row)
    rows.sort(key=lambda r: (r.rank is None, r.rank or 0, r.keyword.term))
    return {
        "rows": rows,
        "date": max((r.date for r in rows if r.date), default=None),
        "max": MAX_PER_APP,
        "limit": store.SEARCH_LIMIT,
    }


@dataclass
class Summary:
    """The keyword tiles: how many tracked keywords an app
    ranks in the top 10 for, and how many climbed or fell since a week before."""

    tracked: int = 0  # (app, keyword) pairs
    top10: int = 0
    compared: int = 0  # pairs with a search a week before their latest
    top10_change: int = 0  # among those compared
    up: int = 0  # climbed, counting the new
    down: int = 0  # fell, counting the lost
    new: int = 0  # in the results now, not a week before
    lost: int = 0  # in the results a week before, not now
    date: date | None = None


def summary(apps: list[App], storefront: str | None = None) -> Summary:
    """The keyword tiles for ``apps`` (in ``storefront``, or in every storefront): each pair's
    latest search against its latest a week or more before, as the Keywords tab's 7-day
    column compares them."""
    pairs = Keyword.tracked_for.through.objects.filter(app__in=apps)
    if storefront is not None:
        pairs = pairs.filter(keyword__storefront=storefront)
    pairs = list(pairs.values_list("keyword_id", "app__apple_id"))
    out = Summary(tracked=len(pairs))
    if not pairs:
        return out
    searches: dict[int, list[tuple[date, list[int]]]] = {}
    snaps = KeywordSnapshot.objects.filter(keyword_id__in={k for k, _ in pairs}).order_by("date")
    last = snaps.values_list("date", flat=True).last()
    if last is None:
        return out
    since = last - timedelta(days=SPARK_DAYS)  # a keyword not searched for a month is left out
    for k, day, ids in snaps.filter(date__gte=since).values_list("keyword_id", "date", "ids"):
        searches.setdefault(k, []).append((day, ids))
    out.date = last

    def rank(ids: list[int], apple_id: int) -> int | None:
        return ids.index(apple_id) + 1 if apple_id in ids else None

    for k, you in pairs:
        if k not in searches:
            continue
        day, ids = searches[k][-1]
        now = rank(ids, you)
        out.top10 += bool(now and now <= 10)
        before = [s for s in searches[k] if s[0] <= day - timedelta(days=WEEK)]
        if not before:
            continue
        old = rank(before[-1][1], you)
        out.compared += 1
        out.top10_change += bool(now and now <= 10) - bool(old and old <= 10)
        out.new += bool(now and not old)
        out.lost += bool(old and not now)
        out.up += bool(now and (not old or now < old))
        out.down += bool(old and (not now or now > old))
    return out


W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 240, 40, 12, 12, 22


def detail(app: App, keyword: Keyword) -> dict:
    """One keyword over the last ``HISTORY_DAYS``: a rank line per app (yours and each
    competitor that showed up), today's top results, and where each app stands."""
    snaps = list(keyword.snapshots.order_by("date"))
    look = latest_suggestions([keyword]).get(keyword.pk)
    out = {
        "keyword": keyword,
        "flag": flag(keyword.storefront),
        "country": name(keyword.storefront),
        "scores": scores(keyword, snaps[-1] if snaps else None, look),
        "typed": keyword.term[: look.prefix] if look and look.prefix else "",
    }
    if not snaps:
        return out
    end = snaps[-1].date
    since = end - timedelta(days=HISTORY_DAYS - 1)
    snaps = [s for s in snaps if s.date >= since]
    latest = snaps[-1]
    you, *rivals = players(app)
    ranks = {p.apple_id: [s.rank(p.apple_id) for s in snaps] for p in (you, *rivals)}
    # Only the competitors this search has shown, best placed first and colored in that order
    ranked = sorted(
        (p for p in rivals if any(ranks[p.apple_id])),
        key=lambda p: min(r for r in ranks[p.apple_id] if r),
    )
    ps = [you] + [
        replace(p, color=COLORS[min(i, len(COLORS) - 1)]) for i, p in enumerate(ranked, start=1)
    ]
    seen = [r for rs in ranks.values() for r in rs if r is not None]
    worst = max([*seen, 10])
    step = nice_step(worst, 4)
    bottom = min(store.SEARCH_LIMIT, step * -(-worst // step))
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    span = max((end - since).days, 1)

    def x(day: date) -> float:
        return round(PAD_L + plot_w * (day - since).days / span, 1)

    def y(rank: float) -> float:
        return round(PAD_T + plot_h * (rank - 1) / max(bottom - 1, 1), 1)

    series = []
    for p in ps:
        if not any(ranks[p.apple_id]):
            continue
        lines, current, dots = [], [], []
        for s, r in zip(snaps, ranks[p.apple_id], strict=True):
            if r is None:
                if current:
                    lines.append(current)
                current = []
                continue
            current.append(f"{x(s.date)},{y(r)}")
            dots.append({"x": x(s.date), "y": y(r), "tip": f"{p.name}\n{fmt_day(s.date)}: #{r}"})
        if current:
            lines.append(current)
        series.append({
            "player": p, "lines": [" ".join(c) for c in lines],
            "dots": dots if len(snaps) <= 120 else dots[-1:],
        })  # fmt: skip
    ticks = [1, *range(int(step), int(bottom) + 1, int(step))] if step >= 1 else [1]
    every = _days(since, end)
    week = [s for s in snaps if s.date <= end - timedelta(days=WEEK)]
    standings = []
    for p in ps:
        now = latest.rank(p.apple_id)
        old = week[-1].rank(p.apple_id) if week else None
        standings.append({
            "player": p, "rank": now,
            "change": old - now if now and old else None,
            "best": min(filter(None, ranks[p.apple_id]), default=None),
        })  # fmt: skip
    mine = {p.apple_id: p for p in ps}
    return out | {
        "date": end,
        "series": series,
        "grid": [{"y": y(t), "label": f"#{t}"} for t in ticks],
        "xticks": month_ticks(every, lambda i: x(every[i]), 56),
        "top": [
            r | {"rank": i + 1, "player": mine.get(r["id"])} for i, r in enumerate(latest.top[:10])
        ],
        "standings": standings,
        "results": len(latest.ids),
        "limit": store.SEARCH_LIMIT,
        "width": W, "height": H, "pad_l": PAD_L, "plot_r": W - PAD_R,
    }  # fmt: skip
