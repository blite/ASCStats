import zlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ascstats.stats.models import App, AppVersion, CustomerReview, RatingSnapshot, ReviewSummary
from ascstats.stats.ratings import (
    current,
    daily_new_ratings,
    due_storefronts,
    import_reviews,
    import_summaries,
    latest_totals,
    snapshot,
    version_ratings,
)
from ascstats.stats.versions import sync_asc_versions
from tests.fake_asc import FakeASC

SNAKE = 1000000001
DAY = date(2026, 10, 10)


def lookup_transport(results_by_front: dict[str, list[dict]], calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "itunes.apple.com"
        calls.append(request)
        front = request.url.params["country"]
        if front == "gb" and sum(1 for c in calls if c.url.params["country"] == "gb") == 1:
            return httpx.Response(429)  # rate limited once, then fine
        return httpx.Response(200, json={"results": results_by_front.get(front, [])})

    return httpx.MockTransport(handler)


@pytest.mark.django_db
def test_public_lookup_snapshot_is_exact_and_idempotent(apps, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    monkeypatch.setattr("ascstats.stats.ratings.time.sleep", lambda _: None)
    snake = App.objects.get(apple_id=SNAKE)
    call_command("seed_synthetic", "--days", "2")  # sales in US, GB, ... -> storefronts
    RatingSnapshot.objects.all().delete()
    results = {
        "us": [{"trackId": SNAKE, "userRatingCount": 84, "averageUserRating": 4.46429,
                "version": "2.3", "releaseDate": "2025-01-01T08:00:00Z",
                "artworkUrl512": "https://is1-ssl.mzstatic.com/snake/512x512bb.jpg",
                "primaryGenreName": "Games"}],
        "gb": [{"trackId": SNAKE, "userRatingCount": 3, "averageUserRating": 3.66667,
                "primaryGenreName": "Spiele"}],
    }  # fmt: skip
    calls = []
    assert snapshot(DAY, transport=lookup_transport(results, calls), pause=0) == 2
    assert snapshot(DAY, transport=lookup_transport(results, calls), pause=0) == 2
    us = RatingSnapshot.objects.get(app=snake, storefront="us")
    assert (us.count, us.star_sum, us.version) == (84, 375, "2.3")  # 84 x 4.46429 = 375
    assert RatingSnapshot.objects.get(app=snake, storefront="gb").star_sum == 11
    snake.refresh_from_db()  # the US listing wins over other storefronts
    assert (snake.released_on, snake.primary_genre) == (date(2025, 1, 1), "Games")
    assert snake.icon_url.endswith("/512x512bb.jpg")
    expected = {str(a.apple_id) for a in apps}
    assert all(set(c.url.params["id"].split(",")) == expected for c in calls)


@pytest.mark.django_db
def test_new_ratings_are_exact_differences_per_storefront(apps):
    snake = App.objects.get(apple_id=SNAKE)
    snaps = [
        (DAY, "us", 84, 375),
        (DAY + timedelta(days=1), "us", 87, 385),  # 3 new ratings worth 10 stars
        (DAY + timedelta(days=1), "gb", 50, 200),  # storefront first seen: not "new"
        (DAY + timedelta(days=2), "us", 87, 385),
        (DAY + timedelta(days=2), "gb", 51, 205),
    ]
    RatingSnapshot.objects.bulk_create(
        RatingSnapshot(app=snake, date=d, storefront=f, count=c, star_sum=s) for d, f, c, s in snaps
    )
    assert daily_new_ratings(snake) == {
        DAY + timedelta(days=1): (3, 10),
        DAY + timedelta(days=2): (1, 5),
    }


@pytest.mark.django_db
def test_version_ratings_split_velocity_and_quality_by_version(apps):
    snake = App.objects.get(apple_id=SNAKE)
    AppVersion.objects.create(app=snake, platform="IOS", version="1.0", first_seen=DAY)
    AppVersion.objects.create(
        app=snake, platform="IOS", version="1.1", first_seen=DAY + timedelta(days=3)
    )
    count = stars = 0
    for n, (new, new_stars) in enumerate([(0, 0), (4, 20), (2, 10), (6, 12), (2, 4)]):
        count, stars = count + new, stars + new_stars
        RatingSnapshot.objects.create(app=snake, date=DAY + timedelta(days=n), storefront="us",
                                      count=count, star_sum=stars)  # fmt: skip
    CustomerReview.objects.create(
        app=snake, asc_id="a", rating=2, territory="USA", version="1.1", platform="IOS",
        created=datetime(2026, 10, 10, tzinfo=UTC),
    )  # fmt: skip
    by_version = {v.version: v for v in version_ratings(snake)}
    old, new = by_version["1.0"], by_version["1.1"]
    assert (old.days_observed, old.new_ratings, old.new_average) == (2, 6, 5.0)
    assert (new.days_observed, new.new_ratings, new.new_average) == (2, 8, 2.0)  # a regression
    assert (new.reviews, new.review_average) == (1, 2.0)


@pytest.mark.django_db
def test_reviews_are_filed_under_apples_version_with_get_only(apps):
    fake = FakeASC()
    client = fake.client()
    snake = App.objects.get(apple_id=SNAKE)
    sync_asc_versions(client, snake)  # 2.3 -> ver-2, 2.4 -> ver-3
    assert import_reviews(snake, client) == 2
    assert import_reviews(snake, client) == 2  # idempotent
    crash = CustomerReview.objects.get(asc_id="rev-2")
    assert (crash.rating, crash.version, crash.territory) == (1, "2.3", "GBR")
    reply = (crash.reviewer_nickname, crash.response_body, crash.response_state)
    assert reply == ("someone", "Fixed in 2.4, sorry!", "PUBLISHED")
    assert crash.responded_at == datetime(2026, 10, 7, 16, 30, tzinfo=UTC)
    lovely = CustomerReview.objects.get(asc_id="rev-1")
    assert lovely.version == ""  # Apple filed it nowhere
    assert (lovely.response_body, lovely.responded_at) == ("", None)  # unanswered
    params = next(r.url.params for r in fake.requests if r.url.path.endswith("/customerReviews")
                  and "/apps/" in r.url.path)  # fmt: skip
    assert params["include"] == "response"
    assert import_summaries(snake, client) == 2  # one per platform in the fixture
    assert ReviewSummary.objects.filter(app=snake, territory="USA").exists()
    assert {r.method for r in fake.requests} == {"GET"}


@pytest.mark.django_db
def test_synthetic_ratings_flow_into_api_and_dashboard(client, apps):
    call_command("seed_synthetic", "--days", "40")
    snake = App.objects.get(apple_id=SNAKE)
    assert RatingSnapshot.objects.filter(app=snake).values("date").distinct().count() == 40
    now = current(snake)
    assert now["count"] > 0
    assert 1 <= now["average"] <= 5
    assert now["week_new"] > 0
    snake.refresh_from_db()
    assert (snake.released_on, snake.primary_genre) == (date(2025, 1, 1), "Games")
    replied = CustomerReview.objects.filter(app=snake).exclude(response_body="")
    assert 0 < replied.count() < CustomerReview.objects.filter(app=snake).count()
    assert now["unanswered"] == CustomerReview.objects.filter(app=snake, response_body="").count()
    body = client.get(f"/api/apps/{SNAKE}/ratings").json()
    assert body["summary"]
    assert body["unanswered_reviews"] == now["unanswered"]
    assert any(v["ratings_per_day"] for v in body["versions"])
    chart = client.get(f"/chart/{SNAKE}/90/")
    assert b"Latest written reviews" in chart.content
    assert (
        b"Apple&#x27;s review summary" in chart.content
        or b"Apple's review summary" in chart.content
    )


@pytest.mark.django_db
def test_import_skips_apps_the_key_cannot_see_but_fails_if_none_readable(
    apps, settings, monkeypatch
):
    settings.ASC_SOURCE = "asc"
    fake = FakeASC()
    forbidden = httpx.Response(403, json={"errors": [{"detail": "not allowed"}]})
    for a in apps[1:]:
        for _ in range(5):
            fake.queue(f"/v1/apps/{a.apple_id}/customerReviews", forbidden)
    monkeypatch.setattr("ascstats.asc.credentials.client_from_env", fake.client)
    call_command("import_reviews")  # Snake readable, the other two skipped
    assert CustomerReview.objects.filter(app__apple_id=SNAKE).count() == 2
    for _ in range(5):
        fake.queue(f"/v1/apps/{SNAKE}/customerReviews", forbidden)
    with pytest.raises(CommandError, match="role"):
        call_command("import_reviews")


CODES = ["fr", "de", "it", "es", "nl", "se", "ar", "br", "cl", "co", "pe", "mx", "au", "nz"]


def _turn(front: str) -> int:
    """Days after ``DAY`` until the storefront's weekly turn (0: on ``DAY``)."""
    return (zlib.crc32(front.encode()) - DAY.toordinal()) % 7


@pytest.mark.django_db
def test_quiet_storefronts_are_asked_weekly_busy_ones_daily(apps):
    snake = App.objects.get(apple_id=SNAKE)
    quiet, stale = [f for f in CODES if _turn(f)][:2]
    today = next(f for f in CODES if not _turn(f))
    snaps = [
        (DAY - timedelta(days=20), "gb", 50), (DAY - timedelta(days=1), "gb", 51),  # moved
        (DAY - timedelta(days=20), quiet, 9), (DAY - timedelta(days=1), quiet, 9),  # did not
        (DAY - timedelta(days=9), stale, 4),  # missed its turn
        (DAY - timedelta(days=7), today, 3),  # its turn is today
    ]  # fmt: skip
    RatingSnapshot.objects.bulk_create(
        RatingSnapshot(app=snake, date=d, storefront=f, count=c, star_sum=c * 4)
        for d, f, c in snaps
    )
    candidates = ["us", "gb", quiet, stale, today, "jp"]
    assert due_storefronts(DAY, candidates) == ["us", "gb", stale, today, "jp"]  # jp: never asked
    assert quiet in due_storefronts(DAY + timedelta(days=_turn(quiet)), candidates)


@pytest.mark.django_db
def test_weekly_storefront_lands_its_week_and_still_counts_in_totals(apps):
    snake = App.objects.get(apple_id=SNAKE)
    snaps = [(DAY + timedelta(days=n), "us", 100 + n, 4 * (100 + n)) for n in range(8)]
    snaps += [(DAY, "fr", 10, 40), (DAY + timedelta(days=7), "fr", 13, 55)]
    RatingSnapshot.objects.bulk_create(
        RatingSnapshot(app=snake, date=d, storefront=f, count=c, star_sum=s) for d, f, c, s in snaps
    )
    daily = daily_new_ratings(snake)
    assert daily[DAY + timedelta(days=3)] == (1, 4)  # US only
    assert daily[DAY + timedelta(days=7)] == (1 + 3, 4 + 15)  # fr's whole week lands here
    assert latest_totals([snake]) == {snake.pk: (107 + 13, 428 + 55)}
    RatingSnapshot.objects.filter(storefront="fr", date=DAY + timedelta(days=7)).delete()
    assert current(snake)["count"] == 107 + 10  # fr not asked on the latest day: still counted


@pytest.mark.django_db
@pytest.mark.parametrize(("us_before", "every"), [("0.99", False), ("1.99", True)])
def test_a_us_price_change_asks_every_storefront(apps, settings, monkeypatch, us_before, every):
    settings.ASC_SOURCE = "asc"
    snake = App.objects.get(apple_id=SNAKE)
    quiet = next(f for f in CODES if _turn(f))
    monkeypatch.setattr("ascstats.stats.ratings.storefronts", lambda _: ["us", quiet])
    yesterday = DAY - timedelta(days=1)
    RatingSnapshot.objects.bulk_create([
        RatingSnapshot(app=snake, date=yesterday, storefront="us", count=5, star_sum=20,
                       price=Decimal(us_before), currency="USD"),
        RatingSnapshot(app=snake, date=yesterday, storefront=quiet, count=2, star_sum=8),
    ])  # fmt: skip
    results = {
        "us": [{"trackId": SNAKE, "userRatingCount": 5, "averageUserRating": 4.0,
                "price": 0.99, "currency": "USD"}],
        quiet: [{"trackId": SNAKE, "userRatingCount": 2, "averageUserRating": 4.0,
                 "price": 1.09, "currency": "EUR"}],
    }  # fmt: skip
    calls = []
    snapshot(DAY, transport=lookup_transport(results, calls), pause=0)
    assert [c.url.params["country"] for c in calls] == (["us", quiet] if every else ["us"])
    us = RatingSnapshot.objects.get(app=snake, date=DAY, storefront="us")
    assert (us.price, us.currency) == (Decimal("0.99"), "USD")
    if every:
        abroad = RatingSnapshot.objects.get(app=snake, date=DAY, storefront=quiet)
        assert (abroad.price, abroad.currency) == (Decimal("1.09"), "EUR")
