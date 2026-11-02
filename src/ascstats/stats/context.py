"""Template context every page shares."""

from django.conf import settings

from .models import App


def site(request):
    return {
        "demo": settings.DEMO_SITE,
        "source_url": settings.SOURCE_URL,
        "airflow_url": settings.AIRFLOW_URL,
        "nav_apps": App.objects.active().only("apple_id", "name", "icon_url"),  # read lazily
    }
