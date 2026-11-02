"""Keyword difficulty, popularity, volume and opportunity for App Store
search. The popularity weights are fitted against Apple Ads search terms.

Every score comes from evidence ASCStats already keeps: the day's search results (titles and
rating counts) and a weekly look at the store's search suggestions. Apple publishes real
search popularity only through Apple Ads, so popularity here is an estimate.

Scales: difficulty, popularity (as "volume") and opportunity are 0-100.
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from dataclasses import dataclass

# --- text ------------------------------------------------------------------------------------


def search_key(text: str) -> str:
    """Lowercase, accents and punctuation gone, single spaces: how a term is compared."""
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", plain).split())


def _unsegmented(text: str) -> bool:
    """Scripts written without spaces between words (Chinese, Japanese, Thai): match inside."""
    return any("぀" <= c <= "ヿ" or "㐀" <= c <= "鿿" or "฀" <= c <= "๿" for c in text)


def _same_word(a: str, b: str) -> bool:
    """The same word, allowing a regular plural either way (puzzle/puzzles, match/matches)."""
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return long in (short + "s", short + "es") or (
        short.endswith("y") and long == short[:-1] + "ies"
    )


def _has_word(tokens: list[str], haystack: str, word: str) -> bool:
    return word in haystack if _unsegmented(word) else any(_same_word(t, word) for t in tokens)


def _has_phrase(tokens: list[str], haystack: str, words: list[str], phrase: str) -> bool:
    if _unsegmented(phrase):
        return phrase in haystack
    return any(
        all(i + k < len(tokens) and _same_word(tokens[i + k], w) for k, w in enumerate(words))
        for i in range(len(tokens))
    )


EXACT, ALL_WORDS, PARTIAL = 1.0, 0.85, 0.5


@dataclass(frozen=True)
class Match:
    strong: bool  # every word of the term is in the title
    evidence: float  # 1 for the whole phrase, down to 0 for none of its words
    exact: bool  # the whole phrase, word for word


def title_match(title: str, keyword: str) -> Match:
    """How well an app's title matches a search term."""
    phrase, haystack = search_key(keyword), search_key(title)
    if not phrase or not haystack:
        return Match(False, 0.0, False)
    tokens, words = haystack.split(), phrase.split()
    if _has_phrase(tokens, haystack, words, phrase):
        return Match(True, EXACT, True)
    if len(words) > 1 and any(t.startswith("".join(words)) for t in tokens):
        return Match(True, EXACT, False)  # run together: "wordsearch" for "word search"
    present = sum(_has_word(tokens, haystack, w) for w in words)
    if present == len(words):
        # all the words, closer together counting for more
        spots = [
            next((i for i, t in enumerate(tokens) if _same_word(t, w)), None) for w in set(words)
        ]
        closeness = 0.0
        if len(spots) > 1 and None not in spots:
            closeness = min(1.0, len(spots) / (max(spots) - min(spots) + 1))
        return Match(True, ALL_WORDS + (1 - ALL_WORDS) * closeness, False)
    return Match(False, PARTIAL * present / len(words), False)


def targets(title: str, keyword: str) -> bool:
    """The title carries the term as written, from the start of a word: an app aiming at it."""
    phrase, haystack = search_key(keyword), search_key(title)
    if not phrase:
        return False
    return phrase in haystack if _unsegmented(phrase) else f" {phrase}" in f" {haystack}"


# --- the results page --------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    title: str
    ratings: int | None


STRENGTH_FLOOR, STRENGTH_CEILING = 1_000, 1_000_000  # ratings counted as no and full strength


def strength(ratings: int | None) -> float | None:
    """0 to 1: an app's rating count on a log scale from 1,000 to 1,000,000."""
    if ratings is None:
        return None
    if ratings <= 0:
        return 0.0
    span = math.log10(STRENGTH_CEILING / STRENGTH_FLOOR)
    return min(1.0, max(0.0, math.log10(ratings / STRENGTH_FLOOR) / span))


def page_strength(page: list[Result]) -> float:
    values = [s for r in page if (s := strength(r.ratings)) is not None]
    return sum(values) / len(values) if values else 0.0


def targeting(page: list[Result], keyword: str) -> float:
    return sum(targets(r.title, keyword) for r in page) / len(page) if page else 0.0


# Difficulty: a base, how strong the first page is, and how many of its titles aim at the term
DIFFICULTY_BASE, DIFFICULTY_STRENGTH, DIFFICULTY_TARGETING = 13, 40, 26


def difficulty(top: list[Result], keyword: str) -> int | None:
    """0-100 from the top 10 results. None with no results to judge."""
    page = top[:10]
    if not page:
        return None
    total = (
        DIFFICULTY_BASE
        + DIFFICULTY_STRENGTH * page_strength(page)
        + DIFFICULTY_TARGETING * targeting(page, keyword)
    )
    return min(100, max(1, int(total)))


