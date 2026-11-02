"""The public App Store, read-only and keyless: iTunes Lookup (listings and rating totals),
iTunes Search (search results, for keyword ranks) and search hints (suggestions, for
keyword popularity). Requests carry only app IDs, a search term
and a storefront. App Store Connect, which needs a key, is ``ascstats.asc`` next door.

Apple informally tolerates about 20 calls a minute per IP on these endpoints.
Every caller spaces its requests by ``PAUSE``, and the daily jobs
that use them run one after another (ratings, then keywords), so together they stay near 12.
"""

from .itunes import (
    LOOKUP_IDS,
    PAUSE,
    SEARCH_LIMIT,
    STOREFRONT_IDS,
    TIMEOUT,
    StoreError,
    client,
    hints,
    lookup,
    lookup_all,
    search,
)

__all__ = [
    "LOOKUP_IDS", "PAUSE", "SEARCH_LIMIT", "STOREFRONT_IDS", "TIMEOUT", "StoreError", "client",
    "hints", "lookup", "lookup_all", "search",
]  # fmt: skip
