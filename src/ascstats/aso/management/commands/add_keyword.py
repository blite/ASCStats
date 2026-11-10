"""Track (or stop tracking) search terms for one of your apps:

python manage.py add_keyword 1000000001 "snake game" "classic snake" --storefront gb
python manage.py add_keyword 1000000001 "snake game" --storefront gb --remove
"""

from django.core.management.base import BaseCommand, CommandError

from ascstats.aso import jobs, keywords
from ascstats.stats.models import App


class Command(BaseCommand):
    help = "Track search terms for one of your apps in one storefront, or --remove them."

    def add_arguments(self, parser):
        parser.add_argument("app", type=int, help="your app's Apple ID")
        parser.add_argument("terms", nargs="+", help="search terms")
        parser.add_argument("--storefront", default="us", help="two-letter code (default: us)")
        parser.add_argument("--remove", action="store_true", help="stop tracking them")

    def handle(self, *, app, terms, storefront, remove, **_):
        mine = App.objects.filter(apple_id=app).first()
        if mine is None:
            raise CommandError(f"no app with the Apple ID {app}")
        if remove:
            for term in terms:
                k = mine.keywords.filter(
                    term=keywords.normalize(term), storefront=storefront
                ).first()
                if k is None:
                    raise CommandError(f"{mine.name} does not track {term!r} in {storefront}")
                keywords.remove(mine, k)
                self.stdout.write(f"{mine.name} no longer tracks {k}")
            return
        try:
            added = keywords.add(mine, "\n".join(terms), storefront)
        except keywords.KeywordError as error:
            raise CommandError(str(error)) from None
        for k in added:  # as the Keywords tab does: an idea's keywords get no daily search
            jobs.search_keyword.enqueue(k.pk)
        self.stdout.write(f"{mine.name} now tracks {', '.join(map(str, added))}; searching now")
