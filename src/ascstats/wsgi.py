import os

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ascstats.settings")
application = get_wsgi_application()

# Settings fall back to a known key so tests and one-off commands run without one; a site
# served with it would let anyone sign their own session cookie.
if not settings.DEBUG and not os.environ.get("DJANGO_SECRET_KEY"):
    raise ImproperlyConfigured("set DJANGO_SECRET_KEY to serve the site (or DJANGO_DEBUG=1)")
