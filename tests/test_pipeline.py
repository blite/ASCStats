from datetime import date

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ascstats.asc.client import ReportNotReady
from ascstats.stats.metrics import build_conversion_metrics
from ascstats.stats.models import AnalyticsRow, App, ConversionMetric, IngestionRun, SalesRow
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
    assert sorted(AnalyticsRow.objects.values_list("event", "counts")) == [
        ("First-time download", 11),
        ("Redownload", 2),
    ]
    assert {r.method for r in fake.requests} == {"GET"}


@pytest.mark.django_db
def test_analytics_and_conversion_metrics(apps):
    pipeline = Pipeline()
    pipeline.run("sales", DAY)
    pipeline.run("analytics", DAY)
    assert build_conversion_metrics(DAY, DAY) == 3
    metric = ConversionMetric.objects.get(app__sku="SYN-SUDOKU")
    assert metric.page_views > metric.first_time_downloads > 0
    assert 0 < metric.conversion_rate < 1
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
    counts = (SalesRow.objects.count(), AnalyticsRow.objects.count())
    call_command("seed_synthetic", "--days", "3")
    assert (SalesRow.objects.count(), AnalyticsRow.objects.count()) == counts
    assert App.objects.count() == 3
