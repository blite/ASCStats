from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ascstats.aso.keywords import snapshot


class Command(BaseCommand):
    help = (
        "Search the App Store once for every tracked keyword (iTunes Search, top 200) and save "
        "every app's position. Run daily, after snapshot_ratings: they share Apple's rate limit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--date", type=date.fromisoformat, help="synthetic mode only: the day to generate"
        )

    def handle(self, *, date, **_):
        if date and settings.ASC_SOURCE != "synthetic":
            raise CommandError("the public search only knows today; --date is synthetic-only")
        self.stdout.write(f"keyword searches: {snapshot(date)} saved")
