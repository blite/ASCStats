from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ascstats.asc.client import ASCError
from ascstats.stats.models import App


class Command(BaseCommand):
    help = (
        "Make sure every active app has a running ONGOING Analytics Reports request, creating "
        "one only where none runs (Apple stops idle ones). The pipeline's only App Store "
        "Connect write. No-op in synthetic mode."
    )

    def handle(self, **_):
        if settings.ASC_SOURCE != "asc":
            self.stdout.write("synthetic mode: nothing to request")
            return
        from ascstats.asc.credentials import client_from_env
        from ascstats.asc.report_requests import ReportRequester

        requester = ReportRequester(client_from_env())
        errors = []
        for app in App.objects.filter(is_active=True):
            try:
                result = requester.ensure_ongoing(app.apple_id)
            except ASCError as exc:
                errors.append(f"{app.name}: {exc}")
                self.stderr.write(f"{app.name}: {exc}")
                continue
            state = "created" if result.created else "already running"
            self.stdout.write(f"{app.name}: {state} ({result.request_id})")
        if errors:
            raise CommandError(f"{len(errors)} app(s) failed; see above")
