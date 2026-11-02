from django.core.management.base import BaseCommand

from ascstats.stats import schedule


class Command(BaseCommand):
    help = (
        "Queue the background jobs that are due (imports, rebuilds, the daily store and App "
        "Store Connect jobs, the weekly restatement). Idempotent: run it every 15 minutes."
    )

    def handle(self, **_):
        queued = schedule.run()
        for d in queued:
            args = " ".join(f"{k}={v}" for k, v in d.kwargs.items())
            self.stdout.write(f"queued {d.job} {args}".rstrip())
        if not queued:
            self.stdout.write("nothing due")
