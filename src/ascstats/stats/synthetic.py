"""Synthetic App Store Connect reports.

Produces files in the same byte format Apple delivers (gzipped TSV, same headers), so the
whole pipeline (raw landing, validation, load, quality checks) runs without real data.
Every number is a deterministic function of (app, date), so re-generating a day gives the
same bytes: that is what makes idempotent re-imports easy to see.

The public repository must never contain real sales numbers; this module is how it demos.
"""

from __future__ import annotations

import csv
import functools
import gzip
import hashlib
import io
import math
import random
from dataclasses import dataclass
from datetime import date, timedelta

from .parsing import DISCOVERY_REPORT, DOWNLOADS_REPORT


@dataclass(frozen=True)
class SyntheticApp:
    apple_id: int
    name: str
    sku: str
    bundle_id: str
    price: str  # USD customer price of the app itself
    base_units: float  # typical daily downloads at launch
    iap: tuple[int, str, str, str] | None  # (apple_id, sku, title, price)


# Apple IDs in a range no real app uses, all prefixed "Synthetic".
APPS = [
    SyntheticApp(
        1000000001,
        "Synthetic Snake",
        "SYN-SNAKE",
        "com.example.synthetic.snake",
        "0",
        60,
        (1000000101, "SYN-SNAKE-NOADS", "Remove Ads", "2.99"),
    ),
    SyntheticApp(
        1000000002,
        "Synthetic Sudoku",
        "SYN-SUDOKU",
        "com.example.synthetic.sudoku",
        "0.99",
        18,
        None,
    ),
    SyntheticApp(
        1000000003,
        "Synthetic Stargazer",
        "SYN-STARS",
        "com.example.synthetic.stars",
        "0",
        35,
        (1000000103, "SYN-STARS-PRO", "Stargazer Pro", "4.99"),
    ),
]
EPOCH = date(2025, 1, 1)
# Release history per app: (version, release day, demand multiplier from that day on). The
# multipliers are the "true" effect of each version, so the version-impact analysis has a
# known answer to find. 2.1 and 1.7 are regressions.
RELEASES = {
    1000000001: [
        ("1.0", EPOCH, 1.0), ("1.1", date(2025, 4, 14), 1.0), ("1.2", date(2025, 8, 4), 1.1),
        ("2.0", date(2026, 1, 12), 1.3), ("2.0.1", date(2026, 1, 20), 1.0),
        ("2.1", date(2026, 5, 18), 0.8), ("2.2", date(2026, 7, 27), 1.2),
        ("2.3", date(2026, 10, 5), 1.2),
    ],
    1000000002: [
        ("1.0", EPOCH, 1.0), ("1.1", date(2025, 6, 9), 1.05), ("1.2", date(2026, 3, 2), 0.9),
        ("1.3", date(2026, 9, 21), 1.15),
    ],
    1000000003: [
        ("1.0", EPOCH, 1.0), ("1.5", date(2025, 10, 13), 1.2), ("1.6", date(2026, 6, 15), 1.0),
        ("1.7", date(2026, 9, 28), 0.8),
    ],
}  # fmt: skip
UPDATE_DAYS = 21  # how long a release keeps producing update rows
COUNTRIES = {  # country -> (weight, currency, proceeds multiplier vs USD price)
    "US": (0.45, "USD", 1.0),
    "GB": (0.12, "GBP", 0.8),
    "DE": (0.10, "EUR", 0.92),
    "CA": (0.08, "CAD", 1.36),
    "AU": (0.07, "AUD", 1.5),
    "JP": (0.06, "JPY", 150.0),
    "FR": (0.06, "EUR", 0.92),
    "BR": (0.06, "BRL", 5.0),
}
DEVICES = {"iPhone": 0.8, "iPad": 0.2}
SOURCES = {"App Store search": 0.55, "App Store browse": 0.3, "Web referrer": 0.15}

SALES_HEADER = [
    "Provider", "Provider Country", "SKU", "Developer", "Title", "Version",
    "Product Type Identifier", "Units", "Developer Proceeds", "Begin Date", "End Date",
    "Customer Currency", "Country Code", "Currency of Proceeds", "Apple Identifier",
    "Customer Price", "Promo Code", "Parent Identifier", "Subscription", "Period", "Category",
    "CMB", "Device", "Supported Platforms", "Proceeds Reason", "Preserved Pricing", "Client",
    "Order Type",
]  # fmt: skip
DISCOVERY_HEADER = [
    "Date", "App Name", "App Apple Identifier", "Event", "Page Type", "Source Type",
    "Engagement Type", "Device", "Platform Version", "Territory", "Counts", "Unique Counts",
]  # fmt: skip
DOWNLOADS_HEADER = [
    "Date", "App Name", "App Apple Identifier", "Download Type", "App Version", "Device",
    "Platform Version", "Source Type", "Source Info", "Campaign", "Page Type", "Page Title",
    "Pre-Order", "Territory", "Counts",
]  # fmt: skip


