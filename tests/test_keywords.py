import re
from datetime import date
from pathlib import Path

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

import ascstats.stats
from ascstats.aso import keywords
from ascstats.aso.models import Competitor, Keyword, KeywordSnapshot
from ascstats.stats.cache import data_version
from ascstats.stats.models import App

SNAKE, SUDOKU = 1000000001, 1000000002
DAY = date(2026, 10, 10)


def test_stats_never_imports_aso():
    """stats is the base: aso plugs into it through stats.extensions, never the reverse."""
    root = Path(ascstats.stats.__file__).parent
    offenders = [
        p.relative_to(root)
        for p in root.rglob("*.py")
        if re.search(r"\bascstats\.aso\b|from \.\.aso", p.read_text())
    ]
    assert offenders == []


@pytest.mark.django_db
def test_add_normalizes_shares_and_caps(apps):
    snake, sudoku = apps[0], apps[1]
    added = keywords.add(snake, "Snake Game\n  classic   snake ,snake game")
    assert [k.term for k in added] == ["snake game", "classic snake"]
    assert keywords.add(sudoku, "snake game")[0] == added[0]  # one keyword, one search a day
    with pytest.raises(keywords.KeywordError, match="already tracks"):
        keywords.add(snake, "SNAKE GAME")
    assert keywords.add(snake, "snake game", "gb")[0].storefront == "gb"  # another store
    with pytest.raises(keywords.KeywordError, match="storefront"):
        keywords.add(snake, "snake", "usa")
    with pytest.raises(keywords.KeywordError, match="room for"):
        keywords.add(snake, "\n".join(f"term {i}" for i in range(keywords.MAX_PER_APP)))
    keywords.remove(snake, added[0])
    assert list(added[0].tracked_for.all()) == [sudoku]


def results(*ids):
    return [{"trackId": i, "trackName": f"App {i}", "sellerName": "S", "userRatingCount": 9}
            for i in ids]  # fmt: skip


@pytest.mark.django_db
def test_daily_search_keeps_the_whole_order(apps, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    monkeypatch.setattr("ascstats.store.itunes.time.sleep", lambda _: None)
    snake = apps[0]
    keywords.add(snake, "snake game\nworm game")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/search"
        assert request.url.params["entity"] == "software"
        if request.url.params["term"] == "worm game" and len(calls) < 4:
            return httpx.Response(503)  # refused, then answered on a retry
        return httpx.Response(200, json={"results": results(42, SNAKE, 7)})

    assert keywords.snapshot(DAY, transport=httpx.MockTransport(handler), pause=0) == 2
    snap = KeywordSnapshot.objects.get(keyword__term="snake game", date=DAY)
    assert snap.ids == [42, SNAKE, 7]
    assert (snap.rank(SNAKE), snap.rank(99)) == (2, None)
    assert snap.top[0] == {"id": 42, "name": "App 42", "seller": "S", "icon": "", "ratings": 9,
                           "average": None}  # fmt: skip
    asked = len(calls)
    assert keywords.snapshot(DAY, transport=httpx.MockTransport(handler), pause=0) == 0
    assert len(calls) == asked  # a rerun only searches what is missing


@pytest.mark.django_db
def test_two_failed_searches_stop_the_run(apps, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    monkeypatch.setattr("ascstats.store.itunes.time.sleep", lambda _: None)
    keywords.add(apps[0], "a b\nc d\ne f")
    refuse = httpx.MockTransport(lambda request: httpx.Response(429))
    assert keywords.snapshot(DAY, transport=refuse, pause=0) == 0
    assert not KeywordSnapshot.objects.exists()


@pytest.fixture
def seeded(db):
    call_command("seed_synthetic", "--days", "30")


def test_keywords_tab_ranks_you_and_your_competitors(client, seeded):
    snake = App.objects.get(apple_id=SNAKE)
    board = keywords.board(snake)
    terms = {(r.keyword.term, r.keyword.storefront) for r in board["rows"]}
    assert terms == {("snake game", "us"), ("classic snake", "us"), ("worm game", "us"),
                     ("snake game", "gb")}  # fmt: skip
    ranked = [r for r in board["rows"] if r.rank]
    assert ranked
    assert ranked == sorted(ranked, key=lambda r: r.rank)
    assert all(r.spark for r in ranked)
    assert any(r.rival for r in board["rows"])  # its synthetic competitors show up too
    page = client.get(f"/app/{SNAKE}/keywords/").content.decode()
    assert "snake game" in page
    assert 'hx-get="/app/{app}/keywords/{keyword}/"' in page
    k = Keyword.objects.get(term="snake game", storefront="us")
    detail = client.get(f"/app/{SNAKE}/keywords/{k.pk}/").content.decode()
    assert "You and your competitors" in detail
    assert "Synthetic Worm Rush" in detail
    sudoku_kw = Keyword.objects.get(term="daily sudoku")
    assert client.get(f"/app/{SNAKE}/keywords/{sudoku_kw.pk}/").status_code == 404


def test_manage_keywords_page(client, seeded):
    snake = App.objects.get(apple_id=SNAKE)
    assert b"csrfmiddlewaretoken" in client.get(f"/app/{SNAKE}/keywords/manage/").content
    before = data_version()
    done = client.post(
        f"/app/{SNAKE}/keywords/manage/", {"terms": "Snake Arcade", "storefront": "us"}, follow=True
    )
    assert "Tracking “snake arcade”" in done.content.decode()
    assert data_version() != before  # the cached Keywords tab is rebuilt
    added = snake.keywords.get(term="snake arcade")
    page = client.get(f"/app/{SNAKE}/keywords/").content.decode()
    assert "next search" in page  # tracked, not searched yet
    client.post(f"/app/{SNAKE}/keywords/manage/", {"remove": added.pk})
    assert not snake.keywords.filter(pk=added.pk).exists()
    with override_settings(DEMO_SITE=True):
        assert client.get(f"/app/{SNAKE}/keywords/manage/").status_code == 404


def test_add_keyword_command(seeded):
    call_command("add_keyword", str(SUDOKU), "number puzzle", "--storefront", "gb")
    sudoku = App.objects.get(apple_id=SUDOKU)
    assert sudoku.keywords.filter(term="number puzzle", storefront="gb").exists()
    call_command("add_keyword", str(SUDOKU), "Number Puzzle", "--storefront", "gb", "--remove")
    assert not sudoku.keywords.filter(term="number puzzle").exists()
    with pytest.raises(CommandError, match="does not track"):
        call_command("add_keyword", str(SUDOKU), "nothing", "--remove")


def test_seeding_follows_rivals_and_tracks_keywords(seeded):
    assert Competitor.objects.filter(tracked_for__apple_id=SNAKE).count() == 2
    assert KeywordSnapshot.objects.filter(keyword__term="star map").count() == 30
