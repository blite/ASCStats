"""Top charts: the App Store's top free, paid and grossing iPhone apps of a category, with
how far each app moved and what the chart says about the category.

A chart is one storefront, one category (Apple's genre ID, 0 for the whole store) and one
kind. The feed answers the top 100 (``store.CHART_DEPTH``); an app below that is not in it.
Nothing is fetched on a schedule: **Fetch** on the page asks the store for one category's
three charts (``fetch``), and the page shows that fetch until the next one. A fetch on a
later day keeps the earlier one, which is what movement is measured against.

The free overall chart holds no games, as Apple's feed answers it: read games in Games or one
of its genres.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import httpx
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ascstats import store
from ascstats.stats.models import App
from ascstats.stats.territories import name as territory_name

from .models import ChartApp, ChartCapture, Competitor

log = logging.getLogger(__name__)

KINDS = [k for k, _ in ChartCapture.Kind.choices]  # free, paid, grossing: the page's columns
OVERALL = 0
DEFAULT = ("us", 6014)  # Games: where the page opens before anything is fetched
COLUMN = 25  # rows a column shows before "Show all"
MOVERS = 8  # apps per list in the Movers card
WORDS = 30  # title words the Title words card lists
PHRASES = 15
FETCH_PAUSE = 1.0  # seconds between a fetch's three requests: one fetch, while you wait

# Apple's chart categories: (genre ID, name), grouped as the page's category menu lists them
APP_GENRES = [
    (6000, "Business"), (6001, "Weather"), (6002, "Utilities"), (6003, "Travel"),
    (6004, "Sports"), (6005, "Social Networking"), (6006, "Reference"), (6007, "Productivity"),
    (6008, "Photo & Video"), (6009, "News"), (6010, "Navigation"), (6011, "Music"),
    (6012, "Lifestyle"), (6013, "Health & Fitness"), (6014, "Games"), (6015, "Finance"),
    (6016, "Entertainment"), (6017, "Education"), (6018, "Books"), (6020, "Medical"),
    (6021, "Magazines & Newspapers"), (6023, "Food & Drink"), (6024, "Shopping"),
    (6026, "Developer Tools"), (6027, "Graphics & Design"),
]  # fmt: skip
GAME_GENRES = [
    (7001, "Action"), (7002, "Adventure"), (7003, "Casual"), (7004, "Board"), (7005, "Card"),
    (7006, "Casino"), (7009, "Family"), (7011, "Music"), (7012, "Puzzle"), (7013, "Racing"),
    (7014, "Role Playing"), (7015, "Simulation"), (7016, "Sports"), (7017, "Strategy"),
    (7018, "Trivia"), (7019, "Word"),
]  # fmt: skip
GENRES = {OVERALL: "All apps", **dict(APP_GENRES), **dict(GAME_GENRES)}
GAME_IDS = {g for g, _ in GAME_GENRES}


class ChartError(Exception):
    """A storefront or category the charts don't know."""


def genre_name(genre: int) -> str:
    """ "Puzzle games", "Games", "All apps": a category as the page names it."""
    if genre in GAME_IDS:
        return f"{GENRES[genre]} games"
    return GENRES.get(genre, str(genre))


def check(storefront: str, genre: int) -> None:
    if storefront not in store.STOREFRONT_IDS:
        raise ChartError(f"No App Store storefront {storefront!r}.")
    if genre not in GENRES:
        raise ChartError(f"No chart category {genre}.")


# ----------------------------------------------------------------------------- what an app says

