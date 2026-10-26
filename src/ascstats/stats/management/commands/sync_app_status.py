from django.conf import settings
from django.core.management.base import BaseCommand

from ascstats.stats.app_status import apply_asc_status
from ascstats.stats.models import App


class Command(BaseCommand):
    help = (
        "Exclude apps with no version READY_FOR_SALE in App Store Connect. New apps are "
        "checked when the pipeline first sees them; run this once to check the existing ones. "
        "Read-only towards App Store Connect. No-op in synthetic mode."
    )

    def handle(self, **_):
        if settings.ASC_SOURCE != "asc":
            self.stdout.write("synthetic mode: nothing to check")
            return
        from ascstats.asc.credentials import client_from_env

        client = client_from_env()
        for app in App.objects.active():
            reason = apply_asc_status(client, app)
            self.stdout.write(f"{app.name}: {f'excluded, {reason}' if reason else 'active'}")
