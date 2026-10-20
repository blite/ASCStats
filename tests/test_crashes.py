from datetime import date, timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ascstats.asc.client import ReportNotReady
from ascstats.stats.crashes import version_crashes
from ascstats.stats.models import App, UsageDay
from ascstats.stats.parsing import CRASHES_REPORT
from ascstats.stats.pipeline import NO_DATA, Pipeline, analytics_prefix
from tests.fake_asc import FakeASC

SNAKE = 1000000001


@pytest.mark.django_db
def test_crash_rate_per_version_finds_the_buggy_release(apps):
    pipeline = Pipeline()
    for n in range(14):  # 2.1 (crashy) until Jul 26, then 2.2
        pipeline.run("analytics", date(2026, 7, 20) + timedelta(days=n))
    by_version = version_crashes(App.objects.get(apple_id=SNAKE))
    buggy, fixed = by_version["2.1"], by_version["2.2"]
    assert buggy.sessions > 1000
    assert 9 < buggy.per_1k_sessions < 15  # planted 12 per 1k
    assert fixed.per_1k_sessions < 4  # planted 2 per 1k
    assert buggy.unique_devices <= buggy.crashes


@pytest.mark.django_db
def test_too_few_sessions_shows_counts_not_a_rate(apps):
    snake = App.objects.get(apple_id=SNAKE)
    UsageDay.objects.create(app=snake, date=date(2026, 1, 1), platform="IOS", version="9.9",
                            crashes=1, sessions=40)  # fmt: skip
    row = version_crashes(snake)["9.9"]
    assert (row.crashes, row.sessions, row.per_1k_sessions) == (1, 40, None)


@pytest.mark.django_db
def test_report_below_privacy_threshold_lands_no_data_marker(apps, monkeypatch):
    fake = FakeASC()
    pipeline = Pipeline(source="asc", client_factory=fake.client)
    real = pipeline.client.analytics_segments

    def segments(apple_id, name, day):
        if name == CRASHES_REPORT:
            raise ReportNotReady(name)  # fewer than five users: Apple publishes nothing
        return real(apple_id, name, day)

    monkeypatch.setattr(pipeline.client, "analytics_segments", segments)
    day = date(2026, 10, 10)
    sudoku = App.objects.get(apple_id=1000000002)  # the recorded fixtures are Sudoku's
    pipeline.run("analytics", day, app=sudoku)
    keys = pipeline.store.list(analytics_prefix(day, sudoku.apple_id, CRASHES_REPORT))
    assert [k.rsplit("/", 1)[-1] for k in keys] == [NO_DATA]
    usage = UsageDay.objects.filter(app=sudoku)
    assert sum(usage.values_list("crashes", flat=True)) == 0  # nothing published, nothing stored
    assert all(u.version for u in usage)


@pytest.mark.django_db
def test_nothing_published_is_not_ready_until_apples_window_passes(apps, monkeypatch):
    pipeline = Pipeline(source="asc", client_factory=FakeASC().client)

    def nothing(*_):
        raise ReportNotReady("none")

    monkeypatch.setattr(pipeline.client, "analytics_segments", nothing)
    snake = App.objects.get(apple_id=SNAKE)
    assert not pipeline.check("analytics", date.today() - timedelta(days=2), snake)
    assert pipeline.check("analytics", date.today() - timedelta(days=6), snake)


@pytest.mark.django_db
def test_check_only_reads_and_names_apps_without_a_request(apps, settings, monkeypatch):
    settings.ASC_SOURCE = "asc"
    fake = FakeASC()
    import httpx

    empty = httpx.Response(200, json={"data": [], "links": {}})
    fake.queue(f"/v1/apps/{SNAKE}/analyticsReportRequests", empty)
    monkeypatch.setattr("ascstats.asc.credentials.client_from_env", fake.client)
    with pytest.raises(CommandError, match="1 app"):
        call_command("ensure_report_requests", "--check-only")
    assert {r.method for r in fake.requests} == {"GET"}
