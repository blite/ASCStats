import json
from pathlib import Path

from django.core.management.base import BaseCommand

from ascstats.stats.models import App


class Command(BaseCommand):
    help = "Print active app Apple IDs as JSON (and write Airflow's XCom file with --xcom)."

    def add_arguments(self, parser):
        parser.add_argument("--xcom", action="store_true", help="write /airflow/xcom/return.json")

    def handle(self, *, xcom, **_):
        ids = list(App.objects.filter(is_active=True).values_list("apple_id", flat=True))
        payload = json.dumps(ids)
        if xcom:
            Path("/airflow/xcom/return.json").write_text(payload)
        self.stdout.write(payload)
