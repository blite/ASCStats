from datetime import UTC, date, datetime, timedelta
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django_tasks_db.models import DBTaskResult

from ascstats.aso.models import Keyword, KeywordSnapshot
from ascstats.stats import extensions, jobs, schedule
from ascstats.stats.models import IngestionRun, RatingSnapshot

NOW = datetime(2026, 11, 3, 17, 30, tzinfo=UTC)  # a Tuesday, after the daily jobs' hour
ALL = schedule.JOBS


def work():
    """Run every queued job, as the workers would. Between jobs the worker closes the
    database connection, which on Postgres would end the test's transaction: not here."""
    with mock.patch("django_tasks_db.management.commands.db_worker.close_old_connections"):
        call_command("db_worker", "--batch", "--queue-name", "*", "--no-startup-delay",
                     "--no-reload", verbosity=0)  # fmt: skip


def queued(now=NOW, owned=ALL):
    return [(d.job, d.kwargs) for d in schedule.due(now, owned)]


def finish(task, kwargs, status, ago, value=None):
    """Record a past run of ``task`` that ended ``ago`` before NOW."""
    task.enqueue(**kwargs)
    DBTaskResult.objects.filter(task_path=task.module_path).update(
        status=status, started_at=NOW - ago, finished_at=NOW - ago, return_value=value
    )


@pytest.mark.django_db
def test_import_day_reports_not_ready_as_a_result(monkeypatch):
    def not_yet(*args, **kwargs):
        raise CommandError("1 day(s) not ready", returncode=3)

    monkeypatch.setattr(jobs, "call_command", not_yet)
    assert jobs.import_day.call(report="sales", day="2026-11-02") == jobs.NOT_READY
    monkeypatch.setattr(jobs, "call_command", lambda *a, **k: None)
    assert jobs.import_day.call(report="sales", day="2026-11-02") == "imported"

    def broken(*args, **kwargs):
        raise CommandError("bad file")

    monkeypatch.setattr(jobs, "call_command", broken)
    with pytest.raises(CommandError):
        jobs.import_day.call(report="sales", day="2026-11-02")


@pytest.mark.django_db
def test_store_daily_runs_every_step_then_fails(monkeypatch):
    ran = []
    monkeypatch.setattr(jobs, "call_command", lambda name, *a: ran.append(name))

    def broken():
        ran.append("broken")
        raise RuntimeError("store said no")

    monkeypatch.setattr(extensions, "daily_store_steps", [broken, lambda: ran.append("after")])
    with pytest.raises(RuntimeError, match="1 of 3"):
        jobs.store_daily.call(day="2026-11-03")
    assert ran == ["snapshot_ratings", "broken", "after"]


@pytest.mark.django_db
def test_catchup_daily_and_weekly_jobs_are_due():
    jobs_due = queued()
    days = [k["day"] for j, k in jobs_due if j == "sales"]
    assert days == [(NOW.date() - timedelta(days=n)).isoformat() for n in range(7, 0, -1)]
    assert ("store_daily", {"day": "2026-11-03"}) in jobs_due
    assert ("asc_daily", {"day": "2026-11-03"}) in jobs_due
    assert ("restatement", {"start": "2026-10-25", "end": "2026-10-31"}) in jobs_due
    assert not any(j == "derived" for j, _ in jobs_due)  # nothing imported yet
    morning = queued(NOW.replace(hour=9))
    assert not any(j in ("store_daily", "asc_daily") for j, _ in morning)
    monday_noon = queued(datetime(2026, 11, 2, 12, tzinfo=UTC))
    assert not any(j == "restatement" for j, _ in monday_noon)
    assert queued(owned=("store_daily",)) == [("store_daily", {"day": "2026-11-03"})]


@pytest.mark.django_db
def test_a_job_waits_while_queued_and_backs_off_after_failing():
    day = {"day": "2026-11-03"}
    jobs.store_daily.enqueue(**day)
    assert schedule.state(jobs.store_daily, day, NOW) == "wait"
    DBTaskResult.objects.all().delete()
    finish(jobs.store_daily, day, "FAILED", timedelta(hours=1))
    assert schedule.state(jobs.store_daily, day, NOW) == "wait"
    assert schedule.state(jobs.store_daily, day, NOW + timedelta(hours=1)) == "due"
    DBTaskResult.objects.all().delete()
    finish(jobs.store_daily, day, "RUNNING", timedelta(hours=7))
    assert schedule.state(jobs.store_daily, day, NOW) == "due"  # its worker died
    DBTaskResult.objects.all().delete()
    finish(jobs.store_daily, day, "SUCCESSFUL", timedelta(minutes=1))
    assert schedule.state(jobs.store_daily, day, NOW) == "done"


