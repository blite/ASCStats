"""Pages are cached until the data changes, which happens a few times a day at most.

The cache key includes a data version: one query reading the latest import, metric rebuild,
rating snapshot, review, summary, FX and version-dating marks plus the set of active apps.
Every write the pipeline (or an admin toggling an app) makes moves one of them, so a cached
page can never outlive the data it was built from, with no invalidation calls to forget.
"""

from __future__ import annotations

import functools
import hashlib

from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse

from .models import (
    App,
    AppVersion,
    ConversionMetric,
    CustomerReview,
    FxDay,
    IngestionRun,
    RatingSnapshot,
    ReviewSummary,
)

TTL = 24 * 3600


def _t(model) -> str:
    return connection.ops.quote_name(model._meta.db_table)


def data_version() -> str:
    sql = f"""
        SELECT
          (SELECT MAX(finished_at) FROM {_t(IngestionRun)}),
          (SELECT MAX(computed_at) FROM {_t(ConversionMetric)}),
          (SELECT COUNT(*) FROM {_t(RatingSnapshot)}), (SELECT MAX(date) FROM {_t(RatingSnapshot)}),
          (SELECT COUNT(*) FROM {_t(CustomerReview)}), (SELECT COUNT(*) FROM {_t(ReviewSummary)}),
          (SELECT MAX(date) FROM {_t(FxDay)}),
          (SELECT COUNT(*) FROM {_t(AppVersion)}), (SELECT MAX(first_seen) FROM {_t(AppVersion)}),
          (SELECT SUM(CASE WHEN is_active THEN id ELSE 0 END) FROM {_t(App)})
    """  # table names come from model metadata, never from input
    with connection.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
    return hashlib.sha1(repr(row).encode(), usedforsecurity=False).hexdigest()[:16]


def cached_page(view):
    """Serve a view's 200 responses from the cache until the data version changes."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        key = f"page:{data_version()}:{request.get_full_path()}"
        if (body := cache.get(key)) is not None:
            return HttpResponse(body)
        response = view(request, *args, **kwargs)
        if response.status_code == 200:
            cache.set(key, response.content, TTL)
        return response

    return wrapper
