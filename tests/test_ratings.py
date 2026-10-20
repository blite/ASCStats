from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ascstats.stats.models import App, AppVersion, CustomerReview, RatingSnapshot, ReviewSummary
from ascstats.stats.ratings import (
    current,
    daily_new_ratings,
    import_reviews,
    import_summaries,
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
                "version": "2.3"}],
        "gb": [{"trackId": SNAKE, "userRatingCount": 3, "averageUserRating": 3.66667}],
    }  # fmt: skip
    calls = []
    assert snapshot(DAY, transport=lookup_transport(results, calls), pause=0) == 2
    assert snapshot(DAY, transport=lookup_transport(results, calls), pause=0) == 2
    us = RatingSnapshot.objects.get(app=snake, storefront="us")
    assert (us.count, us.star_sum, us.version) == (84, 375, "2.3")  # 84 x 4.46429 = 375
    assert RatingSnapshot.objects.get(app=snake, storefront="gb").star_sum == 11
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
    assert CustomerReview.objects.get(asc_id="rev-1").version == ""  # Apple filed it nowhere
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
    body = client.get(f"/api/apps/{SNAKE}/ratings").json()
    assert body["summary"]
    assert any(v["ratings_per_day"] for v in body["versions"])
    chart = client.get("/chart/", {"app": SNAKE, "days": 90})
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
