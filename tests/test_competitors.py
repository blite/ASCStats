from datetime import date, timedelta

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from ascstats.aso import competitors
from ascstats.aso.models import Competitor, CompetitorRelease, CompetitorSnapshot, ListingChange
from ascstats.stats.cache import data_version
from ascstats.stats.models import App, RatingSnapshot
from ascstats.stats.ratings import snapshot

SNAKE, SUDOKU = 1000000001, 1000000002
RIVAL = 555000111
DAY = date(2026, 10, 10)


def listing(version="4.0", released="2026-09-01", notes="Faster."):
    return {
        "trackId": RIVAL, "trackName": "Real Rival", "sellerName": "Rival Co",
        "artworkUrl512": "https://is1-ssl.mzstatic.com/rival/512x512bb.jpg",
        "primaryGenreName": "Games", "formattedPrice": "Free", "version": version,
        "releaseDate": "2024-02-01T08:00:00Z", "currentVersionReleaseDate": f"{released}T07:00:00Z",
        "releaseNotes": notes, "userRatingCount": 200, "averageUserRating": 4.25,
    }  # fmt: skip


def transport(answer, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        ids = request.url.params["id"].split(",")
        return httpx.Response(
            200, json={"results": [r for r in answer() if str(r["trackId"]) in ids]}
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://apps.apple.com/gb/app/real-rival/id555000111?platform=iphone", (RIVAL, "gb")),
        ("apps.apple.com/app/id555000111", (RIVAL, "us")),
        (" 555000111 ", (RIVAL, "us")),
        ("id555000111", (RIVAL, "us")),
    ],
)
def test_parse_takes_links_and_ids(text, expected):
    assert competitors.parse(text) == expected


def test_parse_refuses_anything_else():
    with pytest.raises(competitors.CompetitorError):
        competitors.parse("Real Rival")


@pytest.mark.django_db
def test_add_reads_the_listing_once_and_shares_competitors(apps, settings):
    settings.ASC_SOURCE = "asc"
    snake, sudoku = apps[0], apps[1]
    calls = []
    rival = competitors.add(
        snake, f"https://apps.apple.com/us/app/x/id{RIVAL}", transport(lambda: [listing()], calls)
    )
    assert (rival.name, rival.seller, rival.price, rival.version) == (
        "Real Rival",
        "Rival Co",
        "Free",
        "4.0",
    )
    assert rival.version_released_on == date(2026, 9, 1)
    assert list(CompetitorRelease.objects.values_list("version", "notes")) == [("4.0", "Faster.")]
    assert competitors.add(sudoku, str(RIVAL)) == rival  # known already: no second lookup
    assert len(calls) == 1
    assert set(rival.tracked_for.all()) == {snake, sudoku}

    for text, message in (
        (str(RIVAL), "already follows"),
        (str(SUDOKU), "one of your own apps"),
    ):
        with pytest.raises(competitors.CompetitorError, match=message):
            competitors.add(snake, text, transport(lambda: [listing()]))
    with pytest.raises(competitors.CompetitorError, match="No app with the ID 123456789"):
        competitors.add(snake, "123456789", transport(lambda: []))

    competitors.remove(snake, rival)
    assert list(rival.tracked_for.all()) == [sudoku]


@pytest.mark.django_db
def test_add_stops_at_the_cap(apps, settings):
    settings.ASC_SOURCE = "asc"
    snake = apps[0]
    for i in range(competitors.MAX_PER_APP):
        Competitor.objects.create(apple_id=900000000 + i, name=f"R{i}").tracked_for.add(snake)
    cap = f"already follows {competitors.MAX_PER_APP}"
    with pytest.raises(competitors.CompetitorError, match=cap):
        competitors.add(snake, str(RIVAL), transport(lambda: [listing()]))


