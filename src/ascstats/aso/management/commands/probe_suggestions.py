from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ascstats.aso.suggestions import MAX_PER_RUN, probe


class Command(BaseCommand):
    help = (
        "Look at how the App Store's search suggestions offer the keywords due this week (the "
        "evidence for their popularity). Run daily, after snapshot_keywords: they share "
        "Apple's rate limit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--date", type=date.fromisoformat, help="synthetic mode only: the day to generate"
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=MAX_PER_RUN,
            help=f"keywords this run (default {MAX_PER_RUN})",
        )

    def handle(self, *, date, limit, **_):
        if date and settings.ASC_SOURCE != "synthetic":
            raise CommandError("the store only suggests for today; --date is synthetic-only")
        self.stdout.write(f"suggestions: looked at {probe(date, limit=limit)} keywords")
