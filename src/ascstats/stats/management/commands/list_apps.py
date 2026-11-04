import json

from django.core.management.base import BaseCommand

from ascstats.stats.models import App


class Command(BaseCommand):
    help = "Print active app Apple IDs as JSON."
    # Strings, not numbers, so the IDs can go straight into command-line arguments.

    def handle(self, **_):
        ids = [str(i) for i in App.objects.active().values_list("apple_id", flat=True)]
        self.stdout.write(json.dumps(ids))
