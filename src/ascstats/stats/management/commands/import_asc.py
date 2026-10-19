"""manage.py import_asc --report {sales,analytics} --date YYYY-MM-DD [--step STEP] [--app ID]
manage.py import_asc --report sales --from 2026-01-01 --to 2026-01-31   (backfill)
manage.py import_asc --report sales --from today-7 --to yesterday        (restatement)

Exit codes: 0 ok, 1 failed, 3 report not published yet (so a scheduler can retry later).
"""

from datetime import UTC, date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError

from ascstats.asc.client import ReportNotReady
from ascstats.stats.models import App, Report
from ascstats.stats.pipeline import STEPS, Pipeline

NOT_READY_EXIT = 3


def parse_day(value: str) -> date:
    """ISO date, or "yesterday" / "today-N" (UTC) so cron schedules need no shell date math."""
    today = datetime.now(UTC).date()
    if value == "yesterday":
        return today - timedelta(days=1)
    if value.startswith("today-"):
        return today - timedelta(days=int(value.removeprefix("today-")))
    return date.fromisoformat(value)


class Command(BaseCommand):
    help = "Import one day (or a backfill range) of an App Store Connect report."

    def add_arguments(self, parser):
        parser.add_argument("--report", required=True, choices=Report.values)
        parser.add_argument("--date", type=parse_day)
        parser.add_argument("--from", dest="start", type=parse_day)
        parser.add_argument("--to", dest="end", type=parse_day)
        parser.add_argument("--step", default="all", choices=["all", *STEPS])
        parser.add_argument("--app", type=int, help="App Apple ID; default: every active app")
        parser.add_argument("--source", choices=["asc", "synthetic"], help="default: $ASC_SOURCE")

    def handle(self, *, report, date, start, end, step, app, source, **_):
        if date and (start or end):
            raise CommandError("use --date or --from/--to, not both")
        if date:
            start = end = date
        if not (start and end) or start > end:
            raise CommandError("give --date, or --from and --to with from <= to")
        target = None
        if app is not None:
            target = App.objects.filter(apple_id=app).first()
            if target is None:
                raise CommandError(f"unknown app {app}; run seed_synthetic or add it in admin")

        pipeline = Pipeline(source=source)
        failed, not_ready = [], []
        day = start
        while day <= end:
            try:
                result = pipeline.run(report, day, step=step, app=target)
            except ReportNotReady:
                not_ready.append(day)
                self.stderr.write(f"{report} {day}: not published yet")
            except Exception as exc:
                failed.append(day)
                self.stderr.write(f"{report} {day}: {type(exc).__name__}: {exc}")
                if start == end:
                    raise CommandError(str(exc)) from exc
            else:
                self.stdout.write(
                    f"{report} {day} {step}: parsed={result.rows_parsed} "
                    f"deleted={result.rows_deleted} loaded={result.rows_loaded}"
                )
            day += timedelta(days=1)

        if failed:
            raise CommandError(f"{len(failed)} day(s) failed: {', '.join(map(str, failed))}")
        if not_ready:
            raise CommandError(f"{len(not_ready)} day(s) not ready", returncode=NOT_READY_EXIT)
