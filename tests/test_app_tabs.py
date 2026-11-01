from datetime import date

import pytest
from django.core.management import call_command

from ascstats.stats import rating_trends
from ascstats.stats.models import RatingSnapshot

SNAKE = 1000000001


@pytest.fixture
def seeded(db):
    call_command("seed_synthetic", "--days", "5")


def test_every_tab_links_to_the_others(client, seeded):
    for path in ("", "ratings/", "reviews/", "reviews/all/", "keywords/", "competitors/"):
        page = client.get(f"/app/{SNAKE}/{path}")
        assert page.status_code == 200, path
        html = page.content.decode()
        for tab in ("ratings/", "reviews/", "keywords/", "competitors/"):
            assert f'href="/app/{SNAKE}/{tab}"' in html
        assert html.count('aria-current="page"') >= 2  # the app in the sidebar, and its tab
        tab = path.split("/")[0]
        assert f'data-app-tab="{tab + "/" if tab else ""}"' in html  # remembered per browser
    assert client.get("/app/999/ratings/").status_code == 404


def test_ratings_tab_and_its_chart(client, seeded):
    page = client.get(f"/app/{SNAKE}/ratings/").content.decode()
    assert "All countries" in page
    assert 'name="country" value="us"' in page
    assert "United States" in page
    assert "\U0001f1fa\U0001f1f8" in page  # its flag
    for country, window in (("all", "90d"), ("us", "all"), ("us", "30d")):
        chart = client.get(f"/app/{SNAKE}/ratings/{country}/{window}/")
        assert chart.status_code == 200
        assert b"<polyline" in chart.content
    assert client.get(f"/app/{SNAKE}/ratings/all/2w/").status_code == 404
    assert client.get(f"/app/{SNAKE}/ratings/usa/90d/").status_code == 404


def test_app_reviews_tab_is_that_apps_queue(client, seeded):
    page = client.get(f"/app/{SNAKE}/reviews/all/").content.decode()
    assert 'href="/app/1000000002/' not in page.split('aria-label="Sidebar"')[0]
    assert "Synthetic Sudoku" not in page.split("</aside>")[1]  # no other app's reviews


def _snap(app, day, front, count, stars):
    RatingSnapshot.objects.create(
        app=app, date=date(2026, 10, day), storefront=front, count=count, star_sum=stars
    )


def test_history_carries_quiet_storefronts_forward(apps):
    snake = apps[0]
    _snap(snake, 1, "us", 100, 450)
    _snap(snake, 1, "fr", 10, 40)  # asked weekly
    _snap(snake, 2, "us", 104, 466)
    _snap(snake, 8, "us", 110, 496)
    _snap(snake, 8, "fr", 12, 50)
    _snap(snake, 8, "de", 5, 25)  # first seen: history, not new ratings

    h = rating_trends.history(snake, "all", "all")
    by_day = {p.day.day: p for p in h["rows"]}
    assert (by_day[1].count, by_day[1].new) == (110, None)
    assert (by_day[2].count, by_day[2].new) == (114, 4)  # France carried forward
    assert (by_day[8].count, by_day[8].new) == (127, 6 + 2)
    assert h["new"] == 12
    assert h["new_average"] == pytest.approx((16 + 30 + 10) / 12)

    week = rating_trends.history(snake, "fr", "30d")
    assert [p.count for p in week["rows"]] == [12, 10]

    c = rating_trends.countries(snake)
    assert [r.code for r in c["rows"]] == ["us", "fr", "de"]
    assert c["count"] == 127
    assert c["new"] == 8  # ratings that landed on the latest day
    assert c["rows"][0].name == "United States"
