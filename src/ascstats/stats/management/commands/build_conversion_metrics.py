from datetime import UTC, date, datetime, timedelta

from django.core.management.base import BaseCommand

from ascstats.stats.metrics import build_conversion_metrics


class Command(BaseCommand):
    help = "Rebuild the derived conversion table for a date range (default: the last 7 days)."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="start", type=date.fromisoformat)
        parser.add_argument("--to", dest="end", type=date.fromisoformat)
        parser.add_argument("--days", type=int, default=7)

    def handle(self, *, start, end, days, **_):
        end = end or datetime.now(UTC).date() - timedelta(days=1)
        start = start or end - timedelta(days=days - 1)
        written = build_conversion_metrics(start, end)
        self.stdout.write(f"conversion metrics {start}..{end}: {written} rows")
