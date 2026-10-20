import pytest

from ascstats.stats import synthetic
from ascstats.stats.models import App


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Pages are cached by data version; tests with similar data must not share pages."""
    from django.core.cache import cache

    cache.clear()


@pytest.fixture(autouse=True)
def _raw_store(settings, tmp_path):
    settings.RAW_STORAGE_URL = f"file://{tmp_path}/raw"
    settings.ASC_SOURCE = "synthetic"
    settings.ASC_VENDOR_NUMBER = "80000000"


@pytest.fixture
def apps(db):
    return [
        App.objects.create(apple_id=s.apple_id, name=s.name, sku=s.sku, bundle_id=s.bundle_id)
        for s in synthetic.APPS
    ]
