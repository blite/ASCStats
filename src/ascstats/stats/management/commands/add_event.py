"""manage.py add_event --app 6764618302 --date 2026-10-27 --label "Price $1.99 → $3.99" [--note ...]

Records a hand-entered event (price change, promotion, ...) shown on the app's charts. Running
it again with the same app, date and label updates the note instead of adding a duplicate.
"""

from django.core.management.base import BaseCommand, CommandError

from ascstats.stats.models import App, AppEvent

from .import_asc import parse_day


class Command(BaseCommand):
    help = "Tag a day on an app's charts with an event such as a price change."

    def add_arguments(self, parser):
        parser.add_argument("--app", required=True, type=int, help="App Apple Identifier")
        parser.add_argument("--date", required=True, type=parse_day)
        parser.add_argument("--label", required=True)
        parser.add_argument("--note", default="")

    def handle(self, *, app, date, label, note, **_):
        try:
            target = App.objects.get(apple_id=app)
        except App.DoesNotExist as e:
            raise CommandError(f"no app with Apple ID {app}") from e
        event, created = AppEvent.objects.update_or_create(
            app=target, date=date, label=label, defaults={"note": note}
        )
        self.stdout.write(
            f"{'added' if created else 'updated'} {target.name}: {event.date} {label}"
        )
