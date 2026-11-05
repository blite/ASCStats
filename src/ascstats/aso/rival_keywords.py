"""Competitors' keywords: the searches the apps you follow come up for, and where you stand
in the same searches (the Keywords tab's "Competitors' keywords" card).

Apple doesn't say which searches find an app; its keyword field is private. So a scan
(``scan``, run in the background from the card) guesses the terms a competitor aims at and
searches each once:

- the phrases in its name and subtitle, the two fields search reads most;
- what the store suggests as people type the first few of them;
- the phrases its description repeats.

A term searched in the last ``REUSE_DAYS`` days, tracked or scanned, is not searched again.
Each search is saved like a tracked keyword's (``keywords._save``) under a ``Keyword`` no app
tracks, so tracking it later keeps that first day. The card then lists every term, scanned or
tracked by any of your apps, where a competitor this app follows ranks well, whoever scanned
it. Nothing is tracked until a person presses Track.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock

import httpx
from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from ascstats import store
from ascstats.stats.models import App

from . import keywords as kw
from . import synthetic
from .finder import MAX_WORDS, TRAILING
from .keywords import FILLER
from .models import Competitor, Keyword, KeywordScan, KeywordSnapshot
from .scoring import search_key

log = logging.getLogger(__name__)

SCAN_TERMS = 30  # terms a scan looks at: about three minutes of searches at the store pace
HINT_SEEDS = 4  # name and subtitle phrases typed into the store's search box
HINTS_PER_SEED = 4  # suggestions kept from each
DESCRIPTION_PHRASES = 6
REUSE_DAYS = 7  # a search this recent answers a scan without asking again
HINT_PAUSE = 1.0  # seconds between suggestion requests: nobody is waiting on a scan
FAILURES_TO_STOP = 2
FRESH_DAYS = 30  # searches older than this are left off the card
WORST = 50  # a competitor placed below this does not count as ranking for the term
SHOWN = 120  # rows on the card at most


class ScanError(Exception):
    """Why a scan could not start; the message says so."""


# --- what to search ----------------------------------------------------------------------------


def _usable(words: list[str]) -> bool:
    """A phrase someone would search: not all filler, not cut off mid-thought, and a single
    word only when it means something on its own ("spades", never "free" or "2048")."""
    if not words or len(words) > MAX_WORDS:
        return False
    if words[0] in TRAILING or words[-1] in TRAILING:
        return False
    if all(w in FILLER or w.isdigit() for w in words):
        return False
    return not (len(words) == 1 and (len(words[0]) < 3 or words[0].isdigit()))


def _plain(text: str) -> list[str]:
    words = search_key(text).split()
    if settings.ASC_SOURCE == "synthetic":  # every made-up app's name starts with it
        words = [w for w in words if w != "synthetic"]
    return words


def grams(text: str, longest: int = 3) -> list[str]:
    """The usable 1- to ``longest``-word phrases in ``text``, two-word ones first (the most
    common shape of a search), then one, then three; in reading order within each."""
    words = _plain(text)
    found: dict[str, int] = {}
    for size in (2, 1, 3):
        if size > longest:
            continue
        for i in range(len(words) - size + 1):
            gram = words[i : i + size]
            if _usable(gram):
                found.setdefault(" ".join(gram), size)
    return list(found)


def repeated_phrases(text: str, limit: int = DESCRIPTION_PHRASES) -> list[str]:
    """Two- and three-word phrases a description uses more than once, most used first."""
    counts: Counter = Counter()
    for line in text.splitlines():
        words = _plain(line)
        for size in (2, 3):
            for i in range(len(words) - size + 1):
                gram = words[i : i + size]
                if _usable(gram):
                    counts[" ".join(gram)] += 1
    return [p for p, n in counts.most_common() if n >= 2][:limit]


def candidates(
    name: str, subtitle: str, description: str, ask: Callable[[str], list[str]],
    limit: int = SCAN_TERMS,
) -> list[str]:  # fmt: skip
    """The terms a scan looks at for an app with this listing, best guesses first. ``ask``
    returns the store's suggestions for what has been typed."""
    # Its whole name is its brand ("spades royale"): it ranks first, and that says nothing.
    # A one-word name is often the search itself ("Spades").
    brand = " ".join(_plain(name)) if len(_plain(name)) > 1 else ""
    own = grams(name) + grams(subtitle)
    meaning = {w for g in own for w in g.split() if w not in FILLER}
    out: dict[str, None] = {}
    for g in own:
        if g != brand:
            out.setdefault(g)
    seeds = [g for g in own if len(g.split()) <= 2][:HINT_SEEDS]
    for seed in seeds:
        kept = 0
        for offered in ask(seed):
            words = _plain(offered)
            term = " ".join(words)
            if term in (seed, brand) or not _usable(words) or not meaning & set(words):
                continue
            if term not in out:
                out.setdefault(term)
                kept += 1
            if kept >= HINTS_PER_SEED:
                break
    for p in repeated_phrases(description):
        out.setdefault(p)
    return list(out)[:limit]


