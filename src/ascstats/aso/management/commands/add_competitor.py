"""Follow (or stop following) a competitor for one of your apps:

python manage.py add_competitor 1000000001 https://apps.apple.com/us/app/some-app/id123456789
python manage.py add_competitor 1000000001 123456789 987654321 ...
python manage.py add_competitor 1000000001 123456789 --remove

Several at once are looked up together, a hundred to a request.
"""

from django.core.management.base import BaseCommand, CommandError

from ascstats.aso import competitors
from ascstats.stats.models import App


class Command(BaseCommand):
    help = "Follow competitors (App Store links or IDs) for one of your apps, or --remove one."

    def add_arguments(self, parser):
        parser.add_argument("app", type=int, help="your app's Apple ID")
        parser.add_argument("competitor", nargs="+", help="App Store links or Apple IDs")
        parser.add_argument("--remove", action="store_true", help="stop following it")

    def handle(self, *, app, competitor, remove, **_):
        mine = App.objects.filter(apple_id=app).first()
        if mine is None:
            raise CommandError(f"no app with the Apple ID {app}")
        if remove:
            for text in competitor:
                try:
                    apple_id, _ = competitors.parse(text)
                except competitors.CompetitorError as error:
                    raise CommandError(str(error)) from None
                rival = mine.competitors.filter(apple_id=apple_id).first()
                if rival is None:
                    raise CommandError(f"{mine.name} does not follow {apple_id}")
                competitors.remove(mine, rival)
                self.stdout.write(f"{mine.name} no longer follows {rival.name}")
            return
        added, refused = competitors.add_many(mine, competitor)
        for rival in added:
            self.stdout.write(f"{mine.name} now follows {rival.name} ({rival.apple_id})")
        for message in refused:
            self.stderr.write(message)
        if refused and not added:
            raise CommandError(refused[0] if len(refused) == 1 else f"none of {len(refused)} added")