# --- search suggestions ------------------------------------------------------------------------


PREFIX_CAP = 8  # suggestions are looked up for at most the first 8 characters


@dataclass(frozen=True)
class Reach:
    """How the store's suggestions offer a term: ``hit`` when typing its first
    ``prefix`` characters already suggests it, ``listed`` when only the whole term does,
    ``absent`` when not even that does, ``unknown`` before any look."""

    status: str  # hit | listed | absent | unknown
    prefix: int | None = None
    position: int | None = None


def continues(suggestion: str, keyword: str) -> bool:
    s, k = search_key(suggestion), search_key(keyword)
    return s == k or s.startswith(k + " ")


# --- popularity ------------------------------------------------------------------------------

MODEL_DEPTH = 25  # results the model reads
EXACT_LEADER_DEPTH = 5
MAX_WORDS = 6
EXACT_HEAD = 5  # log10 of 100k ratings: an exact title match that strong marks a head term
NEUTRAL_CONTINUATIONS = 5  # what the model assumes before suggestions are looked up
THIN_RESULTS, THIN_CAP = 5, 10  # fewer results than this cap popularity at 10 of 100

# The App Store popularity model, fitted against the Apple Ads top
# search terms (US, week of 2026-09-20): popularity = expm1(intercept + sum(w * feature)).
WEIGHTS = {
    "intercept": 1.6869, "leader": 0.1742, "depth": 0.056, "titled": 0.3249, "exact": 0.4516,
    "exact_leader": 0.0473, "words": 0.0476, "relevance": -0.0247, "weighted_leader": -0.1278,
    "results": -0.4976, "exact_head": -0.0294, "continuations": 0.644, "suggested": -0.1221,
    "early": 0.9675,
}  # fmt: skip


def _magnitude(count: float) -> float:
    return math.log10(1 + max(0.0, count))


def popularity_features(
    page: list[Result], keyword: str, reach: Reach, continuations: int | None
) -> dict[str, float] | None:
    """What the model reads: how strong and how on-topic the first results are, and how
    readily the store suggests the term. None with no results."""
    page = page[:MODEL_DEPTH]
    if not page:
        return None
    matches = [title_match(r.title, keyword) for r in page]
    ratings = [r.ratings for r in page if r.ratings is not None]
    half = page[: max(1, len(page) // 2)]
    leader = _magnitude(max([r.ratings or 0 for r in half], default=0))
    exact_leader = _magnitude(
        max(
            [
                r.ratings or 0
                for r, m in zip(page[:EXACT_LEADER_DEPTH], matches, strict=False)
                if m.exact
            ],
            default=0,
        )
    )
    relevance = sum(m.evidence for m in matches) / len(page)
    words = len(search_key(keyword).split())
    return {
        "leader": leader,
        "depth": _magnitude(statistics.median(ratings) if ratings else 0),
        "titled": sum(m.strong for m in matches) / len(page),
        "exact": sum(m.exact for m in matches) / len(page),
        "exact_leader": exact_leader,
        "words": min(max(words, 1), MAX_WORDS) - 1,
        "relevance": relevance,
        "weighted_leader": leader * relevance,
        "results": len(page) / MODEL_DEPTH,
        "exact_head": max(0.0, exact_leader - EXACT_HEAD) ** 2,
        "continuations": _magnitude(
            NEUTRAL_CONTINUATIONS if continuations is None else continuations
        ),
        "suggested": 0.0 if reach.status == "absent" else 1.0,
        "early": (PREFIX_CAP + 1 - min(reach.prefix, PREFIX_CAP)) / PREFIX_CAP
        if reach.status == "hit" and reach.prefix
        else 0.0,
    }


def popularity(
    page: list[Result], keyword: str, results: int, reach: Reach, continuations: int | None
) -> int | None:
    """Estimated search popularity, 1-100 (0 with no results). ``results`` is how many
    the search returned in all; ``page`` its first ones."""
    if results == 0:
        return 0
    features = popularity_features(page, keyword, reach, continuations)
    if features is None:
        return None
    raw = math.expm1(WEIGHTS["intercept"] + sum(WEIGHTS[k] * v for k, v in features.items()))
    score = round(min(100, max(1, raw)))
    return min(score, THIN_CAP) if results < THIN_RESULTS else score


# --- opportunity -----------------------------------------------------------------------------

SUFFICIENT_VOLUME, REACH_CURVE = 30, 1.25


def opportunity(volume: int | None, difficulty100: int | None) -> int | None:
    """0-100: enough searches (full credit from 30) times how open the first page is."""
    if volume is None or difficulty100 is None:
        return None
    reach = min(1.0, max(0.0, volume / SUFFICIENT_VOLUME)) ** REACH_CURVE
    return int(reach * (100 - min(100, max(0, difficulty100))))