# "no ads", "ad-free", "without ads", "zero ads", "no annoying ads", "free of ads"
_NO_ADS = re.compile(
    r"\b(?:no|zero|without|free of|never any|0)\s+(?:annoying\s+|intrusive\s+|pop-?ups?\s+|"
    r"forced\s+|third[- ]party\s+|banner\s+|video\s+)?(?:ads|adverts?|advertising|"
    r"advertisements?)\b|\bad[- ]?free\b|\bads?[- ]free\b",
    re.I,
)
# ...but an app that sells the way out of them has ads: "remove ads", "go ad-free",
# "ad-free version", "watch an ad", "non-intrusive ads", "no ads after every level", and
# "no ads" listed among what a subscription or VIP pass unlocks
_HAS_ADS = re.compile(
    r"\bremove\s+(?:all\s+|the\s+)?ads\b|\bads?\s+removal\b|\bgo\s+ad[- ]?free\b|"
    r"\bad[- ]?free\s+(?:version|upgrade|option|subscription|pass|mode)\b|"
    r"\bwatch(?:ing)?\s+(?:an?\s+|short\s+)*(?:video\s+)?ads?\b|\brewarded\s+(?:video|ads?)\b|"
    r"\bnon[- ]?intrusive\s+ads\b|\bcontains\s+ads\b|\bads\s+(?:help|support|keep)\b|"
    r"\bno\s+(?:annoying\s+|forced\s+)?ads\s+(?:after|between|during)\b|"
    r"\b(?:subscri\w*|vip|premium|membership)\b[^.!?]{0,120}\b(?:no|without)\s+ads\b",
    re.I,
)


def ads(summary: str) -> str:
    """What an app's description says about ads: "none" when it promises none, "some" when
    it mentions removing, watching or living with them, "" when it says neither. A reading of
    the listing's words, not of the app."""
    if _HAS_ADS.search(summary):
        return "some"
    if _NO_ADS.search(summary):
        return "none"
    return ""


# Words too common in app names to say anything about a category
_STOP_WORDS = (
    "a an and the of for to in on with by at or your you my our it is be are from as all "
    "app apps game games free new best pro lite plus hd official edition version vs"
)
_STOP = set(_STOP_WORDS.split())
_WORD = re.compile(r"[a-z0-9][a-z0-9'’]*")  # noqa: RUF001


def _name_words(name: str) -> list[list[str]]:
    """An app name's words, lowercase, split into the parts its dashes, colons and pipes
    separate ("Sudoku - Classic Puzzle" is two), so a phrase never spans them."""
    parts = re.split(r"\s[-–—|:]\s|[:|]", name.lower())  # noqa: RUF001
    return [[w.strip("'’") for w in _WORD.findall(p)] for p in parts]  # noqa: RUF001


def title_words(names: list[str]) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """The words and two-word phrases most app names in a chart share, with how many names
    use each: the category's own vocabulary, for keyword ideas."""
    words: Counter = Counter()
    phrases: Counter = Counter()
    for name in names:
        found_words, found_phrases = set(), set()
        for part in _name_words(name):
            found_words |= {w for w in part if w not in _STOP and len(w) > 2}
            found_phrases |= {
                f"{a} {b}" for a, b in itertools.pairwise(part)
                if not (a in _STOP and b in _STOP) and a not in _STOP | {"&"}
            }  # fmt: skip
        words.update(found_words)
        phrases.update(found_phrases)
    common = [(w, n) for w, n in words.most_common() if n > 1][:WORDS]
    paired = [(p, n) for p, n in phrases.most_common() if n > 1][:PHRASES]
    return common, paired


# -------------------------------------------------------------------------------------- fetch


def _save(day: date, storefront: str, genre: int, kind: str, apps: list[dict]) -> ChartCapture:
    with transaction.atomic():
        for a in apps:
            released = None
            with contextlib.suppress(ValueError):
                released = date.fromisoformat(a["released"]) if a.get("released") else None
            ChartApp.objects.update_or_create(
                apple_id=a["id"],
                defaults={
                    "name": a["name"][:255] or str(a["id"]), "seller": a["seller"][:255],
                    "icon_url": a["icon"][:500], "price": a["price"],
                    "currency": a["currency"][:3], "released_on": released,
                    "summary": a["summary"], "seen_on": day,
                },
            )  # fmt: skip
        capture, _ = ChartCapture.objects.update_or_create(
            date=day, storefront=storefront, genre=genre, kind=kind,
            defaults={"ids": [a["id"] for a in apps]},
        )  # fmt: skip
    return capture


