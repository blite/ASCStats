"""Synthetic competitors and search results, so the demo and tests have other apps to compare
with. Like ``stats.synthetic``, every number is a deterministic function of (app, date)."""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from datetime import date, timedelta

from ascstats.stats.synthetic import APPS, COUNTRIES, EPOCH, _rng, version_on

# --- competitors ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SyntheticRival:
    """A made-up competitor: ratings arrive at a steady rate around a mean that each release
    shifts, and every release carries notes, as the public listing would show them."""

    apple_id: int
    name: str
    seller: str
    rival_of: tuple[int, ...]  # synthetic apps it competes with
    ratings_per_day: float
    stars: float  # mean stars of new ratings before any release shifts it
    starting: int  # ratings it already had on EPOCH
    releases: tuple[tuple[str, date, float, str], ...]  # (version, day, star shift, notes)
    price: str = "Free"
    genre: str = "Games"


# Apple IDs next to the synthetic apps', names prefixed "Synthetic" like theirs.
RIVALS = [
    SyntheticRival(
        1000000201, "Synthetic Worm Rush", "Example Arcade Ltd", (1000000001,), 9, 4.5, 1800, (
            ("3.0", EPOCH, 0.0, "A faster worm and new levels."),
            ("3.1", date(2026, 3, 9), 0.0, "Bug fixes and performance improvements."),
            ("3.2", date(2026, 8, 17), -0.7, "Daily challenges and a redesigned shop."),
            ("3.2.1", date(2026, 8, 31), -0.2, "Fixes a crash on launch."),
        ),
    ),
    SyntheticRival(
        1000000202, "Synthetic Slither Classic", "Example Retro Games", (1000000001,), 3, 4.1,
        600, (
            ("1.4", EPOCH, 0.0, "iPad support."),
            ("1.5", date(2026, 6, 1), 0.3, "Classic mode is back."),
        ),
        price="$1.99",
    ),
    SyntheticRival(
        1000000203, "Synthetic Number Place", "Example Puzzles Inc.", (1000000002,), 5, 4.6, 2500, (
            ("5.0", EPOCH, 0.0, "Thousands of new puzzles."),
            ("5.1", date(2026, 2, 16), 0.0, "Dark mode."),
            ("5.2", date(2026, 7, 6), 0.1, "Hints that explain the next step."),
            ("5.3", date(2026, 10, 12), -0.5, "Free puzzles now show an ad between games."),
        ),
    ),
    SyntheticRival(
        1000000204, "Synthetic Sky Atlas", "Example Science Apps", (1000000003,), 6, 4.3, 3000, (
            ("2.0", EPOCH, 0.0, "A new star catalogue."),
            ("2.4", date(2026, 5, 4), 0.0, "Satellite passes."),
            ("3.0", date(2026, 9, 14), 0.3, "Point your phone at the sky: AR view."),
        ),
        genre="Education",
    ),
    SyntheticRival(
        1000000205, "Synthetic Star Chart Lite", "Example Science Apps", (1000000003,), 1.5, 3.8,
        400, (("1.2", EPOCH, 0.0, "Constellation lines."),),
        genre="Education",
    ),
]  # fmt: skip


def rival_by_apple_id(apple_id: int) -> SyntheticRival:
    for rival in RIVALS:
        if rival.apple_id == apple_id:
            return rival
    raise KeyError(f"{apple_id} is not a synthetic competitor")


def rival_release_on(rival: SyntheticRival, day: date) -> tuple[str, date, float, str]:
    return [r for r in rival.releases if r[1] <= day][-1]


@functools.cache
def _rival_history_until(apple_id: int, until: date) -> dict[date, tuple[int, int]]:
    rival = rival_by_apple_id(apple_id)
    count, stars = rival.starting, round(rival.starting * rival.stars)
    out, day = {}, EPOCH
    while day <= until:
        rng = _rng("rival-ratings", apple_id, day)
        new = rival.ratings_per_day * (1.2 if day.weekday() >= 5 else 1.0) * rng.uniform(0.6, 1.4)
        n = int(new) + (1 if rng.random() < new % 1 else 0)
        mean = rival.stars + rival_release_on(rival, day)[2]
        for _ in range(n):
            stars += max(1, min(5, round(rng.gauss(mean, 0.9))))
        count += n
        out[day] = (count, stars)
        day += timedelta(days=1)
    return out


def rival_rating_snapshot(rival: SyntheticRival, day: date) -> dict[str, tuple[int, int]]:
    """{storefront: (count, star_sum)} as the public lookup would report on ``day``."""
    history = _rival_history_until(rival.apple_id, max(day, date.today() + timedelta(days=1)))
    count, stars = history[day]
    return {
        country.lower(): (round(count * weight), round(stars * weight))
        for country, (weight, _, _) in COUNTRIES.items()
    }


