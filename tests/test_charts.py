from datetime import date, timedelta

import httpx
import pytest

from ascstats import store
from ascstats.aso import charts
from ascstats.aso.models import ChartApp, ChartCapture, Competitor

DAY = date(2026, 11, 10)
PUZZLE = 7012


def entry(apple_id, name="App", price="0.00", summary=""):
    return {
        "im:name": {"label": name},
        "im:image": [{"label": "https://x/53.png"}, {"label": "https://x/100.png"}],
        "summary": {"label": summary},
        "im:price": {"label": "Get", "attributes": {"amount": price, "currency": "USD"}},
        "id": {"label": "https://apps.apple.com/…", "attributes": {"im:id": str(apple_id)}},
        "im:artist": {"label": "Seller"},
        "category": {"attributes": {"im:id": "6014", "label": "Games"}},
        "im:releaseDate": {"label": "2026-05-19T00:00:00-07:00"},
    }


def feed(charts_by_kind, calls=None):
    """A store answering each kind's chart from ``charts_by_kind`` (a kind -> entries)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        kind = next(k for k, f in store.CHART_FEEDS.items() if f in request.url.path)
        entries = charts_by_kind.get(kind, [])
        return httpx.Response(200, json={"feed": {"entry": entries} if entries else {}})

    return httpx.MockTransport(handler)


def test_top_chart_reads_the_feed():
    calls = []
    one = {"free": [entry(5, "Sudoku", summary="No ads, ever.")]}
    one["free"] = one["free"][0]  # a chart of one app is an entry, not a list
    with store.client(feed(one, calls)) as http:
        apps = store.top_chart("free", PUZZLE, "gb", http)
        assert store.top_chart("paid", None, "us", http) == []
    assert apps == [
        {"id": 5, "name": "Sudoku", "seller": "Seller", "icon": "https://x/100.png", "price": 0.0,
         "currency": "USD", "genre": "Games", "released": "2026-05-19", "summary": "No ads, ever."}
    ]  # fmt: skip
    assert calls[0].endswith("/gb/rss/topfreeapplications/limit=100/genre=7012/json")
    assert calls[1].endswith("/us/rss/toppaidapplications/limit=100/json")  # the overall chart


@pytest.mark.parametrize(
    ("summary", "said"),
    [
        ("Relaxing puzzles. No ads, no timers.", "none"),
        ("Completely ad-free and offline.", "none"),
        ("Play without annoying ads!", "none"),
        ("Free to play. Remove ads with a one-time purchase.", "some"),
        ("Go ad-free with Premium.", "some"),
        ("Watch a short video ad for a hint.", "some"),
        ("Non-Intrusive Ads: zero interruptions.", "some"),
        ("No annoying ads after every level.", "some"),
        ("VIP subscription: you will unlock instant hints, no ads, double gifts.", "some"),
        ("One purchase. Unlock the full game with no ads.", "none"),
        ("Hundreds of levels to master.", ""),
        ("Read the news headlines.", ""),  # "headlines" is not "ads"
    ],
)
def test_ads_reads_what_the_description_promises(summary, said):
    assert charts.ads(summary) == said


def test_title_words_count_names_not_mentions():
    words, phrases = charts.title_words([
        "Sudoku - Classic Number Puzzle", "Killer Sudoku: Number Puzzle", "Sudoku Sudoku",
        "Jigsaw Puzzles", "2048",
    ])  # fmt: skip
    assert dict(words) == {"sudoku": 3, "number": 2, "puzzle": 2}  # once per name
    assert dict(phrases) == {"number puzzle": 2}  # never across "-" or ":"


@pytest.mark.django_db
def test_fetch_keeps_one_copy_a_day_and_earlier_days():
    calls = []
    answer = {
        "free": [entry(1, "Sudoku", summary="No ads."), entry(2, "Mahjong")],
        "paid": [entry(3, "Chess", price="2.99")],
    }  # grossing answers empty: a chart with no apps
    charts.fetch("us", PUZZLE, DAY, feed(answer, calls), pause=0)
    assert len(calls) == 3
    caps = {c.kind: c.ids for c in ChartCapture.objects.filter(date=DAY)}
    assert caps == {"free": [1, 2], "paid": [3], "grossing": []}
    chess = ChartApp.objects.get(apple_id=3)
    assert (str(chess.price), chess.released_on, chess.seen_on) == ("2.99", date(2026, 5, 19), DAY)

    answer["free"].reverse()
    charts.fetch("us", PUZZLE, DAY, feed(answer, calls), pause=0)  # a refetch replaces the day's
    assert ChartCapture.objects.get(date=DAY, kind="free").ids == [2, 1]
    charts.fetch("us", PUZZLE, DAY + timedelta(days=1), feed(answer), pause=0)  # another day adds
    assert ChartCapture.objects.filter(kind="free").count() == 2
    assert charts.fetched() == {("us", PUZZLE): DAY + timedelta(days=1)}
    assert charts.latest() == ("us", PUZZLE)
    with pytest.raises(charts.ChartError):
        charts.fetch("zz", PUZZLE, DAY, feed(answer), pause=0)


@pytest.mark.django_db
def test_a_failed_fetch_saves_nothing():
    def refuse(request):
        if "topgrossing" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json={"feed": {"entry": [entry(1)]}})

    with pytest.raises(store.StoreError):
        charts.fetch("us", PUZZLE, DAY, httpx.MockTransport(refuse), pause=0)
    assert not ChartCapture.objects.exists()


def seed(day, kind, ids, genre=PUZZLE):
    for i in ids:
        ChartApp.objects.get_or_create(
            apple_id=i,
            defaults={"name": f"App {i}", "seen_on": day, "summary": "No ads." if i % 2 else ""},
        )
    ChartCapture.objects.create(date=day, storefront="us", genre=genre, kind=kind, ids=ids)


@pytest.mark.django_db
def test_page_compares_with_the_window_before(apps):
    seed(DAY - timedelta(days=8), "free", [10, 11, 12, 13])  # inside the 7-day window
    seed(DAY - timedelta(days=1), "free", [11, 10, 12, 13])
    seed(DAY, "free", [12, 11, 14, apps[0].apple_id])
    rival = Competitor.objects.create(apple_id=11, name="App 11")
    rival.tracked_for.add(apps[1])

    page = charts.page("us", PUZZLE, DAY, 1)
    free = page["columns"][0]
    rows = {r.app.apple_id: r for r in free.rows}
    assert [(r.rank, r.change, r.new) for r in free.rows] == [
        (1, 2, False), (2, -1, False), (3, None, True), (4, None, True)
    ]  # fmt: skip
    assert rows[11].rivals == [apps[1].name]
    assert rows[apps[0].apple_id].you
    assert [(n, a.apple_id) for n, a in free.dropped] == [(2, 10), (4, 13)]
    assert rows[11].ads == "none"
    assert rows[12].ads == ""
    assert page["mover_lists"][0][1][0][1].app.apple_id == 12  # climbed most
    assert page["columns"][1].capture is None  # paid: not captured that day
    assert page["older"] == DAY - timedelta(days=1)
    assert page["newer"] is None

    week = charts.page("us", PUZZLE, DAY, 7)["columns"][0]
    assert week.compare_day == DAY - timedelta(days=8)
    assert charts.page("us", PUZZLE, DAY, 30)["columns"][0].compare_day is None
    assert charts.page("us", PUZZLE)["day"] == DAY  # the newest capture
    with pytest.raises(charts.ChartError):
        charts.page("us", PUZZLE, DAY, 3)


@pytest.mark.django_db
def test_charts_page_and_its_buttons(client, apps, monkeypatch):
    assert client.get("/charts/").url == "/charts/us/6014/"  # nothing fetched yet
    assert "Fetch charts" in client.get("/charts/us/6014/").content.decode()
    seed(DAY, "free", [600000021, 600000022])
    seed(DAY, "paid", [600000022])
    page = client.get(f"/charts/us/{PUZZLE}/").content.decode()
    assert 'Puzzle games: <span class="num">1</span> of the top 2 free say no ads.' in page
    assert "App 600000021" in page
    assert 'data-track="600000021"' in page
    assert client.get(f"/charts/us/{PUZZLE}/{DAY}/7d/").status_code == 200
    assert client.get("/charts/zz/1/").status_code == 404
    assert client.get(f"/charts/us/{PUZZLE}/3d/").status_code == 404

    calls = []
    monkeypatch.setattr("ascstats.aso.charts.time.sleep", lambda _: None)
    real_client = store.client
    monkeypatch.setattr(
        "ascstats.store.client",
        lambda transport=None: real_client(feed({"free": [entry(600000023)]}, calls)),
    )
    assert client.post(f"/charts/gb/{PUZZLE}/fetch/").json() == {"ok": True}
    assert len(calls) == 3
    assert client.get("/charts/").url == f"/charts/gb/{PUZZLE}/"  # the category fetched last
    assert "Refetch" in client.get(f"/charts/gb/{PUZZLE}/").content.decode()
    assert client.post("/charts/zz/1/fetch/").status_code == 404

    answer = client.post("/charts/track/", {"app": apps[0].apple_id, "id": 600000021}).json()
    assert answer == {"name": "App 600000021", "app": apps[0].name}
    assert Competitor.objects.get(apple_id=600000021).tracked_for.get() == apps[0]
    again = client.post("/charts/track/", {"app": apps[0].apple_id, "id": 600000021})
    assert again.status_code == 400
    assert "already follows" in again.json()["error"]
    assert (
        client.post("/charts/track/", {"app": apps[0].apple_id, "id": 600000099}).status_code == 400
    )
