from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ascstats.stats.ratings import snapshot


class Command(BaseCommand):
    help = (
        "Record today's public App Store rating totals and prices (iTunes Lookup) for every "
        "active app: the US and busy storefronts daily, quiet ones weekly. Run daily: Apple "
        "publishes no rating history."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--date", type=date.fromisoformat, help="synthetic mode only: the day to generate"
        )
        parser.add_argument(
            "--all-storefronts",
            action="store_true",
            help="ask every storefront, not only the due ones",
        )

    def handle(self, *, date, all_storefronts, **_):
        if date and settings.ASC_SOURCE != "synthetic":
            raise CommandError("the public lookup only knows today; --date is synthetic-only")
        self.stdout.write(
            f"rating snapshots: {snapshot(date, every_storefront=all_storefronts)} rows"
        )
