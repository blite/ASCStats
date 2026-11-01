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
    home = client.get("/").content
    assert b'href="/app/1000000001/"' in home  # each release lane opens its app page
    deep_dive = b'hx-get="/chart/{app}/{days}/"'
    assert deep_dive not in home  # the deep-dive lives on the app page only
    stats = client.get("/app/1000000001/").content
    assert deep_dive in stats
    assert b"<h2>Revenue</h2>" in stats  # the Stats tab has downloads and revenue together
    assert client.get("/app/999/").status_code == 404
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
    with django_assert_max_num_queries(3):  # session, user, then only the data-version query
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
    assert day.label == f"{end:%a}, {end:%b} {end.day}"  # the day named outright
    assert downloads[0].note == f"{day.label} vs {Period(end - timedelta(days=1), 1).label}"
    assert 0 < revenue(end, day)["total"] < revenue(end, week)["total"]

    for base in ("/", "/revenue/"):
        page = client.get(f"{base}7d/").content
        assert b"last 7 days" in page
        assert f'<a href="{base}7d/" aria-current="true">7 days</a>'.encode() in page
        assert b'href="/revenue/7d/"' in page  # the choice follows you between pages
        default = client.get(base).content
        assert f'<a href="{base}" aria-current="true">30 days</a>'.encode() in default
        one_day = client.get(f"{base}1d/").content
        assert f'<a href="{base}1d/" aria-current="true">{day.label}</a>'.encode() in one_day
        assert client.get(f"{base}90d/").status_code == 404
        assert not re.search(rb'(href|hx-get)="/[^"]*\?', page)  # no query strings anywhere


def test_fragments_are_paths(client, seeded):
    assert client.get("/chart/1000000001/45/").status_code == 404  # not a window
    assert client.get("/chart/1000000001/90/").status_code == 200
    page = client.get("/app/1000000001/").content
    assert b'hx-get="/chart/{app}/{days}/" hx-params="none"' in page


def test_home_shows_new_ratings_and_reviews(client, seeded):
    from datetime import date, timedelta

    from ascstats.stats.models import App
    from ascstats.stats.ratings import recent_activity

    end = date.today() - timedelta(days=1)
    week = recent_activity(list(App.objects.active()), end, 7)
    assert 0 < week["observed"] <= 7  # the fixture seeds only a few days of snapshots
    assert week["new_total"] == sum(c.new_ratings for c in week["changes"]) > 0
    assert all(c.change is not None for c in week["changes"])
    assert all(r.created.date() >= end - timedelta(days=6) for r in week["reviews"])
    home = client.get("/7d/").content.decode()
    assert "Ratings and reviews" in home
    assert f"<strong>{week['new_total']}</strong> new ratings" in home


def test_all_period_covers_every_day_imported(client, seeded):
    from datetime import date, timedelta

    from ascstats.stats import dashboard
    from ascstats.stats.charts import Period
    from ascstats.stats.models import SalesDay
    from ascstats.stats.revenue import revenue

    end = date.today() - timedelta(days=1)
    old = SalesDay.objects.order_by("date").first()
    old.pk, old.date = None, end - timedelta(days=200)  # older than the tiles' 90-day sparkline
    old.save()
    first = old.date
    everything = Period.all(end, first)
    assert (everything.start, everything.key, everything.label) == (first, "all", "all time")
    downloads = dashboard.tiles(end, everything)[0]
    assert downloads.value == sum(SalesDay.objects.values_list("downloads", flat=True))
    assert downloads.change_pct is None  # nothing before the first day to compare with
    assert revenue(end, everything)["total"] >= revenue(end, Period(end, 30))["total"] > 0
    for base in ("/", "/revenue/"):
        page = client.get(f"{base}all/").content.decode()
        assert f'<a href="{base}all/" aria-current="true">All</a>' in page
        assert "all time" in page.lower()
        assert 'href="/revenue/all/"' in page  # the choice follows you between pages
    assert f"{downloads.value:,}" in client.get("/all/").content.decode()


def test_app_page_shows_its_revenue(client, seeded):
    from datetime import date, timedelta

    from ascstats.stats.models import App
    from ascstats.stats.revenue import app_revenue, revenue

    end = date.today() - timedelta(days=1)
    by_app = {row["app"].apple_id: row for row in revenue(end)["apps"]}
    app = App.objects.get(apple_id=next(iter(by_app)))
    mine = app_revenue(app, end)
    assert round(mine["recent"], 2) == round(by_app[app.apple_id]["recent"], 2)
    assert round(mine["year"], 2) == round(by_app[app.apple_id]["year"], 2)
    assert len(mine["bars"]) == 52
    page = client.get(f"/app/{app.apple_id}/").content.decode()
    assert "Revenue" in page
    assert "Weekly proceeds over the past year" in page


