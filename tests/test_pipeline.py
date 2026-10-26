from datetime import date

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ascstats.asc.client import ASCError, ReportNotReady
from ascstats.stats import synthetic
from ascstats.stats.app_status import inactive_reason
from ascstats.stats.metrics import build_conversion_metrics
from ascstats.stats.models import App, ConversionMetric, FunnelDay, IngestionRun, SalesDay, SalesRow
from ascstats.stats.parsing import ValidationError
from ascstats.stats.pipeline import Pipeline, QualityError, sales_prefix
from tests.fake_asc import FakeASC

DAY = date(2026, 10, 10)


def snapshot():
    return sorted(
        SalesRow.objects.values_list("app__apple_id", "sku", "country_code", "device", "units")
    )


@pytest.mark.django_db
def test_sales_import_is_idempotent(apps):
    pipeline = Pipeline()
    first = pipeline.run("sales", DAY)
    before = snapshot()
    second = pipeline.run("sales", DAY)
    assert snapshot() == before
    assert first.rows_loaded == second.rows_loaded == second.rows_deleted == len(before)
    statuses = IngestionRun.objects.values_list("status", flat=True)
    assert list(statuses) == ["succeeded", "succeeded"]


@pytest.mark.django_db
def test_in_app_purchases_are_attributed_to_parent_app(apps):
    Pipeline().run("sales", DAY)
    snake = App.objects.get(sku="SYN-SNAKE")
    assert SalesRow.objects.filter(app=snake, product_type="IA1").exists()
    assert not App.objects.filter(apple_id=1000000101).exists()


@pytest.mark.django_db
def test_per_app_load_leaves_other_apps_alone(apps):
    pipeline = Pipeline()
    pipeline.run("sales", DAY)
    sudoku, snake = App.objects.get(sku="SYN-SUDOKU"), App.objects.get(sku="SYN-SNAKE")
    SalesRow.objects.filter(app=snake).update(units=999)
    pipeline.run("sales", DAY, step="load", app=sudoku)
    assert set(SalesRow.objects.filter(app=snake).values_list("units", flat=True)) == {999}


@pytest.mark.django_db
def test_failed_load_rolls_back_the_whole_day(apps, monkeypatch):
    pipeline = Pipeline()
    pipeline.run("sales", DAY)
    before = snapshot()
    monkeypatch.setattr("ascstats.stats.pipeline._sales_row", lambda *a: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        pipeline.run("sales", DAY, step="load")
    assert snapshot() == before
    assert IngestionRun.objects.first().status == "failed"


@pytest.mark.django_db
def test_quality_check_catches_drift(apps):
    pipeline = Pipeline()
    pipeline.run("sales", DAY)
    SalesRow.objects.filter(pk=SalesRow.objects.first().pk).delete()
    with pytest.raises(QualityError):
        pipeline.run("sales", DAY, step="quality")


@pytest.mark.django_db
def test_steps_hand_off_through_the_raw_store(apps):
    pipeline = Pipeline()
    with pytest.raises(ValidationError, match="run the download step first"):
        pipeline.run("sales", DAY, step="validate")
    pipeline.run("sales", DAY, step="download")
    assert pipeline.store.list(sales_prefix(DAY))
    assert pipeline.run("sales", DAY, step="validate").rows_parsed > 0
    pipeline.run("sales", DAY, step="load")
    pipeline.run("sales", DAY, step="quality")


@pytest.mark.django_db
def test_future_dates_are_not_ready(apps):
    with pytest.raises(ReportNotReady):
        Pipeline().run("sales", date(2999, 1, 1))
    assert IngestionRun.objects.get().status == "not_ready"


@pytest.mark.django_db
def test_asc_source_end_to_end_with_recorded_fixtures(apps):
    fake = FakeASC()
    pipeline = Pipeline(source="asc", client_factory=fake.client)
    pipeline.run("sales", DAY)
    sudoku = App.objects.get(sku="SYN-SUDOKU")
    assert sum(SalesRow.objects.filter(app=sudoku).values_list("units", flat=True)) == 14

    # The recorded fixtures only offer App Downloads Standard; scope the run to it.
    monkey_reports = ["App Downloads Standard"]
    import ascstats.stats.pipeline as p

    original, p.ANALYTICS_REPORTS = p.ANALYTICS_REPORTS, monkey_reports
    try:
        pipeline.run("analytics", DAY, app=sudoku)
    finally:
        p.ANALYTICS_REPORTS = original
    # the raw rows are reduced, not stored: one FunnelDay per (day, source)
    assert list(
        FunnelDay.objects.values_list("source_type", "first_time_downloads", "redownloads")
    ) == [("App Store search", 11, 2)]
    assert {r.method for r in fake.requests} == {"GET"}


@pytest.mark.django_db
def test_analytics_and_conversion_metrics(apps):
    pipeline = Pipeline()
    pipeline.run("sales", DAY)
    pipeline.run("analytics", DAY)
    assert build_conversion_metrics(DAY, DAY) == 3
    metric = ConversionMetric.objects.get(app__sku="SYN-SUDOKU")
    assert metric.unique_impressions > metric.unique_page_views > 0
    assert metric.first_time_downloads > 0
    assert 0 < metric.install_rate < metric.tap_through_rate < 1
    build_conversion_metrics(DAY, DAY)
    assert ConversionMetric.objects.count() == 3


@pytest.mark.django_db
def test_import_command_backfill_and_exit_codes(apps):
    call_command("import_asc", "--report", "sales", "--from", "2026-10-01", "--to", "2026-10-03")
    assert set(SalesRow.objects.values_list("report_date", flat=True)) == {
        date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3),
    }  # fmt: skip
    with pytest.raises(CommandError) as exc:
        call_command("import_asc", "--report", "sales", "--date", "2999-01-01")
    assert exc.value.returncode == 3
    with pytest.raises(CommandError, match="not both"):
        call_command(
            "import_asc", "--report", "sales", "--date", "2026-10-01", "--from", "2026-10-01"
        )


