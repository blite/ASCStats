"""Follow (or stop following) a competitor for one of your apps:

python manage.py add_competitor 1000000001 https://apps.apple.com/us/app/some-app/id123456789
python manage.py add_competitor 1000000001 123456789 --remove
"""

from django.core.management.base import BaseCommand, CommandError

from ascstats.aso import competitors
from ascstats.stats.models import App


class Command(BaseCommand):
    help = "Follow a competitor (App Store link or ID) for one of your apps, or --remove it."

    def add_arguments(self, parser):
        parser.add_argument("app", type=int, help="your app's Apple ID")
        parser.add_argument("competitor", help="App Store link or Apple ID")
        parser.add_argument("--remove", action="store_true", help="stop following it")

    def handle(self, *, app, competitor, remove, **_):
        mine = App.objects.filter(apple_id=app).first()
        if mine is None:
            raise CommandError(f"no app with the Apple ID {app}")
        try:
            if remove:
                apple_id, _ = competitors.parse(competitor)
                rival = mine.competitors.filter(apple_id=apple_id).first()
                if rival is None:
                    raise CommandError(f"{mine.name} does not follow {apple_id}")
                competitors.remove(mine, rival)
                self.stdout.write(f"{mine.name} no longer follows {rival.name}")
            else:
                rival = competitors.add(mine, competitor)
                self.stdout.write(f"{mine.name} now follows {rival.name} ({rival.apple_id})")
        except competitors.CompetitorError as error:
            raise CommandError(str(error)) from None