def _rng(*parts: object) -> random.Random:
    seed = hashlib.sha256(":".join(map(str, parts)).encode()).digest()
    return random.Random(int.from_bytes(seed[:8], "big"))


def version_on(app: SyntheticApp, day: date) -> tuple[str, date, float]:
    """(current version, its release day, cumulative demand effect of every release so far)."""
    current, effect = RELEASES[app.apple_id][0], 1.0
    for release in RELEASES[app.apple_id]:
        if release[1] > day:
            break
        current, effect = release, effect * release[2]
    return current[0], current[1], effect


def _demand(app: SyntheticApp, day: date) -> float:
    """Expected first-time downloads: slow growth, weekend bump, a launch spike, the current
    version's effect, noise."""
    age = (day - EPOCH).days
    growth = 1 + 0.004 * max(age, 0)
    weekend = 1.25 if day.weekday() >= 5 else 1.0
    feature = 1 + 2.5 * math.exp(-(((age - 200) / 6) ** 2))  # a one-off App Store feature
    noise = _rng("demand", app.apple_id, day).lognormvariate(0, 0.15)
    return app.base_units * growth * weekend * feature * version_on(app, day)[2] * noise


def downloads(app: SyntheticApp, day: date) -> dict[tuple[str, str], int]:
    """First-time downloads split by (country, device). Shared by sales and analytics."""
    total = _demand(app, day)
    rng = _rng("split", app.apple_id, day)
    out = {}
    for country, (weight, _, _) in COUNTRIES.items():
        for device, dweight in DEVICES.items():
            expected = total * weight * dweight
            units = int(expected) + (1 if rng.random() < expected % 1 else 0)
            if units:
                out[(country, device)] = units
    return out


