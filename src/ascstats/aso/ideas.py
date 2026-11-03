"""Keywords worth tracking next, from the App Store's search suggestions (the Keywords tab's
Suggestions card). Nothing here tracks anything: a person picks from the list.

Two kinds of seed are typed into the store's search box (``store.hints``), and what it
suggests back is the list:

- the app's own keywords in that storefront, shortest first, since a short term has the most
  continuations ("spades" offers "spades royale", "spades with friends");
- phrases in the names of the apps it follows, the ones several of them use first
  ("spades card": "spades card classic").

The suggestions are mostly other apps' names and searches for anything that shares a word, so
one is kept only if it names what the app is: a word of the app's own name that is not filler
(``keywords.FILLER``). That is what drops "offline music" seeded from "spades offline", or
"card game rules" from the phrase "card game".

Asking is live, about half a second a seed, so a look asks ``SEEDS`` of them and a deep look
``DEEP_SEEDS``; the answer is kept for the day.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from ascstats import store
from ascstats.stats.models import App

from . import synthetic
from .keywords import FILLER, MAX_PER_APP
from .scoring import search_key

log = logging.getLogger(__name__)

SEEDS = 12
DEEP_SEEDS = 40
PAUSE = 0.3  # seconds between hints requests: a person is waiting, and a look is short
MAX_WORDS = 5  # longer suggestions are someone's app name, not a search
SEED_WORDS = 3  # a longer seed's suggestions are only itself
NO_STOREFRONT = "The store has no suggestions for this storefront."
NOT_ANSWERING = "The App Store isn't answering; try again in a minute."
PHRASE_USERS = 2  # competitors whose names share a phrase before it is a seed
# A phrase ending in one of these is cut off mid-thought ("spades no", "spades with")
TRAILING = {"a", "an", "and", "by", "for", "in", "no", "of", "on", "or", "the", "to", "vs", "with"}


@dataclass
class Idea:
    term: str
    seeds: list[str] = field(default_factory=list)  # the seeds that offered it, first first
    position: int = 99  # best place among a seed's suggestions, from 1
    competitors: int = 0  # apps it follows that use the seed phrase in their name
    brand: bool = False  # an app it follows is named this: their brand, rarely your search


def _tracked(app: App, storefront: str) -> set[str]:
    terms = app.keywords.filter(storefront=storefront).values_list("term", flat=True)
    return {search_key(t) for t in terms}


def core_words(app: App) -> set[str]:
    """What the app is, from its name: "Spades: No Ads" is "spades"."""
    return {w for w in search_key(app.name).split() if w not in FILLER and not w.isdigit()}


def phrases(names: list[str], core: set[str]) -> dict[str, int]:
    """Two- and three-word phrases in the competitors' ``names`` holding a ``core`` word, by
    how many names use them, those used by at least ``PHRASE_USERS``."""
    users: dict[str, int] = {}
    for n in names:
        found = set()
        words = search_key(n).split()  # "Spades - Classic Cards" reads "spades classic cards"
        for size in (2, 3):
            for i in range(len(words) - size + 1):
                gram = words[i : i + size]
                if core & set(gram) and gram[-1] not in TRAILING:
                    found.add(" ".join(gram))
        for p in found:
            users[p] = users.get(p, 0) + 1
    return {p: n for p, n in users.items() if n >= PHRASE_USERS}


def seeds(app: App, storefront: str, limit: int) -> list[tuple[str, int]]:
    """What to type, with how many competitors use it (0 for the app's own keywords): the
    app's keywords and its competitors' phrases, taking turns, shortest first."""
    core = core_words(app)
    own = sorted(
        {
            k
            for k in app.keywords.filter(storefront=storefront).values_list("term", flat=True)
            if len(k.split()) <= SEED_WORDS and core & set(search_key(k).split())
        },
        key=lambda t: (len(t.split()), t),
    )
    theirs = sorted(
        phrases(list(app.competitors.values_list("name", flat=True)), core).items(),
        key=lambda p: (-p[1], len(p[0].split()), p[0]),
    )
    out: dict[str, int] = {}
    for i in range(max(len(own), len(theirs))):
        for term, used in ([(own[i], 0)] if i < len(own) else []) + theirs[i : i + 1]:
            out.setdefault(term, used)
    return list(out.items())[:limit]


def gather(
    app: App, storefront: str, ask: Callable[[str], list[str]], limit: int = SEEDS
) -> tuple[list[Idea], int]:
    """The ideas, most offered first, and how many seeds were asked. ``ask`` returns the
    store's suggestions for what has been typed."""
    core = core_words(app)
    tracked = _tracked(app, storefront)
    names = {search_key(n) for n in app.competitors.values_list("name", flat=True)}
    ideas: dict[str, Idea] = {}
    asked = seeds(app, storefront, limit)
    for seed, used in asked:
        for at, offered in enumerate(ask(seed), 1):
            term = search_key(offered)
            words = term.split()
            if term in tracked or not core & set(words) or len(words) > MAX_WORDS:
                continue
            if len(words) == 1 or term in FILLER:
                continue
            brand = any(n == term or n.startswith(f"{term} ") for n in names) and len(words) > 1
            idea = ideas.setdefault(term, Idea(term, brand=brand))
            if seed not in idea.seeds:
                idea.seeds.append(seed)
            idea.position = min(idea.position, at)
            idea.competitors = max(idea.competitors, used)
    ranked = sorted(ideas.values(), key=lambda i: (-len(i.seeds), i.position, i.term))
    return ranked, len(asked)


def _key(app: App, storefront: str, deep: bool) -> str:
    return f"ideas:{app.apple_id}:{storefront}:{int(deep)}:{timezone.localdate()}"


def cached(app: App, storefront: str, deep: bool) -> dict | None:
    """Today's look, less what has been tracked since."""
    out = cache.get(_key(app, storefront, deep))
    if out is None:
        return None
    tracked = _tracked(app, storefront)
    return out | {
        "ideas": [i for i in out["ideas"] if i.term not in tracked],
        "room": MAX_PER_APP - app.keywords.count(),
    }


def look(
    app: App,
    storefront: str,
    deep: bool = False,
    transport: httpx.BaseTransport | None = None,
    pause: float = PAUSE,
) -> dict:
    """Ask the store for ideas now and keep the answer for the day; ``error`` says why there
    are none when the store would not answer."""
    limit = DEEP_SEEDS if deep else SEEDS
    if settings.ASC_SOURCE == "synthetic":
        found, asked = gather(app, storefront, lambda t: synthetic.hints(t, storefront), limit)
        out = {"ideas": found, "asked": asked, "error": ""}
    elif storefront not in store.STOREFRONT_IDS:
        return {"ideas": [], "asked": 0, "error": NO_STOREFRONT}
    else:
        requests = 0
        with store.client(transport) as client:

            def ask(term: str) -> list[str]:
                nonlocal requests
                if requests and pause:
                    time.sleep(pause)
                requests += 1
                return store.hints(term, storefront, client)

            try:
                found, asked = gather(app, storefront, ask, limit)
            except store.StoreError as error:
                log.warning("ideas: %s", error)
                return {"ideas": [], "asked": 0, "error": NOT_ANSWERING}
        out = {"ideas": found, "asked": asked, "error": ""}
    out["room"] = MAX_PER_APP - app.keywords.count()
    cache.set(_key(app, storefront, deep), out, 24 * 3600)
    return out
