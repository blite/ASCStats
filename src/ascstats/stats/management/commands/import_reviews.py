from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ascstats.asc.client import ASCError
from ascstats.stats.models import App
from ascstats.stats.ratings import import_reviews, import_summaries


class Command(BaseCommand):
    help = (
        "Import written reviews (filed under the version Apple says they are about) and Apple's "
        "review summaries. App Store Connect GET only. Run build_versions first so versions "
        "carry their App Store Connect IDs."
    )

    def add_arguments(self, parser):
        parser.add_argument("--app", type=int, help="Apple ID; default: every active app")
        parser.add_argument("--date", type=date.fromisoformat, help="synthetic mode: the day")

    def handle(self, *, app, date, **_):
        apps = App.objects.filter(apple_id=app) if app else App.objects.active()
        client = None
        if settings.ASC_SOURCE == "asc":
            from ascstats.asc.credentials import client_from_env

            client = client_from_env()
        failed = []
        for a in apps:
            try:
                reviews = import_reviews(a, client, date)
                summaries = import_summaries(a, client)
            except ASCError as exc:
                # Apps that still have sales but left this team/key answer 403 or 404.
                if exc.status not in (403, 404):
                    raise
                failed.append(a)
                self.stderr.write(f"{a.name}: skipped, App Store Connect said {exc.status}")
                continue
            self.stdout.write(f"{a.name}: {reviews} reviews, {summaries} summaries")
        if apps and len(failed) == len(apps):
            raise CommandError("no app's reviews were readable: check the API key's role")
