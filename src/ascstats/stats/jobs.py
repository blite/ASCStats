"""Background jobs: ``django.tasks`` queued in Postgres (django-tasks-db) and run by
``manage.py db_worker``.

Each job is a thin task around a management command, so a job and a hand-run command always
do the same thing. Jobs go to one of three queues, and each queue has a single worker, so two
jobs in a queue never overlap:

- ``store``: the public App Store (ratings, and the keyword riders in ``extensions``), whose
  rate limit is per IP;
- ``asc``: App Store Connect;
- ``default``: rebuilds from data already loaded.

What runs when is decided by ``schedule.py``, from the data and from these tasks' own results.
"""

from __future__ import annotations

import logging

from django.core.management import call_command
from django.core.management.base import CommandError
from django.tasks import task

from . import extensions
from .management.commands.import_asc import NOT_READY_EXIT

log = logging.getLogger(__name__)

NOT_READY = "not_ready"  # an import's result while Apple has not published the day yet


@task(queue_name="asc")
def import_day(report: str, day: str) -> str:
    """One day of a Sales or Analytics report. "Not published yet" is a result, not a
    failure: the schedule asks again half an hour later."""
    try:
        call_command("import_asc", "--report", report, "--date", day)
    except CommandError as error:
        if error.returncode == NOT_READY_EXIT:
            return NOT_READY
        raise
    return "imported"


@task(queue_name="asc")
def restate(start: str, end: str) -> None:
    """Re-import a week Apple may have restated (refunds, late transactions), then rebuild it."""
    for report in ("sales", "analytics"):
        call_command("import_asc", "--report", report, "--from", start, "--to", end)
    call_command("build_conversion_metrics", "--from", start, "--to", end)


@task
def rebuild() -> None:
    """The derived tables, after an import landed: conversion for the last 7 days and FX rates
    for 14, then the versions dated from sales (which reads App Store Connect: its own queue)."""
    call_command("build_conversion_metrics", "--days", "7")
    call_command("import_fx", "--days", "14")
    versions.enqueue()


@task(queue_name="asc")
def versions() -> None:
    call_command("build_versions")


@task(queue_name="asc")
def asc_daily(day: str) -> None:
    """Once a day (``day`` names the run): versions, then reviews (so each review is filed
    under its version), then the read-only check that every app's Analytics report request is
    still running."""
    call_command("build_versions")
    call_command("import_reviews")
    call_command("ensure_report_requests", "--check-only")


@task(queue_name="store")
def store_daily(day: str) -> None:
    """Once a day (``day`` names the run): today's rating totals, then each registered daily
    store step (keyword searches, then search suggestions). Every step runs even if one before
    it failed; each picks up where a failed run stopped, so a retry only asks what is missing."""
    steps = [lambda: call_command("snapshot_ratings"), *extensions.daily_store_steps]
    failed = []
    for step in steps:
        try:
            step()
        except Exception:
            log.exception("daily store step failed")
            failed.append(step)
    if failed:
        raise RuntimeError(f"{len(failed)} of {len(steps)} daily store steps failed")
