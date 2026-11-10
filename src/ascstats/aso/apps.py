from django.apps import AppConfig


class AsoConfig(AppConfig):
    """Competitors and keywords: other apps on the App Store, next to yours."""

    name = "ascstats.aso"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from ascstats.stats import extensions

        from . import competitors, keywords, rival_keywords, suggestions, views
        from .models import (
            ChartApp,
            ChartCapture,
            Competitor,
            CompetitorRelease,
            CompetitorSnapshot,
            Keyword,
            KeywordScan,
            KeywordSnapshot,
            KeywordSuggestion,
            ListingChange,
        )

        if competitors.Rider in extensions.snapshot_riders:
            return  # registered already
        extensions.snapshot_riders.append(competitors.Rider)
        extensions.data_models += [
            Competitor, Competitor.tracked_for.through, CompetitorSnapshot, CompetitorRelease,
            Keyword, Keyword.tracked_for.through, KeywordSnapshot, KeywordSuggestion,
            ListingChange, KeywordScan, ChartCapture, ChartApp,
        ]  # fmt: skip
        extensions.daily_store_steps += [
            keywords.snapshot,
            suggestions.probe,
            competitors.read_subtitles,
            competitors.read_weekly,  # last: the least important of the store's requests
        ]
        extensions.synthetic_setup += [competitors.seed_synthetic, keywords.seed_synthetic]
        extensions.synthetic_day += [
            keywords.seed_day,
            suggestions.seed_day,
            competitors.seed_subtitles,
            competitors.seed_weekly,
            rival_keywords.seed_day,
        ]
        extensions.export_paths.append(views.export_paths)
        extensions.dashboard_tiles.append(views.dashboard_tiles)