@pytest.mark.django_db
def test_add_many_looks_up_new_ones_together(apps, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    monkeypatch.setattr("ascstats.store.itunes.time.sleep", lambda _: None)
    monkeypatch.setattr("ascstats.store.itunes.LOOKUP_IDS", 40)
    snake, sudoku = apps[0], apps[1]
    Competitor.objects.create(apple_id=RIVAL, name="Real Rival").tracked_for.add(sudoku)
    others = [dict(listing(), trackId=600000000 + i, trackName=f"R{i}") for i in range(60)]
    calls = []
    added, refused = competitors.add_many(
        snake,
        [str(RIVAL), *(str(r["trackId"]) for r in others), "600000999", str(SUDOKU), "nonsense"],
        transport(lambda: others, calls),
    )
    assert len(added) == 61  # the shared one too, without asking for it
    assert len(calls) == 3  # 61 new IDs in two batches, then the one Lookup did not know
    assert str(RIVAL) not in calls[0].url.params["id"]
    assert {m.split(":")[0] for m in refused} == {"600000999", str(SUDOKU), "nonsense"}
    assert snake.competitors.count() == 61


@pytest.mark.django_db
def test_competitors_ride_in_the_daily_snapshot(apps, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    monkeypatch.setattr("ascstats.stats.ratings.time.sleep", lambda _: None)
    monkeypatch.setattr("ascstats.stats.ratings.storefronts", lambda end: ["de", "us"])
    rival = Competitor.objects.create(apple_id=RIVAL, name="Real Rival")
    rival.tracked_for.add(apps[0])
    current = {"listing": listing()}

    def answer():
        return [
            {"trackId": SNAKE, "userRatingCount": 84, "averageUserRating": 4.5},
            current["listing"],
        ]

    calls = []
    snapshot(DAY, transport=transport(answer, calls), pause=0, every_storefront=True)
    # in the US request only: no separate requests, and none in the other storefronts
    riding = {c.url.params["country"] for c in calls if str(RIVAL) in c.url.params["id"]}
    assert riding == {"us"}
    assert {c.url.params["country"] for c in calls} == {"de", "us"}  # your app is in both
    assert list(CompetitorSnapshot.objects.values_list("storefront", flat=True)) == ["us"]
    us = CompetitorSnapshot.objects.get(competitor=rival, date=DAY, storefront="us")
    assert (us.count, us.star_sum, us.version) == (200, 850, "4.0")
    assert RatingSnapshot.objects.filter(date=DAY).count() == 2

    current["listing"] = listing("4.1", "2026-10-11", "Dark mode.")
    snapshot(date(2026, 10, 11), transport=transport(answer), pause=0, every_storefront=True)
    rival.refresh_from_db()
    assert rival.version == "4.1"
    assert list(rival.releases.values_list("version", "first_seen")) == [
        ("4.0", DAY), ("4.1", date(2026, 10, 11)),
    ]  # fmt: skip


@pytest.fixture
def seeded(db):
    call_command("seed_synthetic", "--days", "40")


def test_competitors_tab_compares_on_synthetic_data(client, seeded):
    page = client.get(f"/app/{SNAKE}/competitors/").content.decode()
    assert "Synthetic Worm Rush" in page
    assert "Synthetic Slither Classic" in page
    assert "Synthetic Number Place" not in page  # Sudoku's competitor, not Snake's
    assert "New ratings per week" in page
    assert "Releases" in page
    snake = App.objects.get(apple_id=SNAKE)
    data = competitors.compare(snake)
    you, worm = (
        data["members"][0],
        next(m for m in data["members"] if m.name == "Synthetic Worm Rush"),
    )
    assert you.you
    assert not worm.you
    assert worm.count > 0
    assert worm.new > 0
    assert worm.average is not None
    assert len(worm.weekly) == competitors.WEEKS
    assert worm.version  # current version from its listing
    lanes = {lane["member"].name: lane["dots"] for lane in data["timeline"]["lanes"]}
    # 40 days of snapshots only ever saw 3.2.1: a competitor's history starts with following
    assert [d["label"] for d in lanes["Synthetic Worm Rush"]] == ["3.2.1"]
    rivals = data["members"][1:]
    assert [m.new for m in rivals] == sorted((m.new for m in rivals), reverse=True)


@pytest.mark.django_db
def test_only_the_busiest_competitors_are_charted(seeded):
    snake = App.objects.get(apple_id=SNAKE)
    for i in range(8):
        Competitor.objects.create(apple_id=900000000 + i, name=f"Quiet {i}").tracked_for.add(snake)
    data = competitors.compare(snake)
    assert len(data["members"]) == snake.competitors.count() + 1  # the table has every one
    charted = data["charted"]
    assert 1 < len(charted) <= competitors.CHARTED + 1
    assert not any(m.name.startswith("Quiet") for m in charted)  # no ratings: not charted
    assert len(data["timeline"]["lanes"]) == len(charted)
    assert {s["member"].name for s in data["weekly"]["series"]} <= {m.name for m in charted}


def test_manage_page_adds_and_removes(client, seeded):
    snake = App.objects.get(apple_id=SNAKE)
    page = client.get(f"/app/{SNAKE}/competitors/manage/")
    assert b"csrfmiddlewaretoken" in page.content
    before = data_version()
    response = client.post(f"/app/{SNAKE}/competitors/manage/", {"app": "1000000205"}, follow=True)
    assert b"Following Synthetic Star Chart Lite" in response.content
    assert snake.competitors.filter(apple_id=1000000205).exists()
    assert data_version() != before  # the cached Competitors tab is rebuilt
    bad = client.post(f"/app/{SNAKE}/competitors/manage/", {"app": "not an app"})
    assert b"Paste an App Store link" in bad.content
    client.post(f"/app/{SNAKE}/competitors/manage/", {"remove": "1000000205"})
    assert not snake.competitors.filter(apple_id=1000000205).exists()
    with override_settings(DEMO_SITE=True):
        assert client.get(f"/app/{SNAKE}/competitors/manage/").status_code == 404


def test_add_competitor_command(seeded):
    call_command("add_competitor", str(SUDOKU), "1000000204")
    assert App.objects.get(apple_id=SUDOKU).competitors.filter(apple_id=1000000204).exists()
    call_command("add_competitor", str(SUDOKU), "1000000204", "--remove")
    with pytest.raises(CommandError, match="one of your own apps"):
        call_command("add_competitor", str(SUDOKU), str(SNAKE))
    call_command("add_competitor", str(SUDOKU), "1000000204", "1000000205")
    assert (
        App.objects.get(apple_id=SUDOKU)
        .competitors.filter(apple_id__in=[1000000204, 1000000205])
        .count()
        == 2
    )


def test_competitors_tab_links_each_competitor_to_its_page_and_store(client, seeded):
    page = client.get(f"/app/{SNAKE}/competitors/").content.decode()
    snake = App.objects.get(apple_id=SNAKE)
    for rival in snake.competitors.all():
        assert f'href="/app/{SNAKE}/competitors/{rival.apple_id}/"' in page
        assert f'href="{rival.store_url}"' in page
    assert 'data-sort-table="competitors"' in page
    for key in ("name", "count", "average", "new", "perday", "newaverage", "released", "price"):
        assert f'data-sort="{key}"' in page


def test_a_competitors_page_shows_its_ranks_and_history(client, seeded):
    snake = App.objects.get(apple_id=SNAKE)
    worm = snake.competitors.get(name="Synthetic Worm Rush")
    page = client.get(f"/app/{SNAKE}/competitors/{worm.apple_id}/")
    assert page.status_code == 200
    html = page.content.decode()
    assert "Synthetic Worm Rush" in html
    assert f'href="{worm.store_url}"' in html
    assert "Search ranks" in html
    assert "History" in html
    assert f"Version {worm.version}" in html  # the release first seen when following began

    data = competitors.profile(snake, worm)
    placings = data["ranks"]["placings"]
    assert placings, "the synthetic searches place it somewhere"
    assert [p.rank for p in placings] == sorted(p.rank for p in placings)
    tracked = set(snake.keywords.values_list("pk", flat=True))
    assert all(p.tracked == (p.keyword.pk in tracked) for p in placings)
    dates = [e["date"] for e in data["history"]]
    assert dates == sorted(dates, reverse=True)
    assert data["releases"] == worm.releases.count()

    # Only the app's own competitors have a page under it
    other = Competitor.objects.exclude(tracked_for=snake).first()
    assert client.get(f"/app/{SNAKE}/competitors/{other.apple_id}/").status_code == 404
    assert client.get(f"/app/{SNAKE}/competitors/123/").status_code == 404


def test_history_merges_releases_and_listing_changes(apps):
    rival = Competitor.objects.create(apple_id=555000222, name="Rival")
    rival.tracked_for.add(apps[0])
    day = date(2026, 10, 1)
    rival.releases.create(version="1.0", released_on=day, notes="First", first_seen=day)
    rival.releases.create(
        version="1.1", released_on=day + timedelta(days=5), first_seen=day + timedelta(days=6)
    )
    ListingChange.objects.create(
        competitor=rival, date=day + timedelta(days=6), field="name", before="Rival",
        after="Rival: Spades", storefront="us", version="1.1",
    )  # fmt: skip
    events = competitors.history(rival)
    assert [(e["kind"], e["version"]) for e in events] == [
        ("changes", "1.1"), ("release", "1.1"), ("release", "1.0"),
    ]  # fmt: skip
    assert events[0]["items"][0]["gained"] == ["spades"]


@pytest.mark.parametrize(
    ("price", "value"),
    [("Free", 0.0), ("Gratis", 0.0), ("$4.99", 4.99), ("4,99 €", 4.99), ("¥1,200", 1200.0),
     ("", None)],
)  # fmt: skip
def test_price_value_sorts_prices(price, value):
    assert competitors.price_value(price) == value


@pytest.mark.django_db
def test_reading_all_stores_snapshots_each_one_and_notes_where_it_is_not_sold(apps, settings):
    settings.ASC_SOURCE = "asc"
    snake = apps[0]
    rival = Competitor.objects.create(apple_id=RIVAL, name="Real Rival")
    rival.tracked_for.add(snake)
    CompetitorSnapshot.objects.create(
        competitor=rival, date=DAY - timedelta(days=1), storefront="us", count=190, star_sum=800
    )
    RatingSnapshot.objects.create(app=snake, date=DAY, storefront="de", count=12, star_sum=54)
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        front = request.url.params["country"]
        asked.append(front)
        if front == "br":
            return httpx.Response(200, json={"results": []})  # not sold in Brazil
        count = {"us": 200, "de": 40, "gb": 9, "it": 0}.get(front, 1)
        return httpx.Response(200, json={"results": [
            {"trackId": RIVAL, "userRatingCount": count, "averageUserRating": 4.5}
        ]})  # fmt: skip

    assert competitors.read_stores(rival, DAY, httpx.MockTransport(handler), pause=0) == (
        "11 of 12 storefronts"
    )
    assert asked == competitors.STORES  # the US too: one followed since the daily snapshot
    found = competitors.stores(snake, rival)
    rows = {r.code: r for r in found["rows"]}
    assert [r.code for r in found["empty"]] == ["it"]  # no ratings: counted, not a row
    assert [r.code for r in found["missing"]] == ["br"]  # not sold there
    assert list(rows)[:3] == ["us", "de", "gb"]  # most ratings first
    assert set(rows) == set(competitors.STORES) - {"it", "br"}
    assert (rows["us"].count, rows["de"].count, rows["gb"].count) == (200, 40, 9)
    assert rows["de"].average == 4.5
    assert (rows["de"].you_count, rows["de"].you_average) == (12, 4.5)
    rival.refresh_from_db()
    assert rival.stores_read_on == DAY

    # The comparison stays on the US store
    assert competitors.profile(snake, rival)["member"].count == 200


def test_the_read_all_stores_button_queues_a_job(client, seeded):
    from django_tasks_db.models import DBTaskResult

    from ascstats.aso import jobs

    snake = App.objects.get(apple_id=SNAKE)
    worm = snake.competitors.get(name="Synthetic Worm Rush")
    page = client.get(f"/app/{SNAKE}/competitors/{worm.apple_id}/").content.decode()
    assert "Ratings by store" in page
    assert ('class="lang' in page) == bool(worm.languages)
    assert "Read all stores" in page
    url = f"/app/{SNAKE}/competitors/{worm.apple_id}/stores/"
    job = client.post(url).json()["job"]
    queued = DBTaskResult.objects.get(task_path=jobs.read_stores.module_path)
    assert (str(queued.id), queued.args_kwargs["args"]) == (job, [worm.pk])
    assert queued.priority < jobs.search_keyword.priority
    assert queued.priority < jobs.scan_competitor.priority  # keyword jobs first
    assert client.post(url).json()["job"] == job  # a second click waits for the same read
    assert client.get(url, {"job": job}).json()["done"] is False
    assert client.get(url, {"job": "nonsense"}).json()["done"] is True

    # Once a day per competitor: the job skips one read today, and the button says so
    assert jobs.read_stores.call(worm.pk) != "read today"
    assert jobs.read_stores.call(worm.pk) == "read today"
    refused = client.post(url)
    assert refused.status_code == 429
    assert "Read today" in refused.json()["error"]
    assert (
        "Read today</span>"
        in client.get(f"/app/{SNAKE}/competitors/{worm.apple_id}/").content.decode()
    )


def test_store_reads_stop_at_the_days_limit(seeded, monkeypatch):
    monkeypatch.setattr(competitors, "STORE_READS_PER_DAY", 1)
    first, second = App.objects.get(apple_id=SNAKE).competitors.all()[:2]
    job = competitors.start_store_read(first)
    with pytest.raises(competitors.CompetitorError, match="the day's limit"):
        competitors.start_store_read(second)
    assert competitors.start_store_read(first) == job  # one already waiting is not a new read


def test_a_competitors_page_lists_its_languages_yours_first(apps):
    rival = Competitor.objects.create(
        apple_id=RIVAL, name="Real Rival", languages=["DE", "EN", "JA", "ZH", "XX"]
    )
    found = competitors.languages(rival)
    assert [(r["name"], r["yours"]) for r in found] == [
        ("English", True), ("German", True), ("Chinese", False), ("Japanese", False),
        ("XX", False),
    ]  # fmt: skip


@pytest.mark.django_db
def test_every_other_storefront_is_read_once_a_week(apps, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    fronts = ["us", "de", "fr", "jp", "br", "kr", "mx", "it", "ca", "au", "gb", "es"]
    monkeypatch.setattr("ascstats.stats.ratings.storefronts", lambda end: fronts)
    rival = Competitor.objects.create(apple_id=RIVAL, name="Real Rival")
    rival.tracked_for.add(apps[0])
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["country"])
        return httpx.Response(200, json={"results": [
            {"trackId": RIVAL, "userRatingCount": 10, "averageUserRating": 4.0}
        ]})  # fmt: skip

    week = [DAY + timedelta(days=i) for i in range(7)]
    for day in week:
        competitors.read_weekly(day, transport=httpx.MockTransport(handler), pause=0)
    # every storefront but the US exactly once in the week, a few each day
    assert sorted(asked) == sorted(f for f in fronts if f != "us")
    by_day = CompetitorSnapshot.objects.values_list("date", "storefront")
    assert {f for _, f in by_day} == set(fronts) - {"us"}
    # a retry the same day asks nothing it saved already
    asked.clear()
    competitors.read_weekly(week[0], transport=httpx.MockTransport(handler), pause=0)
    assert asked == []


def test_the_weekly_read_runs_after_the_keyword_searches():
    from ascstats.aso import keywords
    from ascstats.stats import extensions

    steps = extensions.daily_store_steps
    assert steps[-1] == competitors.read_weekly
    assert steps.index(keywords.snapshot) < steps.index(competitors.read_weekly)


@pytest.mark.django_db
def test_store_rows_older_than_two_weeks_are_not_shown(apps):
    snake = apps[0]
    rival = Competitor.objects.create(apple_id=RIVAL, name="Real Rival")
    rival.tracked_for.add(snake)
    old = DAY - timedelta(days=competitors.FRESH_DAYS + 1)
    for front, day in (("us", DAY), ("de", DAY - timedelta(days=6)), ("fr", old)):
        CompetitorSnapshot.objects.create(
            competitor=rival, date=day, storefront=front, count=5, star_sum=20
        )
    rows = {r.code: r for r in competitors.stores(snake, rival)["rows"]}
    assert set(rows) == {"us", "de"}  # France's row is from a store no longer read


@pytest.mark.django_db
def test_ratings_by_store_covers_every_store_ten_at_a_time(client, apps):
    snake = apps[0]
    rival = Competitor.objects.create(apple_id=RIVAL, name="Real Rival")
    rival.tracked_for.add(snake)
    fronts = ["us", "gb", "de", "fr", "jp", "kr", "au", "ca", "br", "mx", "it", "es", "nl"]
    for i, front in enumerate(fronts):
        CompetitorSnapshot.objects.create(
            competitor=rival, date=DAY, storefront=front, count=100 - i, star_sum=400
        )
    CompetitorSnapshot.objects.create(
        competitor=rival, date=DAY, storefront="se", count=0, star_sum=0
    )
    found = competitors.stores(snake, rival)
    assert [r.code for r in found["rows"]] == fronts  # all of them, most ratings first
    assert [r.code for r in found["empty"]] == ["se"]
    html = client.get(f"/app/{SNAKE}/competitors/{RIVAL}/").content.decode()
    table = html.split('<table class="stores"')[1].split("</table>")[0]
    assert table.count("<tr hidden>") == len(fronts) - competitors.STORES_PER_PAGE
    assert "10 of 13</span>" in html
    assert "none in 1 more" in html
