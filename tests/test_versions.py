import random
import statistics
from datetime import date, timedelta

import httpx
import pytest

from ascstats.stats import synthetic
from ascstats.stats.metrics import build_conversion_metrics
from ascstats.stats.models import DOWNLOAD_ROWS, App, AppVersion, SalesRow
from ascstats.stats.pipeline import Pipeline
from ascstats.stats.versions import (
    Delta,
    _trend_delta,
    build_versions,
    sync_asc_versions,
    version_impact,
)
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
    trend = regression.deltas[1]  # planted x0.8 on a slowly growing app
    assert -30 < trend.change_pct < -10
    assert trend.significant
    assert 10 < fix.deltas[0].change_pct < 35
    assert fix.deltas[0].significant
    assert fix.updates > 0
    conversion = fix.deltas[3]
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
        "downloads/day vs trend",
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


def _row(app, day, ptype, units, version, device="iPhone"):
    return SalesRow(
        app=app, report_date=day, sku=app.sku, title=app.name, version=version,
        product_type=ptype, units=units, developer_proceeds=0, currency_of_proceeds="USD",
        customer_price=0, customer_currency="USD", country_code="US", device=device,
    )  # fmt: skip


@pytest.mark.django_db
def test_mac_and_ios_versions_are_dated_and_compared_separately(apps):
    app = App.objects.get(apple_id=SNAKE)
    start = date(2026, 1, 1)
    rows = []
    for n in range(28):
        day = start + timedelta(days=n)
        ios_version, mac_version = ("1.0", "2.0") if n < 14 else ("1.1", "2.0")
        rows += [
            _row(app, day, "1F", 10 if n < 14 else 20, ios_version),
            _row(app, day, "F1", 5, mac_version, device="Desktop"),
            _row(app, day, "F7", 50, ios_version, device="iPad"),  # an iOS update, despite F
            _row(app, day, "F3", 100, ios_version),  # redownloads: not downloads
        ]
    SalesRow.objects.bulk_create(rows)
    build_versions(app)
    assert set(AppVersion.objects.filter(app=app).values_list("platform", "version")) == {
        ("IOS", "1.0"), ("IOS", "1.1"), ("MAC_OS", "2.0"),
    }  # fmt: skip
    (ios,) = [i for i in version_impact(app, platform="IOS") if i.before_days]
    assert (ios.version, ios.released_on) == ("1.1", date(2026, 1, 15))
    assert (ios.deltas[0].before, ios.deltas[0].after) == (10, 20)  # F3, F7, Mac excluded
    assert ios.updates == 14 * 50
    (mac,) = version_impact(app, platform="MAC_OS")
    assert mac.before_days == 0
    assert mac.deltas[0].after == 5


@pytest.mark.django_db
def test_apps_the_api_cannot_see_keep_sales_versions(apps):
    fake = FakeASC()
    fake.queue(
        f"/v1/apps/{SNAKE}/appStoreVersions",
        httpx.Response(404, json={"errors": [{"detail": "not found"}]}),
    )
    assert sync_asc_versions(fake.client(), App.objects.get(apple_id=SNAKE)) == 0


def _growing(seed: int, effect: float, growth: float = 0.03) -> tuple[dict, date]:
    """70 days of downloads growing ``growth``/day with a weekend bump and noise; a release on
    day 50 multiplies them by ``effect``. Returns (downloads per day, release day)."""
    rng = random.Random(seed)
    start = date(2026, 1, 1)
    values = {}
    for n in range(70):
        day = start + timedelta(days=n)
        expected = 20 * (1 + growth) ** n * (1.3 if day.weekday() >= 5 else 1.0)
        values[day] = round(expected * (effect if n >= 50 else 1.0) * rng.lognormvariate(0, 0.08))
    return values, start + timedelta(days=50)


def _trend(seed: int, effect: float) -> Delta:
    values, release = _growing(seed, effect)
    return _trend_delta(
        "downloads/day vs trend",
        values,
        (release - timedelta(days=28), release - timedelta(days=1)),
        (release, release + timedelta(days=13)),
    )


def test_trend_adjustment_does_not_credit_releases_with_growth():
    deltas = [_trend(seed, effect=1.0) for seed in range(100)]
    assert abs(statistics.fmean(d.change_pct for d in deltas)) < 3  # unbiased
    assert sum(d.significant for d in deltas) <= 12  # ~5-8% false alarms, not "every release"


def test_trend_adjustment_finds_a_real_effect_on_top_of_growth():
    deltas = [_trend(seed, effect=0.7) for seed in range(100)]
    assert -33 < statistics.fmean(d.change_pct for d in deltas) < -27
    assert all(d.significant for d in deltas)


@pytest.mark.django_db
def test_version_impact_reports_both_naive_and_trend(apps):
    app = App.objects.get(apple_id=SNAKE)
    values, release = _growing(seed=1, effect=1.0)
    SalesRow.objects.bulk_create(
        [_row(app, day, "1F", n, "2.0" if day >= release else "1.0") for day, n in values.items()]
    )
    build_versions(app)
    (impact,) = [i for i in version_impact(app, platform="IOS") if i.version == "2.0"]
    naive, trend = impact.deltas[0], impact.deltas[1]
    assert naive.change_pct > 30  # 14 days of 3%/day growth looks like a big win naively
    assert naive.significant
    assert trend.metric == "downloads/day vs trend"
    assert abs(trend.change_pct) < naive.change_pct / 3


@pytest.mark.django_db
def test_no_trend_estimate_without_two_weeks_of_history(apps):
    app = App.objects.get(apple_id=SNAKE)
    SalesRow.objects.bulk_create(
        [_row(app, date(2026, 1, 1) + timedelta(days=n), "1F", 10, "1.0" if n < 10 else "1.1")
         for n in range(20)]
    )  # fmt: skip
    build_versions(app)
    (impact,) = [i for i in version_impact(app, platform="IOS") if i.version == "1.1"]
    assert impact.deltas[1].change_pct is None


@pytest.mark.django_db
def test_launch_ramp_is_not_used_as_a_trend(apps):
    app = App.objects.get(apple_id=SNAKE)
    other = App.objects.get(apple_id=1000000002)  # history starts before Snake launched
    SalesRow.objects.bulk_create([_row(other, date(2025, 12, 1), "1F", 5, "1.0")])
    SalesRow.objects.bulk_create(
        [_row(app, date(2026, 1, 1) + timedelta(days=n), "1F", 2 + 3 * n if n < 25 else 40,
              "1.0" if n < 25 else "1.1")
         for n in range(40)]
    )  # fmt: skip
    build_versions(app)
    (impact,) = [i for i in version_impact(app, platform="IOS") if i.version == "1.1"]
    assert impact.deltas[0].change_pct is not None  # naive still compares
    assert impact.deltas[1].change_pct is None  # only 4 post-launch days: no trend estimate
