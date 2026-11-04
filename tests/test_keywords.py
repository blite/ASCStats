import plistlib
import re
from datetime import date
from pathlib import Path

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

import ascstats.stats
from ascstats.aso import keywords, suggestions, views
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
    with pytest.raises(keywords.KeywordError, match="too general"):
        keywords.add(snake, "snake game\nads")  # nothing added: the whole list is refused
    assert keywords.add(snake, "snake no ads")[0].term == "snake no ads"  # fine in a phrase
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


@pytest.mark.django_db
def test_the_daily_jobs_never_track_a_keyword_on_their_own(apps, settings, monkeypatch):
    """Only a person adds keywords. The searches and suggestions turn up plenty of other
    terms; none of them is tracked, for this app or any other."""
    settings.ASC_SOURCE = "asc"
    monkeypatch.setattr("ascstats.store.itunes.time.sleep", lambda _: None)
    keywords.add(apps[0], "snake game")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": results(42, SNAKE, 7)})
        offered = ["snake game", "snake game offline", "snake io", "worm game", "slither"]
        return httpx.Response(
            200, content=plistlib.dumps({"hints": [{"term": t} for t in offered]})
        )

    transport = httpx.MockTransport(handler)
    assert keywords.snapshot(DAY, transport=transport, pause=0) == 1
    assert suggestions.probe(DAY, transport=transport, pause=0) == 1
    assert list(Keyword.objects.values_list("term", flat=True)) == ["snake game"]
    assert [a.keywords.count() for a in apps] == [1] + [0] * (len(apps) - 1)


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


@pytest.mark.django_db
def test_keyword_tiles_count_the_top_10_and_the_week_s_movement(apps):
    snake, sudoku = apps[0], apps[1]
    week = date(2026, 10, 3)
    searches = {  # term: (ids a week before, ids on DAY); SNAKE's rank is its position
        "climbs into top 10": ([9] * 11 + [SNAKE], [SNAKE]),
        "falls out of top 10": ([SNAKE], [9] * 11 + [SNAKE]),
        "enters the results": ([9], [9, SNAKE]),
        "leaves the results": ([SNAKE], [9]),
        "stays put": ([9, SNAKE], [9, SNAKE]),
    }
    for term, (old, now) in searches.items():
        k = keywords.add(snake, term)[0]
        KeywordSnapshot.objects.create(keyword=k, date=week, ids=old)
        KeywordSnapshot.objects.create(keyword=k, date=DAY, ids=now)
    keywords.add(snake, "never searched")
    k = keywords.add(sudoku, "only today")[0]
    KeywordSnapshot.objects.create(keyword=k, date=DAY, ids=[SUDOKU])
    s = keywords.summary([snake])
    assert (s.tracked, s.top10, s.compared, s.top10_change) == (6, 3, 5, 0)
    assert (s.up, s.down, s.new, s.lost) == (2, 2, 1, 1)
    assert s.date == DAY
    s = keywords.summary([snake, sudoku])
    assert (s.tracked, s.top10, s.compared) == (7, 4, 5)  # no week before: top 10 only
    assert keywords.summary([snake], "gb").tracked == 0


@pytest.mark.django_db
def test_keyword_tiles_on_the_overview_and_the_keywords_tab(client, seeded):
    overview = client.get("/").content.decode()
    assert "Keywords in the top 10" in overview
    assert "Keyword movement" in overview
    tab = client.get(f"/app/{SNAKE}/keywords/us/").content.decode()
    assert "Keywords in the top 10" in tab
    assert "in 7 days" in tab  # 30 seeded days: a week to compare against


@pytest.mark.django_db
def test_storefront_tabs_put_the_us_first_then_the_most_keywords(apps):
    app = apps[0]
    keywords.add(app, "química alberca", "mx")
    keywords.add(app, "piscina", "it")
    keywords.add(app, "chimie piscine\nentretien piscine", "fr")
    keywords.add(app, "pool chemistry", "us")
    fronts = keywords.storefronts(app)
    assert [(f.code, f.count, f.country) for f in fronts] == [
        ("us", 1, "United States"), ("fr", 2, "France"), ("it", 1, "Italy"), ("mx", 1, "Mexico"),
    ]  # fmt: skip
    assert [r.keyword.term for r in keywords.board(app, "fr")["rows"]] == [
        "chimie piscine", "entretien piscine",
    ]  # fmt: skip
    assert keywords.board(app, "de")["rows"] == []  # not tracked there


