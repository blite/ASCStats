from django.conf import settings
from django.core.management.base import BaseCommand

from ascstats.stats.models import App
from ascstats.stats.versions import build_versions, sync_asc_versions


class Command(BaseCommand):
    help = (
        "Date every app version from the loaded sales rows; with ASC_SOURCE=asc also add "
        "App Store Connect's version state (read-only)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--app", type=int, help="Apple ID; default: every active app")

    def handle(self, *, app, **_):
        apps = App.objects.filter(apple_id=app) if app else App.objects.filter(is_active=True)
        client = None
        if settings.ASC_SOURCE == "asc":
            from ascstats.asc.credentials import client_from_env

            client = client_from_env()
        for a in apps:
            synced = sync_asc_versions(client, a) if client else 0
            dated = build_versions(a)
            self.stdout.write(f"{a.name}: {dated} versions dated from sales, {synced} from ASC")
