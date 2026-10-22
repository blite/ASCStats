"""Template context every page shares."""

from django.conf import settings


def site(request):
    return {"demo": settings.DEMO_SITE, "source_url": settings.SOURCE_URL}
