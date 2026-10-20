from datetime import UTC, date, datetime, timedelta

from django.core.management.base import BaseCommand
from django.db.models import Min

from ascstats.stats import fx
from ascstats.stats.models import SalesRow


class Command(BaseCommand):
    help = "Load daily ECB reference rates (USD per unit) for converting proceeds."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="start", type=date.fromisoformat)
        parser.add_argument("--to", dest="end", type=date.fromisoformat)
        parser.add_argument("--days", type=int, default=14, help="default window, ending today")
        parser.add_argument(
            "--all", action="store_true", help="from a week before the earliest sales day"
        )

    def handle(self, *, start, end, days, all, **_):
        end = end or datetime.now(UTC).date()
        if all:
            first = SalesRow.objects.aggregate(first=Min("report_date"))["first"] or end
            start = first - timedelta(days=7)  # a rate for a Monday-after-a-holiday weekend
        start = start or end - timedelta(days=days - 1)
        self.stdout.write(f"fx {start}..{end}: {fx.load(start, end)} rates")