def _gzip_tsv(header: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return gzip.compress(buffer.getvalue().encode(), mtime=0)


def _money(usd: str, multiplier: float, share: float = 1.0) -> str:
    value = float(usd) * multiplier * share
    return f"{value:.0f}" if multiplier >= 100 else f"{value:.2f}"


def sales_report(day: date) -> bytes:
    """One vendor-wide SALES/SUMMARY/DAILY file covering every synthetic app."""
    stamp = day.strftime("%m/%d/%Y")
    rows = []

    def row(sku, title, ptype, units, price, country, device, apple_id, parent="", version=""):
        _, currency, mult = COUNTRIES[country]
        rows.append([
            "APPLE", "US", sku, "Synthetic Studio", title, version, ptype, str(units),
            _money(price, mult, 0.7), stamp, stamp, currency, country, currency, str(apple_id),
            _money(price, mult), "", parent, "", "", "Games", "", device, "iOS", "", "", "", "",
        ])  # fmt: skip

    for app in APPS:
        rng = _rng("sales", app.apple_id, day)
        version, released, _ = version_on(app, day)
        for (country, device), units in sorted(downloads(app, day).items()):
            row(app.sku, app.name, "1F", units, app.price, country, device, app.apple_id,
                version=version)  # fmt: skip
            if app.iap and (iap_units := sum(rng.random() < 0.04 for _ in range(units))):
                iap_id, iap_sku, iap_title, iap_price = app.iap
                row(iap_sku, iap_title, "IA1", iap_units, iap_price, country, device, iap_id,
                    parent=app.sku)  # fmt: skip
        if app.price != "0" and rng.random() < 0.15:  # the occasional refund
            row(app.sku, app.name, "1F", -1, app.price, "US", "iPhone", app.apple_id,
                version=version)  # fmt: skip
        since = (day - released).days
        if released > EPOCH and since < UPDATE_DAYS:  # existing users updating, decaying
            installed = app.base_units * 30 * (1 + 0.004 * (day - EPOCH).days)
            for country, (weight, _, _) in COUNTRIES.items():
                if units := round(installed * 0.12 * math.exp(-since / 4) * weight):
                    row(app.sku, app.name, "7F", units, "0", country, "iPhone", app.apple_id,
                        version=version)  # fmt: skip
    return _gzip_tsv(SALES_HEADER, rows)


def analytics_report(app: SyntheticApp, report_name: str, day: date) -> bytes:
    rng = _rng("analytics", report_name, app.apple_id, day)
    rows = []
    for (country, device), first_time in sorted(downloads(app, day).items()):
        for source, sweight in SOURCES.items():
            share = first_time * sweight
            if report_name == DISCOVERY_REPORT:
                page_views = max(1, round(share / rng.uniform(0.25, 0.4)))
                impressions = round(page_views * rng.uniform(6, 10))
                for event, counts in (("Impression", impressions), ("Page view", page_views)):
                    rows.append([
                        day.isoformat(), app.name, str(app.apple_id), event, "Product page",
                        source, "", device, "iOS 26", country, str(counts),
                        str(round(counts * 0.85)),
                    ])  # fmt: skip
            elif report_name == DOWNLOADS_REPORT:
                kinds = (("First-time download", round(share)),
                         ("Redownload", round(share * rng.uniform(0.1, 0.3))))  # fmt: skip
                version = version_on(app, day)[0]
                for kind, counts in kinds:
                    if counts:
                        rows.append([
                            day.isoformat(), app.name, str(app.apple_id), kind, version, device,
                            "iOS 26", source, "", "", "Product page", "", "No", country,
                            str(counts),
                        ])  # fmt: skip
            else:
                raise ValueError(f"no synthetic generator for {report_name!r}")
    header = DISCOVERY_HEADER if report_name == DISCOVERY_REPORT else DOWNLOADS_HEADER
    return _gzip_tsv(header, rows)


def app_by_apple_id(apple_id: int) -> SyntheticApp:
    for app in APPS:
        if app.apple_id == apple_id:
            return app
    raise KeyError(f"{apple_id} is not a synthetic app")


# --- ratings and reviews ---------------------------------------------------------------------

RATING_RATE = 0.03  # new ratings per first-time download


def version_stars(effect: float) -> float:
    """Mean stars of ratings given while a version is live: regressions rate worse."""
    return max(1.5, min(4.9, 4.4 + 2.5 * (effect - 1.0)))


@functools.cache
def _rating_history(apple_id: int, until: date) -> dict[date, tuple[int, int]]:
    """Cumulative (count, star_sum) per day since EPOCH, all storefronts together."""
    app = app_by_apple_id(apple_id)
    count = stars = 0
    out, day = {}, EPOCH
    while day <= until:
        rng = _rng("ratings", apple_id, day)
        new = sum(downloads(app, day).values()) * RATING_RATE
        n = int(new) + (1 if rng.random() < new % 1 else 0)
        release = next(r for r in reversed(RELEASES[apple_id]) if r[1] <= day)
        mean = version_stars(release[2])
        for _ in range(n):
            stars += max(1, min(5, round(rng.gauss(mean, 0.9))))
        count += n
        out[day] = (count, stars)
        day += timedelta(days=1)
    return out


def rating_snapshot(app: SyntheticApp, day: date) -> dict[str, tuple[int, int]]:
    """{storefront: (count, star_sum)} as the public lookup would report on ``day``."""
    count, stars = _rating_history(app.apple_id, day)[day]
    return {
        country.lower(): (round(count * weight), round(stars * weight))
        for country, (weight, _, _) in COUNTRIES.items()
    }


REVIEW_TEXT = {
    5: ("Love it", "Exactly what I wanted, and no ads."),
    4: ("Really good", "Great little game; would like more levels."),
    3: ("Okay", "Fine, but the last update changed things I liked."),
    2: ("Not great", "Since the update it feels slower."),
    1: ("Broken", "Crashes since the latest version."),
}


def reviews(app: SyntheticApp, day: date) -> list[dict]:
    """Written reviews posted on ``day``: about one per ten new ratings."""
    rng = _rng("reviews", app.apple_id, day)
    history = _rating_history(app.apple_id, day)
    new = history[day][0] - history.get(day - timedelta(days=1), (0, 0))[0]
    version, _released, _ = version_on(app, day)
    effect = next(r[2] for r in RELEASES[app.apple_id] if r[0] == version)
    out = []
    for k in range(sum(rng.random() < 0.1 for _ in range(new))):
        rating = max(1, min(5, round(rng.gauss(version_stars(effect), 1.0))))
        title, body = REVIEW_TEXT[rating]
        out.append({
            "id": f"syn-{app.apple_id}-{day.isoformat()}-{k}", "rating": rating, "title": title,
            "body": body, "territory": "USA", "created": day, "version": version,
        })  # fmt: skip
    return out


def review_summary(app: SyntheticApp) -> str:
    return (
        f"People enjoy {app.name}'s relaxed pace and the absence of ads. Some reviewers "
        "mention slowdowns after a recent update and ask for more content."
    )
