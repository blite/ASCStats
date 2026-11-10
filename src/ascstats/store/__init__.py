"""The public App Store, read-only and keyless: iTunes Lookup (listings and rating totals),
iTunes Search (search results, for keyword ranks), search hints (suggestions, for keyword
popularity), the top charts RSS feed (each category's top free, paid and grossing
apps) and the App Store web page (subtitles, which Lookup leaves out). Requests
carry only app IDs, a search term and a storefront. App Store Connect, which needs a key,
is ``ascstats.asc`` next door.

Apple informally tolerates about 20 calls a minute per IP on these endpoints.
Every caller spaces its requests by ``PAUSE``, and the daily jobs
that use them run one after another (ratings, then keywords, then subtitles), so together
they stay near 12. A competitor keyword scan runs in the same queue, never alongside them.
"""

from .itunes import (
    CHART_DEPTH,
    CHART_FEEDS,
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
    subtitle,
    top_chart,
)

__all__ = [
    "CHART_DEPTH", "CHART_FEEDS", "LOOKUP_IDS", "PAUSE", "SEARCH_LIMIT", "STOREFRONT_IDS",
    "TIMEOUT", "StoreError", "client", "hints", "lookup", "lookup_all", "search", "subtitle",
    "top_chart",
]  # fmt: skip