def fetch(
    storefront: str,
    genre: int,
    day: date | None = None,
    transport: httpx.BaseTransport | None = None,
    pause: float = FETCH_PAUSE,
) -> None:
    """Fetch a category's free, paid and grossing charts now: three requests. A fetch
    replaces the same day's, and an earlier day's stays as the one to compare with. Nothing
    is saved unless all three answer; raises ``store.StoreError`` when the store refuses."""
    check(storefront, genre)
    day = day or timezone.localdate()
    answers = {}
    with store.client(transport) as client:
        for kind in KINDS:
            if answers and pause:
                time.sleep(pause)
            answers[kind] = store.top_chart(kind, genre or None, storefront, client)
    for kind, apps in answers.items():
        _save(day, storefront, genre, kind, apps)


def fetched() -> dict[tuple[str, int], date]:
    """Every category fetched at least once: (storefront, genre) -> its newest fetch."""
    newest = ChartCapture.objects.values("storefront", "genre").annotate(day=Max("date"))
    return {(r["storefront"], r["genre"]): r["day"] for r in newest}


def latest() -> tuple[str, int]:
    """The category fetched last, where /charts/ opens; Games in the US store before any."""
    last = ChartCapture.objects.order_by("-created_at").first()
    return (last.storefront, last.genre) if last else DEFAULT


def track(app: App, apple_id: int) -> Competitor:
    """Follow a chart's app as a competitor of ``app``, its name, seller and icon from the
    chart; the rest of its listing fills in from the store (``competitors.add``)."""
    from . import competitors

    hit = ChartApp.objects.filter(apple_id=apple_id).first()
    if hit is None:
        raise competitors.CompetitorError("That app is in no fetched chart.")
    listing = {"trackId": hit.apple_id, "trackName": hit.name, "sellerName": hit.seller,
               "artworkUrl100": hit.icon_url}  # fmt: skip
    return competitors.add(app, str(apple_id), listing=listing)


# --------------------------------------------------------------------------------------- page


@dataclass
class Row:
    rank: int
    app: ChartApp
    before: int | None = None  # its rank in the comparison chart; None: not in it
    compared: bool = False  # a comparison chart exists
    ads: str = ""
    you: bool = False
    rivals: list[str] = field(default_factory=list)  # your apps that follow it

    @property
    def change(self) -> int | None:
        """Places climbed (negative: fell); None when new or nothing to compare."""
        return None if self.before is None else self.before - self.rank

    @property
    def new(self) -> bool:
        return self.compared and self.before is None


@dataclass
class Column:
    kind: str
    label: str
    capture: ChartCapture | None
    compare_day: date | None
    rows: list[Row]
    dropped: list[tuple[int, ChartApp]]  # (its rank before, app): in the comparison, not now

    @property
    def head(self) -> list[Row]:
        return self.rows[:COLUMN]

    @property
    def rest(self) -> list[Row]:
        return self.rows[COLUMN:]

    @property
    def no_ads(self) -> int:
        return sum(r.ads == "none" for r in self.rows)


def _comparison(storefront: str, genre: int, kind: str, day: date):
    """The fetch before ``day``, however long before: the one its movement is measured from."""
    return (
        ChartCapture.objects.filter(
            storefront=storefront,
            genre=genre,
            kind=kind,
            date__lt=day,
        )
        .order_by("-date")
        .first()
    )


