import re

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command


@pytest.fixture
def seeded(db):
    call_command("seed_synthetic", "--days", "5")


def test_health(client, db):
    assert client.get("/api/health").json() == {"ok": True}


def test_apps_endpoint(client, seeded):
    apps = client.get("/api/apps").json()
    assert {a["name"] for a in apps} == {
        "Synthetic Snake",
        "Synthetic Sudoku",
        "Synthetic Stargazer",
    }


def test_daily_sales_endpoint(client, seeded):
    days = client.get("/api/apps/1000000002/sales/daily").json()
    assert len(days) == 5
    assert all(d["units"] > 0 and "USD" in d["proceeds"] for d in days)
    assert client.get("/api/apps/42/sales/daily").status_code == 404


def test_conversion_endpoint(client, seeded):
    rows = client.get("/api/apps/1000000001/conversion").json()
    assert len(rows) == 5
    assert rows[0]["page_views"] > 0


def test_dashboard_and_chart(client, seeded):
    assert b"hx-get" in client.get("/").content
    chart = client.get("/chart/1000000001/30/")
    assert chart.status_code == 200
    assert b"<rect" in chart.content
    assert b"<polyline" in chart.content


def test_admin_lists_synthetic_rows(client, seeded):
    User.objects.create_superuser("admin", "admin@example.com", "pw")
    client.login(username="admin", password="pw")
    for model in (
        "app",
        "salesrow",
        "salesday",
        "funnelday",
        "usageday",
        "conversionmetric",
        "appversion",
        "ingestionrun",
    ):
        assert client.get(f"/admin/stats/{model}/").status_code == 200


def test_inactive_apps_are_left_out_of_every_stat(client, seeded):
    from datetime import date, timedelta

    from ascstats.stats import dashboard
    from ascstats.stats.models import App, SalesRow
    from ascstats.stats.revenue import revenue

    end = date.today() - timedelta(days=1)
    before = dashboard.tiles(end)[0].value
    sudoku = App.objects.get(apple_id=1000000002)
    sudoku.is_active = False
    sudoku.save()
    assert SalesRow.objects.filter(app=sudoku).exists()  # still loaded, just not counted
    assert dashboard.tiles(end)[0].value < before
    assert sudoku not in [lane.app for lane in dashboard.scoreboard(end).lanes]
    assert sudoku not in [a["app"] for a in revenue(end)["apps"]]
    assert 1000000002 not in [a["apple_id"] for a in client.get("/api/apps").json()]
    assert 1000000002 in [
        a["apple_id"] for a in client.get("/api/apps?include_inactive=true").json()
    ]
    for path in ("sales/daily", "versions", "ratings", "conversion"):
        assert client.get(f"/api/apps/1000000002/{path}").status_code == 404
    assert b"No data for this app" in client.get("/chart/1000000002/90/").content


def test_pages_are_cached_until_the_data_changes(client, seeded, django_assert_max_num_queries):
    from ascstats.stats.models import App

    first = client.get("/")
    with django_assert_max_num_queries(1):  # only the data-version query
        again = client.get("/")
    assert again.content == first.content
    sudoku = App.objects.get(apple_id=1000000002)
    sudoku.is_active = False  # any data change moves the version: the page is rebuilt
    sudoku.save()
    assert client.get("/").content != first.content


def test_period_switch(client, seeded):
    from datetime import date, timedelta

    from ascstats.stats import dashboard
    from ascstats.stats.charts import Period
    from ascstats.stats.revenue import revenue

    end = date.today() - timedelta(days=1)
    day, week = Period(end, 1), Period(end, 7)
    assert (day.start, day.prev_start) == (end, end - timedelta(days=1))
    assert (week.start, week.prev_start) == (end - timedelta(days=6), end - timedelta(days=13))

    downloads = [dashboard.tiles(end, p)[0] for p in (day, week)]
    assert 0 < downloads[0].value < downloads[1].value
    assert downloads[0].note == f"{day.label} vs the day before"
    assert 0 < revenue(end, day)["total"] < revenue(end, week)["total"]

    for base in ("/", "/revenue/"):
        page = client.get(f"{base}7d/").content
        assert b"last 7 days" in page
        assert f'<a href="{base}7d/" aria-current="true">7 days</a>'.encode() in page
        assert b'href="/revenue/7d/"' in page  # the choice follows you between pages
        default = client.get(base).content
        assert f'<a href="{base}" aria-current="true">30 days</a>'.encode() in default
        assert day.label.encode() in client.get(f"{base}1d/").content
        assert client.get(f"{base}90d/").status_code == 404
        assert not re.search(rb'(href|hx-get)="/[^"]*\?', page)  # no query strings anywhere


def test_fragments_are_paths(client, seeded):
    assert client.get("/chart/1000000001/45/").status_code == 404  # not a window
    assert client.get("/chart/1000000001/90/").status_code == 200
    assert b'hx-get="/chart/{app}/{days}/" hx-params="none"' in client.get("/").content
