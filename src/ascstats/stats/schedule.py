"""What is due: run every 15 minutes (``manage.py schedule``), it queues each job whose time
has come and which has not run, judging by the data and by the jobs' own results, never by a
calendar of its own. So it catches up by itself: after a day off (the machine asleep, the
cluster down) the next run queues every day that is missing.

- ``sales`` / ``analytics``: each of the last ``CATCHUP_DAYS`` days not imported yet.
- ``derived``: the rebuild, whenever an import finished since the last one.
- ``asc_daily`` / ``store_daily``: once a UTC day, from 17:00 (ratings history cannot be
  backfilled, so a day that is missed stays missed).
- ``restatement``: once an ISO week, from Monday 18:00.

A job is not queued again while it waits or runs; after a failure, two hours later; after
"not published yet", half an hour later. ``SCHEDULED_JOBS`` names the jobs this schedule
owns, so jobs move to it from another scheduler one at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from django.conf import settings
from django.db.models import Max
from django.tasks import Task
from django_tasks_db.models import DBTaskResult

from . import jobs
from .models import IngestionRun, Report

CATCHUP_DAYS = 7
DAILY_AT = 17  # UTC hour: Apple has published the reviews and rating totals by then
RESTATE_AT = 18  # UTC hour on Mondays
RETRY_AFTER = timedelta(hours=2)
NOT_READY_AFTER = timedelta(minutes=30)
STUCK_AFTER = timedelta(hours=6)  # a RUNNING row this old is a worker that died mid-job

JOBS = ("sales", "analytics", "derived", "asc_daily", "store_daily", "restatement")


@dataclass(frozen=True)
class Due:
    job: str
    task: Task
    kwargs: dict


def _results(task: Task, kwargs: dict):
    return DBTaskResult.objects.filter(
        task_path=task.module_path, args_kwargs={"args": [], "kwargs": kwargs}
    ).order_by("-enqueued_at")


def state(task: Task, kwargs: dict, now: datetime) -> str:
    """``due``, ``done`` (succeeded), or ``wait`` (queued, running, or backing off)."""
    last = _results(task, kwargs).first()
    if last is None:
        return "due"
    if last.status == "READY":
        return "wait"
    if last.status == "RUNNING":
        return "wait" if now - (last.started_at or last.enqueued_at) < STUCK_AFTER else "due"
    if last.status == "FAILED":
        return "wait" if now - last.finished_at < RETRY_AFTER else "due"
    if last.return_value == jobs.NOT_READY:
        return "wait" if now - last.finished_at < NOT_READY_AFTER else "due"
    return "done"


def _imported_before(report: str, day: date) -> bool:
    """Imported outside this schedule (by hand, or by the scheduler before it): a load
    succeeded and nothing was found unpublished after it."""
    runs = IngestionRun.objects.filter(report=report, report_date=day)
    last_ok = runs.filter(status=IngestionRun.Status.SUCCEEDED).aggregate(pk=Max("pk"))["pk"]
    return (
        last_ok is not None
        and not runs.filter(status=IngestionRun.Status.NOT_READY, pk__gt=last_ok).exists()
    )


def due(now: datetime | None = None, owned: tuple[str, ...] | None = None) -> list[Due]:
    now = now or datetime.now(UTC)
    owned = settings.SCHEDULED_JOBS if owned is None else owned
    today = now.date()
    candidates: list[Due] = []
    for report in (Report.SALES, Report.ANALYTICS):
        for back in range(CATCHUP_DAYS, 0, -1):  # oldest first
            day = today - timedelta(days=back)
            kwargs = {"report": str(report), "day": day.isoformat()}
            if not _results(jobs.import_day, kwargs).exists() and _imported_before(report, day):
                continue
            candidates.append(Due(str(report), jobs.import_day, kwargs))
    candidates.append(Due("derived", jobs.rebuild, {}))
    if now.hour >= DAILY_AT:
        day = {"day": today.isoformat()}
        candidates += [
            Due("asc_daily", jobs.asc_daily, day),
            Due("store_daily", jobs.store_daily, day),
        ]
    monday = today - timedelta(days=today.weekday())
    if today > monday or now.hour >= RESTATE_AT:
        week = {"start": (monday - timedelta(days=8)).isoformat(),
                "end": (monday - timedelta(days=2)).isoformat()}  # fmt: skip
        candidates.append(Due("restatement", jobs.restate, week))
    out = []
    for c in candidates:
        if c.job not in owned:
            continue
        if c.job == "derived":
            if _rebuild_due(now):
                out.append(c)
        elif state(c.task, c.kwargs, now) == "due":
            out.append(c)
    return out


def _rebuild_due(now: datetime) -> bool:
    """An import finished after the last rebuild was queued, and no rebuild is waiting."""
    landed = IngestionRun.objects.filter(
        status=IngestionRun.Status.SUCCEEDED, report__in=[Report.SALES, Report.ANALYTICS]
    ).aggregate(t=Max("finished_at"))["t"]
    if landed is None:
        return False
    last = _results(jobs.rebuild, {}).first()
    if last is None:
        return True
    if state(jobs.rebuild, {}, now) == "wait":
        return False
    return landed > last.enqueued_at


def run(now: datetime | None = None) -> list[Due]:
    """Queue every job that is due; returns them."""
    queued = due(now)
    for d in queued:
        d.task.enqueue(**d.kwargs)
    return queued
