"""Pages are cached until the data changes, which happens a few times a day at most.

The cache key includes a data version: one query reading the latest import, metric rebuild,
rating snapshot, review, review reply, summary, FX, version-dating and hand-entered event marks
plus the set of active apps and the sidebar's order and hidden apps, and the row count and
newest row of each table another app registers (``extensions.data_models``: competitors,
keywords and who follows them).
Every write the pipeline (or an admin toggling an app) makes moves one of them, so a cached
page can never outlive the data it was built from, with no invalidation calls to forget.
"""

from __future__ import annotations

import functools
import hashlib

from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse

from . import extensions
from .models import (
    App,
    AppEvent,
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


def _arrangement() -> str:
    """Every app's sidebar position, hidden flag and whether it is an idea as one string, in the
    database's own words."""
    if connection.vendor == "postgresql":
        row = "id::text || ':' || sort_order::text || ':' || is_hidden::text || is_idea::text"
        return f"(SELECT string_agg({row}, ',' ORDER BY id) FROM {_t(App)})"
    row = "id || ':' || sort_order || ':' || is_hidden || is_idea"  # SQLite, for the tests
    return f"(SELECT group_concat(r, ',') FROM (SELECT {row} AS r FROM {_t(App)} ORDER BY id))"


def data_version() -> str:
    sql = f"""
        SELECT
          (SELECT MAX(finished_at) FROM {_t(IngestionRun)}),
          (SELECT MAX(computed_at) FROM {_t(ConversionMetric)}),
          (SELECT COUNT(*) FROM {_t(RatingSnapshot)}), (SELECT MAX(date) FROM {_t(RatingSnapshot)}),
          (SELECT COUNT(*) FROM {_t(CustomerReview)}), (SELECT COUNT(*) FROM {_t(ReviewSummary)}),
          (SELECT COUNT(*) FROM {_t(CustomerReview)} WHERE response_body <> ''),
          (SELECT MAX(responded_at) FROM {_t(CustomerReview)}),
          (SELECT MAX(date) FROM {_t(FxDay)}),
          (SELECT COUNT(*) FROM {_t(AppVersion)}), (SELECT MAX(first_seen) FROM {_t(AppVersion)}),
          (SELECT COUNT(*) FROM {_t(AppEvent)}), (SELECT MAX(created_at) FROM {_t(AppEvent)}),
          (SELECT SUM(CASE WHEN is_active THEN id ELSE 0 END) FROM {_t(App)}), {_arrangement()}
          {
        "".join(
            f", (SELECT COUNT(*) FROM {_t(m)}), (SELECT MAX(id) FROM {_t(m)})"
            for m in extensions.data_models
        )
    }
    """  # table names come from model metadata, never from input
    with connection.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
    return hashlib.sha1(repr(row).encode(), usedforsecurity=False).hexdigest()[:16]


def cached_page(view):
    """Serve a view's 200 responses from the cache until the data version changes."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        # per user: the sidebar names who is signed in
        key = f"page:{data_version()}:{request.user.pk}:{request.get_full_path()}"
        if (body := cache.get(key)) is not None:
            return HttpResponse(body)
        response = view(request, *args, **kwargs)
        if response.status_code == 200:
            cache.set(key, response.content, TTL)
        return response

    return wrapper