# --- the scan --------------------------------------------------------------------------------


def start(app: App, competitor: Competitor, storefront: str) -> KeywordScan:
    """Queue a scan of ``competitor`` in ``storefront``, unless one is waiting or running."""
    from . import jobs

    if not re.fullmatch(r"[a-z]{2}", storefront):
        raise ScanError("Pick a storefront.")
    if not app.competitors.filter(pk=competitor.pk).exists():
        raise ScanError(f"{app.name} doesn't follow that app.")
    busy = KeywordScan.objects.filter(
        competitor=competitor, storefront=storefront,
        status__in=[KeywordScan.Status.QUEUED, KeywordScan.Status.RUNNING],
    ).first()  # fmt: skip
    if busy:
        return busy
    scan = KeywordScan.objects.create(competitor=competitor, storefront=storefront)
    jobs.scan_competitor.enqueue(scan.pk)
    return scan


def _listing(competitor: Competitor, storefront: str, client: httpx.Client) -> tuple[str, str, str]:
    """(name, subtitle, description) in ``storefront``: kept from the daily snapshot when it
    is that storefront's, else asked now (the listing is in the storefront's language)."""
    if competitor.listing_storefront == storefront and competitor.description:
        return competitor.name, competitor.subtitle, competitor.description
    found = store.lookup([competitor.apple_id], storefront, client)
    r = next((x for x in found if x.get("trackId") == competitor.apple_id), None)
    if r is None:
        raise store.StoreError(f"{competitor.name} is not in the {storefront.upper()} store")
    time.sleep(store.PAUSE)
    subtitle = store.subtitle(competitor.apple_id, storefront, client) or ""
    return r.get("trackName") or competitor.name, subtitle, r.get("description") or ""


def _recent(storefront: str, day: date) -> set[str]:
    """Terms searched in ``storefront`` in the last ``REUSE_DAYS`` days."""
    return set(
        KeywordSnapshot.objects.filter(
            keyword__storefront=storefront, date__gt=day - timedelta(days=REUSE_DAYS)
        ).values_list("keyword__term", flat=True)
    )


def run(
    scan: KeywordScan,
    day: date | None = None,
    transport: httpx.BaseTransport | None = None,
    pause: float = store.PAUSE,
    hint_pause: float = HINT_PAUSE,
) -> KeywordScan:
    """Look at the scan's terms and search each one not searched lately."""
    day = day or timezone.localdate()
    scan.status = KeywordScan.Status.RUNNING
    scan.save(update_fields=["status"])
    competitor, front = scan.competitor, scan.storefront
    try:
        if settings.ASC_SOURCE == "synthetic":
            searched, terms = _run_synthetic(competitor, front, day)
        else:
            searched, terms = _run_store(competitor, front, day, transport, pause, hint_pause)
    except store.StoreError as error:
        log.warning("keyword scan: %s", error)
        scan.status, scan.error = KeywordScan.Status.FAILED, str(error)[:255]
    else:
        scan.status = KeywordScan.Status.DONE
        scan.terms, scan.searched = terms, searched
    scan.finished_at = timezone.now()
    scan.save()
    return scan


def _keyword(term: str, storefront: str) -> Keyword:
    return Keyword.objects.get_or_create(term=term, storefront=storefront)[0]


def _run_synthetic(competitor: Competitor, front: str, day: date) -> tuple[int, list[str]]:
    try:
        rival = synthetic.rival_by_apple_id(competitor.apple_id)
    except KeyError:
        return 0, []
    listing = synthetic.rival_listing(rival, day)
    terms = candidates(
        listing["trackName"], listing["subtitle"], listing["description"],
        lambda t: synthetic.hints(t, front),
    )  # fmt: skip
    recent, searched = _recent(front, day), 0
    for term in terms:
        if term not in recent:
            kw._save(_keyword(term, front), day, synthetic.search_results(term, front, day))
            searched += 1
    return searched, terms


