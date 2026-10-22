"""Create the synthetic apps and import N days of synthetic reports through the real pipeline
(raw files land in the raw store too), then build conversion metrics."""

from datetime import UTC, date, datetime, timedelta

from django.core.management.base import BaseCommand

from ascstats.stats import synthetic
from ascstats.stats.metrics import build_conversion_metrics
from ascstats.stats.models import App, Report
from ascstats.stats.pipeline import Pipeline
from ascstats.stats.ratings import import_reviews, import_summaries, snapshot
from ascstats.stats.versions import build_versions


class Command(BaseCommand):
    help = "Seed synthetic apps and N days of synthetic sales + analytics."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument(
            "--end", type=date.fromisoformat, help="last day to seed (default: yesterday)"
        )

    def handle(self, *, days, end=None, **_):
        for s in synthetic.APPS:
            App.objects.update_or_create(
                apple_id=s.apple_id,
                defaults={"name": s.name, "sku": s.sku, "bundle_id": s.bundle_id},
            )
        pipeline = Pipeline(source="synthetic")
        end = end or datetime.now(UTC).date() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        day = start
        while day <= end:
            for report in Report.values:
                pipeline.run(report, day)
            snapshot(day, source="synthetic")
            for app in App.objects.active():
                import_reviews(app, None, day)
            day += timedelta(days=1)
        metrics = build_conversion_metrics(start, end)
        for app in App.objects.active():
            build_versions(app)
            import_summaries(app, None)
        self.stdout.write(
            f"seeded {len(synthetic.APPS)} apps, {start}..{end}, {metrics} conversion rows"
        )
