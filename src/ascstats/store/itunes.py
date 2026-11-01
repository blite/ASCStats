"""iTunes Lookup and Search, with the retry policy both share."""

from __future__ import annotations

import time

import httpx

LOOKUP_URL = "https://itunes.apple.com/lookup"
SEARCH_URL = "https://itunes.apple.com/search"
PAUSE = 5.0  # seconds between requests, and the base of the backoff when Apple refuses one
TIMEOUT = 15.0
LOOKUP_IDS = 100  # apps per Lookup request; more are asked in further requests
SEARCH_LIMIT = 200  # results per search, Apple's maximum
RETRIED = (403, 429, 500, 502, 503)  # Apple's ways of saying "not now"


class StoreError(Exception):
    """The store kept refusing, or the connection kept failing."""


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, transport=transport)


def _get(http: httpx.Client, url: str, params: dict, label: str) -> list[dict]:
    """The ``results`` of one GET, asked up to four times with growing pauses while Apple
    refuses or the connection fails; raises ``StoreError`` after that."""
    for attempt in range(4):
        last = attempt == 3
        try:
            response = http.get(url, params=params)
        except httpx.TransportError as error:
            if last:
                raise StoreError(f"{label}: {error!r}") from error
        else:
            if response.status_code not in RETRIED:
                if response.is_error:
                    raise StoreError(f"{label}: HTTP {response.status_code}")
                return response.json().get("results", [])
            if last:
                raise StoreError(f"{label}: HTTP {response.status_code}")
        time.sleep(PAUSE * 2 ** (attempt + 1))
    raise AssertionError("unreachable")


def lookup(ids: list[int], storefront: str, http: httpx.Client) -> list[dict]:
    """One storefront's listing for every app in ``ids`` (up to ``LOOKUP_IDS``)."""
    return _get(
        http, LOOKUP_URL, {"id": ",".join(map(str, ids)), "country": storefront}, storefront
    )


def lookup_all(ids: list[int], storefront: str, http: httpx.Client, pause: float) -> list[dict]:
    """``lookup`` for any number of apps, ``LOOKUP_IDS`` per request, ``pause`` between."""
    out: list[dict] = []
    for i in range(0, len(ids), LOOKUP_IDS):
        if i and pause:
            time.sleep(pause)
        out += lookup(ids[i : i + LOOKUP_IDS], storefront, http)
    return out


def search(term: str, storefront: str, http: httpx.Client, limit: int = SEARCH_LIMIT) -> list[dict]:
    """The iPhone apps a store search for ``term`` returns, in order. It is the public Search
    API's order, which follows the App Store's own closely but not exactly."""
    params = {"term": term, "country": storefront, "entity": "software", "limit": limit}
    return _get(http, SEARCH_URL, params, f"{storefront} {term!r}")
