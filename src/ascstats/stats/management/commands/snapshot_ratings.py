from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ascstats.stats.ratings import snapshot


class Command(BaseCommand):
    help = (
        "Record today's public App Store rating totals (iTunes Lookup) for every active app "
        "in every storefront it sells in. Run daily: Apple publishes no rating history."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--date", type=date.fromisoformat, help="synthetic mode only: the day to generate"
        )

    def handle(self, *, date, **_):
        if date and settings.ASC_SOURCE != "synthetic":
            raise CommandError("the public lookup only knows today; --date is synthetic-only")
        self.stdout.write(f"rating snapshots: {snapshot(date)} rows")
