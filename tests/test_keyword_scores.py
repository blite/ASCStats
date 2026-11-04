import plistlib
from datetime import date, timedelta

import httpx
import pytest
from django.core.management import call_command

from ascstats.aso import keywords, scoring, suggestions
from ascstats.aso.models import Keyword, KeywordSuggestion
from ascstats.aso.scoring import Reach, Result
from ascstats.stats.models import App

SNAKE = 1000000001
DAY = date(2026, 10, 10)


def test_search_key_and_title_matching():
    assert scoring.search_key("  Café: Jigsaw-Puzzles! ") == "cafe jigsaw puzzles"
    m = scoring.title_match("Jigsaw Puzzles: No Ads", "jigsaw puzzle")  # plural either way
    assert (m.strong, m.exact, m.evidence) == (True, True, 1.0)
    assert scoring.title_match("Wordsearch Daily", "word search").evidence == 1.0  # run together
    apart = scoring.title_match("Search the Word Pro", "word search")
    assert (apart.strong, apart.exact) == (True, False)
    assert 0.85 <= apart.evidence < 1
    half = scoring.title_match("Word Games", "word search")
    assert (half.strong, half.evidence) == (False, 0.25)
    assert scoring.targets("Euchre: Classic Card Game", "classic card")
    assert not scoring.targets("Neoclassic Cards", "classic card")  # not from a word start
    assert scoring.targets("经典接龙纸牌", "接龙")  # no spaces in Chinese: matched inside


def test_strength_and_difficulty():
    assert scoring.strength(1_000) == 0
    assert scoring.strength(1_000_000) == 1
    assert scoring.strength(31_623) == pytest.approx(0.5, abs=0.001)
    assert scoring.difficulty([], "euchre") is None
    strong = [Result("Euchre Pro", 2_000_000)] * 10
    weak = [Result("Card Games", 50)] * 10
    assert scoring.difficulty(strong, "euchre") == 13 + 40 + 26  # strongest, all aiming at it
    assert scoring.difficulty(weak, "euchre") == 13  # the base: nothing in the way


def test_popularity_reads_suggestions_and_results():
    page = [Result(f"Euchre {i}", 50_000 // (i + 1)) for i in range(25)]

    def pop(reach, continuations=5, results=200):
        return scoring.popularity(page, "euchre", results, reach, continuations)

    early, late = pop(Reach("hit", 1, 1)), pop(Reach("hit", 6, 1))
    assert 1 <= late < early <= 100  # suggested sooner: searched more
    assert pop(Reach("hit", 3, 1), continuations=20) > pop(Reach("hit", 3, 1), continuations=1)
    assert scoring.popularity(page, "euchre", 0, Reach("hit", 1, 1), 5) == 0  # nothing found
    assert pop(Reach("hit", 1, 1), results=3) <= scoring.THIN_CAP  # a thin page caps it
    assert scoring.popularity([], "euchre", 10, Reach("unknown"), None) is None


def test_opportunity():
    assert scoring.opportunity(30, 40) == 60  # enough searches: as open as the page is
    assert scoring.opportunity(15, 40) == 25  # half the searches count for less than half
    assert scoring.opportunity(None, 40) is None


def hint_lists(table):
    return lambda typed: table.get(typed, [])


def test_look_finds_how_early_the_term_is_suggested():
    ask = hint_lists({
        "euchre": ["euchre", "euchre free", "euchre card game", "euchre 3d"],
        "euchre ": ["euchre online", "euchre free"],
        "euc": ["euchre", "eucalyptus"],
    })  # fmt: skip
    found = suggestions.look("euchre", ask)
    assert found == {"reach": "hit", "prefix": 3, "position": 1, "continuations": 5,
                     "requests": 5}  # fmt: skip
    listed = suggestions.look("spades", hint_lists({"spades": ["spider", "spades"]}))
    assert (listed["reach"], listed["position"], listed["prefix"]) == ("listed", 2, None)
    absent = suggestions.look("zzz game", hint_lists({}))
    assert (absent["reach"], absent["requests"]) == ("absent", 2)


@pytest.mark.django_db
def test_probe_asks_the_store_and_spreads_the_week(apps, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    monkeypatch.setattr("ascstats.store.itunes.time.sleep", lambda _: None)
    keywords.add(apps[0], "snake game\nworm game")
    keywords.add(apps[0], "snake game", "zz")  # no storefront number: skipped
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.params["term"], request.headers["X-Apple-Store-Front"]))
        term = request.url.params["term"]
        offered = ["snake game", "snake game 3d"] if term.startswith("sn") else []
        return httpx.Response(
            200, content=plistlib.dumps({"hints": [{"term": t} for t in offered]})
        )

    assert suggestions.probe(DAY, transport=httpx.MockTransport(handler), pause=0) == 2
    assert {front for _, front in seen} == {"143441,29"}
    snake = KeywordSuggestion.objects.get(keyword__term="snake game", keyword__storefront="us")
    assert (snake.reach, snake.prefix, snake.continuations) == ("hit", 2, 2)  # "sn"
    worm = KeywordSuggestion.objects.get(keyword__term="worm game")
    assert worm.reach == "absent"
    assert suggestions.due(DAY + timedelta(days=6)) == [Keyword.objects.get(storefront="zz")]
    assert len(suggestions.due(DAY + timedelta(days=7))) == 3  # a week on: due again


@pytest.mark.django_db
def test_probe_stops_after_two_failures(apps, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    monkeypatch.setattr("ascstats.store.itunes.time.sleep", lambda _: None)
    keywords.add(apps[0], "a b\nc d\ne f")
    refuse = httpx.MockTransport(lambda request: httpx.Response(503))
    assert suggestions.probe(DAY, transport=refuse, pause=0) == 0
    assert not KeywordSuggestion.objects.exists()


def test_keywords_tab_shows_scores(client, db):
    call_command("seed_synthetic", "--days", "10")
    snake = App.objects.get(apple_id=SNAKE)
    rows = keywords.board(snake)["rows"]
    assert all(r.scores.difficulty is not None for r in rows)
    assert all(r.scores.popularity is not None for r in rows)
    assert all(not r.scores.estimated for r in rows)  # the seed looks at the suggestions too
    page = client.get(f"/app/{SNAKE}/keywords/").content.decode()
    for key in ("term", "rank", "change", "best", "popularity", "difficulty", "opportunity"):
        assert f'data-sort="{key}"' in page  # every column sorts
    assert "data-opportunity=" in page
    k = Keyword.objects.get(term="snake game", storefront="us")
    detail = client.get(f"/app/{SNAKE}/keywords/{k.pk}/").content.decode()
    assert "Popularity" in detail
    assert "Difficulty" in detail
    assert "Opportunity" in detail
