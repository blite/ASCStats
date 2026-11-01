from django.apps import AppConfig


class AsoConfig(AppConfig):
    """Competitors and keywords: other apps on the App Store, next to yours."""

    name = "ascstats.aso"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from ascstats.stats import extensions

        from . import competitors, keywords, views
        from .models import (
            Competitor,
            CompetitorRelease,
            CompetitorSnapshot,
            Keyword,
            KeywordSnapshot,
        )

        if competitors.Rider in extensions.snapshot_riders:
            return  # registered already
        extensions.snapshot_riders.append(competitors.Rider)
        extensions.data_models += [
            Competitor, Competitor.tracked_for.through, CompetitorSnapshot, CompetitorRelease,
            Keyword, Keyword.tracked_for.through, KeywordSnapshot,
        ]  # fmt: skip
        extensions.synthetic_setup += [competitors.seed_synthetic, keywords.seed_synthetic]
        extensions.synthetic_day.append(keywords.seed_day)
        extensions.export_paths.append(views.export_paths)