def rival_listing(rival: SyntheticRival, day: date) -> dict:
    """An iTunes Lookup result for a competitor on ``day``."""
    version, released, _, notes = rival_release_on(rival, day)
    return {
        "trackId": rival.apple_id,
        "trackName": rival.name,
        "sellerName": rival.seller,
        "primaryGenreName": rival.genre,
        "formattedPrice": rival.price,
        "releaseDate": f"{rival.releases[0][1].isoformat()}T08:00:00Z",
        "version": version,
        "currentVersionReleaseDate": f"{released.isoformat()}T08:00:00Z",
        "releaseNotes": notes,
    }


# --- search results ---------------------------------------------------------------------------

# Keywords each synthetic app tracks: (term, storefront)
KEYWORDS = {
    1000000001: [("snake game", "us"), ("classic snake", "us"), ("worm game", "us"),
                 ("snake game", "gb")],
    1000000002: [("sudoku puzzles", "us"), ("daily sudoku", "us")],
    1000000003: [("star map", "us"), ("night sky", "us"), ("astronomy app", "us")],
}  # fmt: skip
_ADJ = ["Pocket", "Retro", "Tiny", "Super", "Pixel", "Daily", "Zen", "Classic", "Neon", "Cosmic"]
_NOUN = ["Arcade", "Puzzles", "Quest", "Logic", "Explorer", "Studio", "Garden", "Mind", "Atlas"]
FILLER = [  # other apps that show up in results: (apple_id, name, seller)
    (1000009001 + i, f"Synthetic {_ADJ[i % len(_ADJ)]} {_NOUN[i * 7 % len(_NOUN)]}",
     f"Example Studio {i % 9 + 1}")
    for i in range(48)
]  # fmt: skip


def _relevance(apple_id: int, term: str, storefront: str, day: date) -> float:
    """How strongly an app matches a search on ``day``: a fixed fit per (app, term), a slow
    drift of a few weeks, a little daily noise, and for a synthetic app, the demand effect
    of the version it has live (better versions climb)."""
    fit = _rng("fit", apple_id, term, storefront).uniform(0.2, 1.0)
    phase = _rng("phase", apple_id, term).uniform(0, 2 * math.pi)
    drift = 0.12 * math.sin((day - EPOCH).days / 23 + phase)
    noise = _rng("serp", apple_id, term, storefront, day).gauss(0, 0.03)
    boost = 0.0
    for app in APPS:
        if app.apple_id == apple_id:
            boost = 0.25 * (version_on(app, day)[2] - 1)
    return fit + drift + noise + boost


def search_results(term: str, storefront: str, day: date) -> list[dict]:
    """The results a store search for ``term`` returns on ``day``, best match first, shaped
    like iTunes Search results. Apps that track or compete on the term always appear; about
    half the filler apps do."""
    owners = [a for a, terms in KEYWORDS.items() if (term, storefront) in terms]
    names = {a.apple_id: (a.name, "Synthetic Apps") for a in APPS}
    names |= {r.apple_id: (r.name, r.seller) for r in RIVALS}
    names |= {i: (n, s) for i, n, s in FILLER}
    pool = set(owners) | {r.apple_id for r in RIVALS if set(r.rival_of) & set(owners)}
    pool |= {i for i, _, _ in FILLER if _rng("in", i, term).random() < 0.5}
    ranked = sorted(pool, key=lambda i: -_relevance(i, term, storefront, day))
    return [
        {
            "trackId": i, "trackName": names[i][0], "sellerName": names[i][1],
            "userRatingCount": _rng("count", i).randint(40, 9000),
            "averageUserRating": round(_rng("avg", i).uniform(3.4, 4.9), 2),
        }
        for i in ranked
    ]  # fmt: skip


def suggestions(term: str, storefront: str) -> dict:
    """How the store's suggestions offer ``term``, shaped like ``suggestions.look``: most
    terms are suggested within a few letters, some only in full, a few not at all."""
    rng = _rng("suggest", term, storefront)
    roll = rng.random()
    if roll < 0.1:
        return {"reach": "absent", "prefix": None, "position": None, "continuations": 0,
                "requests": 1}  # fmt: skip
    if roll < 0.25:
        return {"reach": "listed", "prefix": None, "position": rng.randint(1, 6),
                "continuations": rng.randint(0, 3),
                "requests": 2 + min(len(term) - 1, 8)}  # fmt: skip
    prefix = rng.randint(2, min(6, len(term) - 1))
    return {"reach": "hit", "prefix": prefix, "position": rng.randint(1, 4),
            "continuations": rng.randint(2, 14), "requests": 2 + prefix}  # fmt: skip
