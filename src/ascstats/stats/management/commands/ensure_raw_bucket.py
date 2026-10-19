import time

from django.core.management.base import BaseCommand, CommandError

from ascstats.stats.storage import S3Store, raw_store


class Command(BaseCommand):
    help = "Create the raw landing bucket if it does not exist (no-op for file:// storage)."

    def add_arguments(self, parser):
        parser.add_argument("--wait", type=int, default=60, help="seconds to wait for the store")

    def handle(self, *, wait, **_):
        store = raw_store()
        if not isinstance(store, S3Store):
            return
        deadline = time.monotonic() + wait
        while True:
            try:
                store.ensure_bucket()
                break
            except Exception as exc:
                if time.monotonic() > deadline:
                    raise CommandError(f"object store not reachable: {exc}") from exc
                time.sleep(2)
        self.stdout.write(f"bucket {store.bucket} ready")
