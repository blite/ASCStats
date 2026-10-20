"""Create the synthetic apps and import N days of synthetic reports through the real pipeline
(raw files land in the raw store too), then build conversion metrics."""

from datetime import UTC, datetime, timedelta

from django.core.management.base import BaseCommand

from ascstats.stats import synthetic
from ascstats.stats.metrics import build_conversion_metrics
from ascstats.stats.models import App, Report
from ascstats.stats.pipeline import Pipeline
from ascstats.stats.versions import build_versions


class Command(BaseCommand):
    help = "Seed synthetic apps and N days of synthetic sales + analytics."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90)

    def handle(self, *, days, **_):
        for s in synthetic.APPS:
            App.objects.update_or_create(
                apple_id=s.apple_id,
                defaults={"name": s.name, "sku": s.sku, "bundle_id": s.bundle_id},
            )
        pipeline = Pipeline(source="synthetic")
        end = datetime.now(UTC).date() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        day = start
        while day <= end:
            for report in Report.values:
                pipeline.run(report, day)
            day += timedelta(days=1)
        metrics = build_conversion_metrics(start, end)
        for app in App.objects.filter(is_active=True):
            build_versions(app)
        self.stdout.write(
            f"seeded {len(synthetic.APPS)} apps, {start}..{end}, {metrics} conversion rows"
        )