@pytest.mark.django_db
def test_seed_synthetic_is_repeatable():
    call_command("seed_synthetic", "--days", "3")
    counts = (SalesRow.objects.count(), SalesDay.objects.count(), FunnelDay.objects.count())
    call_command("seed_synthetic", "--days", "3")
    assert (SalesRow.objects.count(), SalesDay.objects.count(), FunnelDay.objects.count()) == counts
    assert App.objects.count() == 3


@pytest.mark.django_db
def test_list_apps_emits_string_ids_for_pod_args(apps, capsys):
    import json

    call_command("list_apps")
    ids = json.loads(capsys.readouterr().out)
    assert ids
    assert all(isinstance(i, str) for i in ids)


@pytest.mark.django_db
def test_analytics_import_skips_an_app_asc_will_not_serve(apps, monkeypatch, capsys):
    """One deleted app (403) or one without an ONGOING request (404) must not cost the day
    for every other app."""
    blocked = {apps[0].pk: 403, apps[1].pk: 404}
    real_run = Pipeline.run

    def run(self, report, day, step="all", app=None):
        if app is not None and app.pk in blocked:
            raise ASCError(blocked[app.pk], "nope")
        return real_run(self, report, day, step=step, app=app)

    monkeypatch.setattr(Pipeline, "run", run)
    call_command("import_asc", "--report", "analytics", "--date", str(DAY))
    assert set(FunnelDay.objects.values_list("app", flat=True)) == {apps[2].pk}
    assert capsys.readouterr().err.count("skipped") == 2

    blocked[apps[2].pk] = 500  # anything else still fails the run
    with pytest.raises(CommandError, match="1 day"):
        call_command("import_asc", "--report", "analytics", "--date", str(DAY))


def _versions(*states):
    return httpx.Response(200, json={
        "data": [{"id": f"v{n}", "attributes": {"appStoreState": s}} for n, s in enumerate(states)],
        "links": {},
    })  # fmt: skip


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (None, None),  # the recorded fixture: one version READY_FOR_SALE, one in review
        (httpx.Response(404, json={"errors": []}), "not in App Store Connect"),
        (httpx.Response(403, json={"errors": []}), "not visible to this API key"),
        (
            _versions("DEVELOPER_REMOVED_FROM_SALE"),
            "not ready for sale (DEVELOPER_REMOVED_FROM_SALE)",
        ),
        (_versions("READY_FOR_SALE", "REMOVED_FROM_SALE"), None),  # one platform still on sale
        (_versions(), "not ready for sale (no versions)"),
    ],
)
def test_inactive_reason_from_asc(response, reason):
    fake = FakeASC()
    if response is not None:
        fake.queue("/v1/apps/42/appStoreVersions", response)
    assert inactive_reason(fake.client(), 42) == reason


@pytest.mark.django_db
def test_new_app_is_checked_against_asc_once():
    fake = FakeASC()
    deleted = synthetic.APPS[0].apple_id
    fake.queue(f"/v1/apps/{deleted}/appStoreVersions", httpx.Response(404, json={"errors": []}))
    pipeline = Pipeline(source="asc", client_factory=fake.client)
    pipeline.run("sales", DAY)
    assert set(App.objects.filter(is_active=False).values_list("apple_id", flat=True)) == {deleted}

    asked = len([r for r in fake.requests if r.url.path.endswith("/appStoreVersions")])
    pipeline.run("sales", DAY)  # existing apps are not asked again
    assert len([r for r in fake.requests if r.url.path.endswith("/appStoreVersions")]) == asked
