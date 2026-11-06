from datetime import UTC, datetime

from django.core.management.base import BaseCommand
from django.db.models import Min

from ascstats.stats.metrics import build_conversion_metrics
from ascstats.stats.models import App, UtcDay
from ascstats.stats.pipeline import Pipeline


class Command(BaseCommand):
    help = (
        "Rebuild every app's analytics tables (funnel, usage, UTC days, purchases) from all "
        "the raw report copies kept, newest copy per date, then the conversion metrics."
    )

    def add_arguments(self, parser):
        parser.add_argument("--app", type=int, help="one app's Apple ID (default: every app)")

    def handle(self, *, app, **_):
        pipeline = Pipeline()
        apps = App.objects.filter(apple_id=app) if app else App.objects.all()
        for a in apps:
            self.stdout.write(f"{a.name}: {pipeline.rebuild(a)} rows")
        first = UtcDay.objects.aggregate(d=Min("date"))["d"]
        if first:
            end = datetime.now(UTC).date()
            self.stdout.write(f"conversion metrics: {build_conversion_metrics(first, end)} rows")
