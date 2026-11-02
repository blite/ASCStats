"""iTunes Lookup, Search and search hints, with the retry policy they share."""

from __future__ import annotations

import plistlib
import time

import httpx

LOOKUP_URL = "https://itunes.apple.com/lookup"
SEARCH_URL = "https://itunes.apple.com/search"
HINTS_URL = "https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints"
PAUSE = 5.0  # seconds between requests, and the base of the backoff when Apple refuses one
TIMEOUT = 15.0
LOOKUP_IDS = 100  # apps per Lookup request; more are asked in further requests
SEARCH_LIMIT = 200  # results per search, Apple's maximum
RETRIED = (403, 429, 500, 502, 503)  # Apple's ways of saying "not now"


# The App Store's own storefront numbers, which the hints endpoint takes in a header
STOREFRONT_IDS = {
    "us": 143441, "fr": 143442, "de": 143443, "gb": 143444, "at": 143445, "be": 143446,
    "fi": 143447, "gr": 143448, "ie": 143449, "it": 143450, "lu": 143451, "nl": 143452,
    "pt": 143453, "es": 143454, "ca": 143455, "se": 143456, "no": 143457, "dk": 143458,
    "ch": 143459, "au": 143460, "nz": 143461, "jp": 143462, "hk": 143463, "sg": 143464,
    "cn": 143465, "kr": 143466, "in": 143467, "mx": 143468, "ru": 143469, "tw": 143470,
    "vn": 143471, "za": 143472, "my": 143473, "ph": 143474, "th": 143475, "id": 143476,
    "pk": 143477, "pl": 143478, "sa": 143479, "tr": 143480, "ae": 143481, "hu": 143482,
    "cl": 143483, "cz": 143489, "il": 143491, "ua": 143492, "co": 143501, "br": 143503,
    "ar": 143505, "pe": 143507, "eg": 143516,
}  # fmt: skip


class StoreError(Exception):
    """The store kept refusing, or the connection kept failing."""


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, transport=transport)


def _get(
    http: httpx.Client, url: str, params: dict, label: str, headers: dict | None = None
) -> httpx.Response:
    """One GET, asked up to four times with growing pauses while Apple refuses or the
    connection fails; raises ``StoreError`` after that."""
    for attempt in range(4):
        last = attempt == 3
        try:
            response = http.get(url, params=params, headers=headers)
        except httpx.TransportError as error:
            if last:
                raise StoreError(f"{label}: {error!r}") from error
        else:
            if response.status_code not in RETRIED:
                if response.is_error:
                    raise StoreError(f"{label}: HTTP {response.status_code}")
                return response
            if last:
                raise StoreError(f"{label}: HTTP {response.status_code}")
        time.sleep(PAUSE * 2 ** (attempt + 1))
    raise AssertionError("unreachable")


def lookup(ids: list[int], storefront: str, http: httpx.Client) -> list[dict]:
    """One storefront's listing for every app in ``ids`` (up to ``LOOKUP_IDS``)."""
    params = {"id": ",".join(map(str, ids)), "country": storefront}
    return _get(http, LOOKUP_URL, params, storefront).json().get("results", [])


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
    return _get(http, SEARCH_URL, params, f"{storefront} {term!r}").json().get("results", [])


def hints(term: str, storefront: str, http: httpx.Client) -> list[str]:
    """The search suggestions the App Store offers for what has been typed so far, in the
    order it offers them. Raises ``StoreError`` for a storefront it has no number for."""
    if storefront not in STOREFRONT_IDS:
        raise StoreError(f"no storefront number for {storefront!r}")
    response = _get(
        http, HINTS_URL, {"clientApplication": "Software", "term": term},
        f"{storefront} hints {term!r}",
        headers={"X-Apple-Store-Front": f"{STOREFRONT_IDS[storefront]},29"},
    )  # fmt: skip
    try:
        found = plistlib.loads(response.content).get("hints", [])
    except (plistlib.InvalidFileException, ValueError, AttributeError) as error:
        raise StoreError(f"{storefront} hints {term!r}: unreadable answer") from error
    return [h["term"] for h in found if isinstance(h, dict) and h.get("term")]