def test_keywords_tab_has_a_tab_per_storefront(client, seeded):
    snake = App.objects.get(apple_id=SNAKE)
    assert [(f.code, f.count) for f in keywords.storefronts(snake)] == [("us", 3), ("gb", 1)]
    gb = keywords.board(snake, "gb")["rows"]
    assert [(r.keyword.term, r.keyword.storefront) for r in gb] == [("snake game", "gb")]
    assert {r.keyword.storefront for r in keywords.board(snake, "us")["rows"]} == {"us"}

    def terms(page):
        return re.findall(r'name="keyword" value="(\d+)"', page)

    us_kw = {str(k.pk) for k in snake.keywords.filter(storefront="us")}
    gb_kw = {str(k.pk) for k in snake.keywords.filter(storefront="gb")}
    first = client.get(f"/app/{SNAKE}/keywords/").content.decode()
    assert set(terms(first)) == us_kw  # the US first
    assert f'href="/app/{SNAKE}/keywords/us/" aria-current="page"' in first
    assert f'href="/app/{SNAKE}/keywords/gb/"' in first
    page = client.get(f"/app/{SNAKE}/keywords/gb/").content.decode()
    assert set(terms(page)) == gb_kw
    assert f'href="/app/{SNAKE}/keywords/gb/" aria-current="page"' in page
    assert f'"kwFront:{SNAKE}", "gb/"' in page  # remembered per browser, like the app tabs
    assert 'name="storefront" value="gb"' in page  # deleting from here comes back here
    # a storefront it no longer tracks (remembered by a browser) goes back to the first tab
    gone = client.get(f"/app/{SNAKE}/keywords/fr/")
    assert (gone.status_code, gone.url) == (302, f"/app/{SNAKE}/keywords/")
    assert "Tracking <strong>1</strong> of <strong>4</strong> keywords" in page
    # one storefront: a single pill, which names it
    sudoku = client.get(f"/app/{SUDOKU}/keywords/").content.decode()
    assert f'href="/app/{SUDOKU}/keywords/us/" aria-current="page" title="United States"' in sudoku
    assert f"app/{SNAKE}/keywords/gb/" in views.export_paths(snake)  # the static demo has it


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


def test_delete_picked_keywords_from_the_tab(client, seeded):
    snake = App.objects.get(apple_id=SNAKE)
    sudoku = App.objects.get(apple_id=SUDOKU)
    first, second, kept = snake.keywords.order_by("pk")[:3]
    first.tracked_for.add(sudoku)  # shared: only Snake stops tracking it
    page = client.get(f"/app/{SNAKE}/keywords/")
    assert 'form="kwdelete"' in page.content.decode()
    assert "csrftoken" in page.cookies  # the cached tab's form reads its token from here
    assert client.get(f"/app/{SNAKE}/keywords/delete/").status_code == 405
    done = client.post(f"/app/{SNAKE}/keywords/delete/", {"delete": [first.pk, second.pk, "x"]})
    assert done.status_code == 302
    assert done.url == f"/app/{SNAKE}/keywords/"
    assert set(snake.keywords.filter(pk__in=[first.pk, second.pk, kept.pk])) == {kept}
    assert sudoku.keywords.filter(pk=first.pk).exists()
    gb = snake.keywords.get(storefront="gb")
    back = client.post(f"/app/{SNAKE}/keywords/delete/", {"delete": [], "storefront": "gb"})
    assert back.url == f"/app/{SNAKE}/keywords/gb/"  # back to the storefront's tab
    client.post(f"/app/{SNAKE}/keywords/delete/", {"delete": [gb.pk], "storefront": "gb"})
    assert not snake.keywords.filter(storefront="gb").exists()
    page = client.get(f"/app/{SNAKE}/keywords/").content.decode()
    assert f'value="{second.pk}" form="kwdelete"' not in page  # the cached tab was rebuilt
    with override_settings(DEMO_SITE=True):
        assert client.post(f"/app/{SNAKE}/keywords/delete/").status_code == 404
        assert 'form="kwdelete"' not in client.get(f"/app/{SUDOKU}/keywords/").content.decode()


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
