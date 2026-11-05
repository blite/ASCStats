"""A weekly look at how the store's search suggestions offer each keyword: the evidence the
popularity estimate leans on most (``scoring.popularity``). Terms people search a lot are
suggested after a letter or two, and many longer searches start with them.

One look types the whole term, then its first letters one at a time (up to 8) until the term
is suggested, then the term and a space: 2 to 10 requests. Popularity moves slowly, so each
keyword is looked at once a week, never-seen and oldest first, at most ``MAX_PER_RUN`` a run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date

import httpx
from django.conf import settings
from django.db.models import Count, Max, Q
from django.utils import timezone

from ascstats import store

from . import synthetic
from .models import Keyword, KeywordSuggestion
from .scoring import PREFIX_CAP, continues, search_key

log = logging.getLogger(__name__)

WEEK = 7
MAX_PER_RUN = 50  # about 25 minutes at the store pace, after the nightly searches
FAILURES_TO_STOP = 2


def look(term: str, ask: Callable[[str], list[str]]) -> dict:
    """How the suggestions offer ``term``. ``ask`` returns the suggestions for what has been
    typed; each distinct text is asked once."""
    asked: dict[str, list[str]] = {}

    def suggestions(typed: str) -> list[str]:
        if typed not in asked:
            asked[typed] = ask(typed)
        return asked[typed]

    target = search_key(term)

    def position(typed: str) -> int | None:
        keys = [search_key(s) for s in suggestions(typed)]
        return keys.index(target) + 1 if target in keys else None

    full = position(term)
    if full is None:
        found = {"reach": "absent", "prefix": None, "position": None}
    else:
        found = {"reach": "listed", "prefix": None, "position": full}
        for length in range(1, min(len(term) - 1, PREFIX_CAP) + 1):
            if (at := position(term[:length])) is not None:
                found = {"reach": "hit", "prefix": length, "position": at}
                break
    offered = {search_key(s) for typed in (term, f"{term} ") for s in suggestions(typed)}
    return found | {
        "continuations": sum(continues(s, term) for s in offered),
        "requests": len(asked),
    }


def due(day: date, limit: int = MAX_PER_RUN) -> list[Keyword]:
    """Tracked keywords not looked at this week, never-seen first, then the oldest. An idea's
    keywords are looked at once, like their one search, so their popularity has its evidence."""
    last = dict(
        KeywordSuggestion.objects.values("keyword")
        .annotate(d=Max("date"))
        .values_list("keyword", "d")
    )
    # one query: every keyword an active app or idea tracks, with how many real apps track it
    every = Keyword.objects.filter(tracked_for__is_active=True).annotate(
        live=Count("tracked_for", filter=Q(tracked_for__is_idea=False))
    )
    waiting = [
        k for k in every
        if k.pk not in last or (k.live and (day - last[k.pk]).days >= WEEK)
    ]  # fmt: skip
    waiting.sort(key=lambda k: (k.pk in last, last.get(k.pk) or date.min, k.term))
    return waiting[:limit]


def _save(keyword: Keyword, day: date, found: dict) -> None:
    KeywordSuggestion.objects.update_or_create(keyword=keyword, date=day, defaults=found)


def probe(
    day: date | None = None,
    source: str | None = None,
    transport: httpx.BaseTransport | None = None,
    pause: float = store.PAUSE,
    limit: int = MAX_PER_RUN,
) -> int:
    """Look at the suggestions for the keywords due today; returns the keywords looked at.
    Two keywords failing in a row stop the run for the day."""
    day = day or timezone.localdate()
    source = source or settings.ASC_SOURCE
    keywords = due(day, limit)
    if source == "synthetic":
        for k in keywords:
            _save(k, day, synthetic.suggestions(k.term, k.storefront))
        return len(keywords)
    looked = failures = requests = 0
    with store.client(transport) as client:

        def ask(front: str) -> Callable[[str], list[str]]:
            def one(typed: str) -> list[str]:
                nonlocal requests
                if requests and pause:
                    time.sleep(pause)
                requests += 1
                return store.hints(typed, front, client)

            return one

        for k in keywords:
            if k.storefront not in store.STOREFRONT_IDS:
                log.warning("suggestions: no storefront number for %s, skipped", k)
                continue
            try:
                _save(k, day, look(k.term, ask(k.storefront)))
                looked += 1
                failures = 0
            except store.StoreError as error:
                log.warning("suggestions: %s", error)
                failures += 1
                if failures >= FAILURES_TO_STOP:
                    log.error("suggestions stopped: %d keywords failed in a row", failures)
                    break
    log.info("suggestions: looked at %d keywords in %d requests", looked, requests)
    return looked


def seed_day(day: date) -> None:
    probe(day, source="synthetic")


def latest(keywords: list[Keyword]) -> dict[int, KeywordSuggestion]:
    """Each keyword's most recent look, by keyword id."""
    out: dict[int, KeywordSuggestion] = {}
    for s in KeywordSuggestion.objects.filter(keyword__in=keywords).order_by("date"):
        out[s.keyword_id] = s
    return out