def page(storefront: str, genre: int, day: date | None = None) -> dict:
    """Everything the Top charts page shows for one category on one day."""
    check(storefront, genre)
    here = ChartCapture.objects.filter(storefront=storefront, genre=genre)
    captured = sorted(set(here.values_list("date", flat=True)), reverse=True)
    if day is None:
        day = captured[0] if captured else timezone.localdate()
    captures = {c.kind: c for c in here.filter(date=day)}
    compared = {k: _comparison(storefront, genre, k, day) for k in KINDS}
    wanted = {i for c in [*captures.values(), *filter(None, compared.values())] for i in c.ids}
    apps = ChartApp.objects.in_bulk(wanted, field_name="apple_id")
    yours = set(App.objects.filter(apple_id__in=wanted).values_list("apple_id", flat=True))
    rivals: dict[int, list[str]] = {}
    for c in Competitor.objects.filter(apple_id__in=wanted).prefetch_related("tracked_for"):
        rivals[c.apple_id] = [a.name for a in c.tracked_for.all() if a.is_active]

    columns = []
    for kind, label in ChartCapture.Kind.choices:
        cap, old = captures.get(kind), compared[kind]
        before = {i: n for n, i in enumerate(old.ids, 1)} if old else {}
        rows = [
            Row(rank=n, app=apps[i], before=before.get(i), compared=old is not None,
                ads=ads(apps[i].summary), you=i in yours, rivals=rivals.get(i, []))
            for n, i in enumerate(cap.ids if cap else [], 1) if i in apps
        ]  # fmt: skip
        now = set(cap.ids) if cap else set()
        dropped = (
            [(n, apps[i]) for i, n in before.items() if i not in now and i in apps] if cap else []
        )
        columns.append(Column(kind, label, cap, old.date if old else None, rows, dropped))

    movers = _movers(columns)
    names = list({r.app.apple_id: r.app.name for c in columns for r in c.rows}.values())
    words, phrases = title_words(names)
    done = fetched()
    i = captured.index(day) if day in captured else None
    return {
        "storefront": storefront, "genre": genre, "genre_label": genre_name(genre),
        "store_name": territory_name(storefront), "day": day,
        "today": timezone.localdate(), "captured": captured,
        "older": captured[i + 1] if i is not None and i + 1 < len(captured) else None,
        "newer": captured[i - 1] if i else None,
        "columns": columns, "any_capture": bool(captures),
        "mover_lists": [
            ("Climbed", movers["climbs"], "up"), ("New in the chart", movers["new"], "new"),
            ("Fell", movers["falls"], "down"), ("Dropped out", movers["dropped"], "dropped"),
        ],
        "words": words, "phrases": phrases, "chart_apps": len(names),
        "fetches": [
            {"storefront": sf, "genre": g, "label": genre_name(g), "store": territory_name(sf),
             "day": d}
            for (sf, g), d in sorted(done.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "storefronts": sorted(store.STOREFRONT_IDS, key=territory_name),
        "app_genres": [(OVERALL, GENRES[OVERALL]), *sorted(APP_GENRES, key=lambda g: g[1])],
        "game_genres": GAME_GENRES,
        "trackers": list(App.objects.researched().order_by("is_idea", "sort_order", "name")),
    }  # fmt: skip


def _movers(columns: list[Column]) -> dict:
    """The biggest climbs and falls, the new entries and the apps that dropped out, across
    the three charts; each item is (kind label, row or (rank, app))."""
    climbs, falls, new, dropped = [], [], [], []
    for c in columns:
        for r in c.rows:
            if r.new:
                new.append((c.label, r))
            elif r.change and r.change > 0:
                climbs.append((c.label, r))
            elif r.change and r.change < 0:
                falls.append((c.label, r))
        dropped += [(c.label, d) for d in c.dropped]
    return {
        "climbs": sorted(climbs, key=lambda m: -m[1].change)[:MOVERS],
        "new": sorted(new, key=lambda m: m[1].rank)[:MOVERS],
        "falls": sorted(falls, key=lambda m: m[1].change)[:MOVERS],
        "dropped": sorted(dropped, key=lambda m: m[1][0])[:MOVERS],
    }