def test_events_show_on_the_app_charts(client, seeded):
    from datetime import date, timedelta

    from ascstats.stats.models import AppEvent

    day = (date.today() - timedelta(days=2)).isoformat()
    before = client.get("/chart/1000000001/30/").content
    assert b"rel event" not in before
    tag = ["--app", "1000000001", "--date", day, "--label", "Price $1.99 → $3.99"]
    call_command("add_event", *tag)
    call_command("add_event", *tag, "--note", "first raise")
    assert AppEvent.objects.get().note == "first raise"  # same tag again updates, not duplicates
    chart = client.get("/chart/1000000001/30/").content.decode()  # the event moves the cache key
    assert 'class="rel event"' in chart
    assert "Price $1.99 → $3.99" in chart
    assert "first raise" in chart


@pytest.mark.django_db
def test_review_queue_works_the_unanswered_worst_first(client, apps):
    from datetime import UTC, datetime

    from ascstats.stats.models import App, CustomerReview

    snake, sudoku, _ = App.objects.order_by("apple_id")
    made = datetime(2026, 10, 1, tzinfo=UTC)
    rows = [  # (app, id, rating, day, reply, state)
        (snake, "a", 4, 1, "", ""), (snake, "b", 1, 2, "", ""), (sudoku, "c", 2, 3, "", ""),
        (snake, "d", 1, 4, "Fixed in 2.4", "PUBLISHED"),
        (sudoku, "e", 5, 5, "Thanks!", "PENDING_PUBLISH"),
    ]  # fmt: skip
    for app, asc_id, rating, day, reply, state in rows:
        CustomerReview.objects.create(
            app=app, asc_id=asc_id, rating=rating, title=f"T-{asc_id}", body=f"B-{asc_id}",
            territory="USA", created=made.replace(day=day), response_body=reply,
            response_state=state,
        )  # fmt: skip
    CustomerReview.objects.filter(asc_id="c").update(platform="MAC_OS")

    def titles(path):
        response = client.get(path)
        assert response.status_code == 200
        return re.findall(r"<strong>T-(\w)</strong>", response.content.decode()), response

    order, page = titles("/reviews/")
    assert order == ["b", "c", "a"]  # unanswered only, lowest rating first, then newest
    assert b"3</span> reviews need a reply" in page.content
    asc = f"appstoreconnect.apple.com/apps/{snake.apple_id}/distribution/activity/ios/"
    assert f"{asc}ratingsResponses".encode() in page.content
    assert f"apps.apple.com/us/app/id{snake.apple_id}".encode() in page.content
    mac = f"appstoreconnect.apple.com/apps/{sudoku.apple_id}/distribution/activity/macos/"
    assert f"{mac}ratingsResponses".encode() in page.content  # a Mac review opens the Mac page
    assert titles("/reviews/replied/")[0] == ["e", "d"]  # newest first, pending counts
    assert b"Pending publication" in titles("/reviews/replied/")[1].content
    assert titles("/reviews/all/")[0] == ["e", "d", "c", "b", "a"]
    assert titles(f"/reviews/app/{snake.apple_id}/")[0] == ["b", "a"]
    assert titles(f"/reviews/replied/app/{sudoku.apple_id}/")[0] == ["e"]
    assert client.get("/reviews/nonsense/").status_code == 404
    assert client.get("/reviews/app/42/").status_code == 404
    snake.is_active = False
    snake.save()
    assert titles("/reviews/all/")[0] == ["e", "c"]  # inactive apps are left out


@pytest.mark.django_db
def test_a_new_reply_refreshes_cached_pages(client, apps):
    from datetime import UTC, datetime

    from ascstats.stats.models import App, CustomerReview

    review = CustomerReview.objects.create(
        app=App.objects.first(), asc_id="r", rating=1, title="Bad", territory="USA",
        created=datetime(2026, 10, 1, tzinfo=UTC),
    )  # fmt: skip
    assert b'class="badge needs"' in client.get("/reviews/all/").content
    review.response_body = "Sorry, fixed"
    review.response_state = "PUBLISHED"
    review.save()  # same number of reviews: only the reply moved the data version
    page = client.get("/reviews/all/").content
    assert b"Sorry, fixed" in page
    assert b'class="badge needs"' not in page


def test_store_page_uses_the_review_storefront():
    from ascstats.stats.territories import store_page

    assert store_page(42, "CAN") == "https://apps.apple.com/ca/app/id42"
    assert store_page(42, "GBR") == "https://apps.apple.com/gb/app/id42"
    assert store_page(42, "") == "https://apps.apple.com/us/app/id42"
