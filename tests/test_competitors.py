from datetime import date

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from ascstats.aso import competitors
from ascstats.aso.models import Competitor, CompetitorRelease, CompetitorSnapshot
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
    assert all(str(RIVAL) in c.url.params["id"] for c in calls)  # no separate requests
    us = CompetitorSnapshot.objects.get(competitor=rival, date=DAY, storefront="us")
    assert (us.count, us.star_sum, us.version) == (200, 850, "4.0")
    assert RatingSnapshot.objects.filter(date=DAY, storefront="us").count() == 1

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
