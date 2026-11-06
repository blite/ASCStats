"""Purchases and downloads history the daily import has not landed.

Analytics reports only flow from the day an app's ongoing request was made, and Apple keeps
each daily copy about two weeks. So first the ongoing request's App Store Purchases copies
Apple still keeps, landed where the daily download puts them. Then, once per app, a
ONE_TIME_SNAPSHOT request, which Apple fills with history back to 2024 over a day or two: its
purchases and downloads land under snapshot/ in the raw store. Run it again until every app
says ready; each run rebuilds the app's analytics tables.
"""

from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ascstats.asc.client import ASCError
from ascstats.stats.models import App
from ascstats.stats.parsing import DOWNLOADS_REPORT, PURCHASES_REPORT
from ascstats.stats.pipeline import Pipeline, analytics_prefix, snapshot_prefix

# what the site's UTC days are built from
SNAPSHOT_REPORTS = (PURCHASES_REPORT, DOWNLOADS_REPORT)


class Command(BaseCommand):
    help = "Land each app's App Store Purchases history: recent copies, then a snapshot."

    def add_arguments(self, parser):
        parser.add_argument("--app", type=int, help="one app's Apple ID (default: active apps)")
        parser.add_argument(
            "--no-snapshot", action="store_true", help="only the recent copies; request nothing"
        )

    def handle(self, *, app, no_snapshot, **_):
        if settings.ASC_SOURCE != "asc":
            raise CommandError("backfill_purchases needs ASC_SOURCE=asc")
        from ascstats.asc.credentials import client_from_env
        from ascstats.asc.report_requests import ReportRequester

        client = client_from_env()
        requester = ReportRequester(client)
        pipeline = Pipeline(client_factory=lambda: client)

        def land(request_id: str, a: App, name: str, prefix_of, replace: bool) -> tuple[int, int]:
            """Every DAILY instance of one report of a request; returns (instances, files)."""
            instances = []
            for report in client.paginate(
                f"/v1/analyticsReportRequests/{request_id}/reports", {"filter[name]": name}
            ):
                instances += client.paginate(
                    f"/v1/analyticsReports/{report['id']}/instances",
                    {"filter[granularity]": "DAILY"},
                )
            files = 0
            for instance in instances:
                day = date.fromisoformat(instance["attributes"]["processingDate"])
                prefix = prefix_of(day, a.apple_id, name)
                if not replace and pipeline.store.list(prefix):
                    continue  # the daily download has it already
                segments = client.paginate(
                    f"/v1/analyticsReportInstances/{instance['id']}/segments"
                )
                data = [client.download_segment(s) for s in segments]
                pipeline.store.delete_prefix(prefix)
                for n, raw in enumerate(data):
                    pipeline.store.put(f"{prefix}segment-{n:03d}.tsv.gz", raw)
                files += len(data)
            return len(instances), files

        apps = App.objects.filter(apple_id=app) if app else App.objects.active()
        for a in apps:
            try:
                if ongoing := requester.running_request(a.apple_id):
                    n, files = land(ongoing, a, PURCHASES_REPORT, analytics_prefix, False)
                    self.stdout.write(f"{a.name}: recent, {n} instances, {files} new files")
                if not no_snapshot:
                    ensured = requester.ensure_snapshot(a.apple_id)
                    if ensured.created:
                        self.stdout.write(f"{a.name}: snapshot requested; run again later")
                    else:
                        for name in SNAPSHOT_REPORTS:
                            n, files = land(ensured.request_id, a, name, snapshot_prefix, True)
                            ready = f"{n} instances, {files} files" if n else "nothing yet"
                            self.stdout.write(f"{a.name}: snapshot {name}, {ready}")
            except ASCError as exc:
                self.stderr.write(f"{a.name}: App Store Connect said {exc.status}: {exc}")
                continue
            self.stdout.write(f"{a.name}: rebuilt, {pipeline.rebuild(a)} rows")