@pytest.mark.django_db
def test_an_unpublished_day_is_asked_again_half_an_hour_later():
    day = {"report": "sales", "day": "2026-11-02"}
    finish(jobs.import_day, day, "SUCCESSFUL", timedelta(minutes=10), jobs.NOT_READY)
    assert schedule.state(jobs.import_day, day, NOW) == "wait"
    assert schedule.state(jobs.import_day, day, NOW + timedelta(minutes=20)) == "due"


@pytest.mark.django_db
def test_days_imported_before_the_schedule_are_not_imported_again(apps):
    day = date(2026, 11, 1)
    run = dict(report="sales", report_date=day, step="all", source="asc")
    IngestionRun.objects.create(**run, status=IngestionRun.Status.NOT_READY)
    assert ("sales", {"report": "sales", "day": "2026-11-01"}) in queued()
    IngestionRun.objects.create(**run, status=IngestionRun.Status.SUCCEEDED)
    assert ("sales", {"report": "sales", "day": "2026-11-01"}) not in queued()


@pytest.mark.django_db
def test_the_rebuild_follows_each_import():
    landed = datetime.now(UTC) - timedelta(minutes=1)  # before the rebuild below is queued
    IngestionRun.objects.create(report="sales", report_date=date(2026, 11, 2), step="all",
                                source="asc", status="succeeded", finished_at=landed)  # fmt: skip
    assert ("derived", {}) in queued()
    jobs.rebuild.enqueue()
    assert ("derived", {}) not in queued()  # one is waiting already
    DBTaskResult.objects.update(status="SUCCESSFUL", finished_at=NOW)
    assert ("derived", {}) not in queued(NOW + timedelta(minutes=15))  # nothing new landed
    IngestionRun.objects.update(finished_at=NOW + timedelta(hours=1))
    assert ("derived", {}) in queued(NOW + timedelta(hours=1))


@pytest.mark.django_db
def test_schedule_and_workers_run_the_pipeline(apps, settings, monkeypatch):
    """Synthetic data end to end: the schedule queues, the workers run, the next schedule
    finds nothing left but what follows from it."""
    settings.SCHEDULED_JOBS = tuple(j for j in ALL if j != "restatement")  # weekday-bound
    monkeypatch.setattr("ascstats.stats.fx.load", lambda start, end: 0)  # no network
    call_command("seed_synthetic", "--days", "3")  # also tracks the demo keywords
    KeywordSnapshot.objects.all().delete()
    RatingSnapshot.objects.all().delete()
    now = datetime.now(UTC).replace(hour=17, minute=30)
    schedule.run(now)
    work()
    assert not DBTaskResult.objects.filter(status="FAILED").exists(), list(
        DBTaskResult.objects.filter(status="FAILED").values_list("traceback", flat=True)
    )
    for back in range(1, 8):
        day = now.date() - timedelta(days=back)
        assert IngestionRun.objects.filter(report="sales", report_date=day,
                                           status="succeeded").exists()  # fmt: skip
    assert RatingSnapshot.objects.filter(date=now.date()).exists()
    assert KeywordSnapshot.objects.count() == Keyword.objects.count()
    assert DBTaskResult.objects.filter(task_path=jobs.rebuild.module_path).exists()
    assert DBTaskResult.objects.filter(task_path=jobs.versions.module_path).exists()
    again = [d.job for d in schedule.due(now + timedelta(minutes=15))]
    assert again == ["derived"]  # imports landed after the rebuild was queued: once more
    schedule.run(now + timedelta(minutes=15))
    work()
    assert schedule.due(now + timedelta(minutes=30)) == []


@pytest.mark.django_db
def test_adding_a_keyword_searches_it_now(client, apps):
    call_command("seed_synthetic", "--days", "2")
    page = client.post("/app/1000000001/keywords/manage/",
                       {"terms": "snake arcade", "storefront": "us"}, follow=True)  # fmt: skip
    assert "Searching now" in page.content.decode()
    keyword = Keyword.objects.get(term="snake arcade")
    assert not keyword.snapshots.exists()
    work()
    assert keyword.snapshots.exists()
    result = DBTaskResult.objects.get(task_path="ascstats.aso.jobs.search_keyword")
    assert (result.queue_name, result.priority, result.return_value) == ("store", 50, 1)


@pytest.mark.django_db
def test_restatement_reimports_the_week(apps):
    start, end = date(2026, 10, 25), date(2026, 10, 26)
    jobs.restate.call(start=start.isoformat(), end=end.isoformat())
    for report in ("sales", "analytics"):
        loaded = IngestionRun.objects.filter(report=report, status="succeeded")
        assert set(loaded.values_list("report_date", flat=True)) == {start, end}
