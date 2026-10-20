from datetime import date, timedelta

import pytest

from ascstats.stats import synthetic
from ascstats.stats.metrics import build_conversion_metrics
from ascstats.stats.models import DOWNLOAD_ROWS, App, AppVersion, SalesRow
from ascstats.stats.pipeline import Pipeline
from ascstats.stats.versions import build_versions, sync_asc_versions, version_impact
from tests.fake_asc import FakeASC

SNAKE = 1000000001


def load(start: date, end: date) -> None:
    pipeline = Pipeline()
    day = start
    while day <= end:
        pipeline.run("sales", day)
        pipeline.run("analytics", day)
        day += timedelta(days=1)
    build_conversion_metrics(start, end)


@pytest.mark.django_db
def test_updates_are_not_counted_as_downloads(apps):
    release_day = date(2026, 10, 5)  # Snake 2.3
    Pipeline().run("sales", release_day)
    snake = App.objects.get(apple_id=SNAKE)
    updates = SalesRow.objects.filter(app=snake, product_type="7F")
    assert updates.exists()
    assert set(updates.values_list("version", flat=True)) == {"2.3"}
    assert not SalesRow.objects.filter(DOWNLOAD_ROWS, product_type="7F").exists()


@pytest.mark.django_db
def test_versions_are_dated_by_first_appearance_in_sales(apps):
    load(date(2026, 9, 25), date(2026, 10, 10))
    snake = App.objects.get(apple_id=SNAKE)
    assert build_versions(snake) == 2
    assert build_versions(snake) == 2  # idempotent
    assert dict(AppVersion.objects.filter(app=snake).values_list("version", "first_seen")) == {
        "2.2": date(2026, 9, 25),  # already live when the data starts
        "2.3": date(2026, 10, 5),
    }


@pytest.mark.django_db
def test_impact_finds_the_known_effects(apps):
    load(date(2026, 4, 20), date(2026, 8, 20))  # Snake 2.1 (x0.8) and 2.2 (x1.2)
    snake = App.objects.get(apple_id=SNAKE)
    build_versions(snake)
    impacts = {i.version: i for i in version_impact(snake, window=14)}

    assert impacts["2.0.1"].before_days == 0  # no baseline loaded before the data starts
    regression, fix = impacts["2.1"], impacts["2.2"]
    assert (regression.released_on, regression.before_days, regression.after_days) == (
        date(2026, 5, 18),
        14,
        14,
    )
    downloads = regression.deltas[0]
    assert downloads.metric == "downloads/day"
    assert -30 < downloads.change_pct < -10
    assert downloads.significant
    assert 10 < fix.deltas[0].change_pct < 35
    assert fix.deltas[0].significant
    assert fix.updates > 0
    conversion = fix.deltas[2]
    assert conversion.before
    assert conversion.after


@pytest.mark.django_db
def test_windows_stop_at_neighbouring_releases(apps):
    load(date(2026, 1, 1), date(2026, 2, 10))  # 2.0 on 01-12, 2.0.1 eight days later
    snake = App.objects.get(apple_id=SNAKE)
    build_versions(snake)
    impacts = {i.version: i for i in version_impact(snake, window=14)}
    assert impacts["2.0"].after_days == 8
    assert impacts["2.0.1"].before_days == 8


@pytest.mark.django_db
def test_asc_versions_add_state_with_get_only(apps):
    fake = FakeASC()
    snake = App.objects.get(apple_id=SNAKE)
    assert sync_asc_versions(fake.client(), snake) == 2
    pending = AppVersion.objects.get(app=snake, version="2.4")
    assert pending.asc_state == "WAITING_FOR_REVIEW"
    assert pending.first_seen is None
    assert {r.method for r in fake.requests} == {"GET"}


@pytest.mark.django_db
def test_versions_endpoint_and_chart_markers(client, apps):
    load(date(2026, 9, 14), date(2026, 10, 18))
    for a in App.objects.all():
        build_versions(a)
    rows = client.get(f"/api/apps/{SNAKE}/versions").json()
    latest = rows[-1]
    assert latest["version"] == "2.3"
    assert [d["metric"] for d in latest["deltas"]] == [
        "downloads/day",
        "in-app purchases/day",
        "conversion rate",
    ]
    assert client.get("/api/apps/42/versions").status_code == 404
    chart = client.get("/chart/", {"app": SNAKE, "days": 90})
    assert b"v2.3" in chart.content
    assert b"What each release did" in chart.content


def test_synthetic_versions_follow_the_release_table():
    snake = synthetic.app_by_apple_id(SNAKE)
    assert synthetic.version_on(snake, date(2026, 10, 4))[0] == "2.2"
    assert synthetic.version_on(snake, date(2026, 10, 5))[0] == "2.3"