def _run_store(
    competitor: Competitor, front: str, day: date, transport, pause: float, hint_pause: float
) -> tuple[int, list[str]]:
    with store.client(transport) as client:
        name, subtitle, description = _listing(competitor, front, client)
        asked = 0

        def ask(typed: str) -> list[str]:
            nonlocal asked
            if front not in store.STOREFRONT_IDS:
                return []
            if asked and hint_pause:
                time.sleep(hint_pause)
            asked += 1
            try:
                return store.hints(typed, front, client)
            except store.StoreError as error:
                log.warning("keyword scan hints: %s", error)
                return []

        terms = candidates(name, subtitle, description, ask)
        recent = _recent(front, day)
        searched = failures = 0
        for term in terms:
            if term in recent:
                continue
            if searched + failures and pause:
                time.sleep(pause)
            try:
                results = store.search(term, front, client)
            except store.StoreError as error:
                log.warning("keyword scan search: %s", error)
                failures += 1
                if failures >= FAILURES_TO_STOP:
                    if not searched:
                        raise
                    break
                continue
            failures = 0
            kw._save(_keyword(term, front), day, results)
            searched += 1
    return searched, terms


def seed_day(day: date) -> None:
    """The synthetic competitors' scans, weekly, so the demo's card has terms to show."""
    if day.weekday() != 0:
        return
    for rival in Competitor.objects.filter(tracked_for__is_active=True).distinct():
        scan = run(KeywordScan.objects.create(competitor=rival, storefront="us"), day)
        at = datetime.combine(day, clock(9), tzinfo=UTC)  # dated by the day, not by the seeding
        KeywordScan.objects.filter(pk=scan.pk).update(created_at=at, finished_at=at)


# --- the card ----------------------------------------------------------------------------------


@dataclass
class Spot:
    rank: int
    competitor: Competitor


@dataclass
class Term:
    keyword: Keyword
    date: date
    rivals: list[Spot] = field(default_factory=list)  # best first
    you: int | None = None
    scores: kw.Scores = field(default_factory=kw.Scores)

    @property
    def best(self) -> int:
        return self.rivals[0].rank

    @property
    def top10(self) -> int:
        return sum(s.rank <= 10 for s in self.rivals)


def board(app: App, storefront: str | None) -> dict:
    """The card: every term searched lately in ``storefront`` that ``app`` doesn't track and
    a competitor it follows ranks for (top ``WORST``), the most competitors in the top 10
    first; and each competitor's latest scan there."""
    rivals = {c.apple_id: c for c in app.competitors.all()}
    out = {"terms": [], "scans": [], "front": storefront, "worst": WORST, "rivals": rivals}
    if not storefront or not rivals:
        return out
    tracked = set(app.keywords.filter(storefront=storefront).values_list("pk", flat=True))
    latest = (
        KeywordSnapshot.objects.filter(keyword__storefront=storefront)
        .exclude(keyword_id__in=tracked)
        .values("keyword_id")
        .annotate(day=Max("date"))
    )
    last = max((r["day"] for r in latest), default=None)
    if last is not None:
        fresh = {
            r["keyword_id"]: r["day"]
            for r in latest
            if r["day"] > last - timedelta(days=FRESH_DAYS)
        }
        snaps = KeywordSnapshot.objects.filter(keyword_id__in=fresh).select_related("keyword")
        terms = []
        for snap in snaps:
            if snap.date != fresh[snap.keyword_id]:
                continue
            spots = sorted(
                (Spot(r, c) for i, c in rivals.items() if (r := snap.rank(i)) and r <= WORST),
                key=lambda s: s.rank,
            )
            if not spots:
                continue
            terms.append(Term(snap.keyword, snap.date, spots, snap.rank(app.apple_id),
                              kw.scores(snap.keyword, snap, None)))  # fmt: skip
        terms.sort(key=lambda t: (-t.top10, t.best, -(t.scores.popularity or 0), t.keyword.term))
        out["terms"] = terms[:SHOWN]
        out["total"] = len(terms)
        out["date"] = last
    scans = {}
    for s in KeywordScan.objects.filter(competitor__in=rivals.values(), storefront=storefront):
        scans.setdefault(s.competitor_id, s)  # newest first
    out["scans"] = scans
    return out


def status(app: App, storefront: str) -> dict:
    """The scan form: each competitor with its latest scan in ``storefront``."""
    rivals = list(app.competitors.all())
    latest: dict[int, KeywordScan] = {}
    for s in KeywordScan.objects.filter(competitor__in=rivals, storefront=storefront):
        latest.setdefault(s.competitor_id, s)
    rows = [(c, latest.get(c.pk)) for c in rivals]
    busy = [
        s
        for _, s in rows
        if s and s.status in (KeywordScan.Status.QUEUED, KeywordScan.Status.RUNNING)
    ]
    # Never scanned first, then the longest ago
    rows.sort(key=lambda r: (r[1] is not None, r[1].created_at if r[1] else 0, r[0].name))
    return {"rows": rows, "busy": busy, "front": storefront, "app": app, "terms": SCAN_TERMS}
