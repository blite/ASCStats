"""Template context every page shares."""

from django.conf import settings

from .models import App


def site(request):
    return {
        "demo": settings.DEMO_SITE,
        "source_url": settings.SOURCE_URL,
        "nav_apps": App.objects.listed().only("apple_id", "name", "icon_url"),  # read lazily
        "nav_ideas": App.objects.ideas().only("apple_id", "name"),
    }
