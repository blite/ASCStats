"""Daily FX reference rates, so proceeds from every storefront can be summed in USD.

Rates come from the European Central Bank via the Frankfurter API (free, no key; the request
carries only a date range). The ECB publishes on working days, so a weekend or holiday uses
the latest earlier rate. Currencies the ECB does not cover stay unconverted and are reported
as such instead of being guessed.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import date
from decimal import Decimal

import httpx
from django.db import transaction

from .models import FxDay

API = "https://api.frankfurter.dev/v1"


def fetch(start: date, end: date, transport: httpx.BaseTransport | None = None) -> dict:
    """{date: {currency: units per USD}} for working days in [start, end]."""
    with httpx.Client(timeout=30, transport=transport) as client:
        response = client.get(
            f"{API}/{start.isoformat()}..{end.isoformat()}", params={"base": "USD"}
        )
        response.raise_for_status()
        return {date.fromisoformat(d): r for d, r in response.json()["rates"].items()}


def load(start: date, end: date, transport: httpx.BaseTransport | None = None) -> int:
    """Store one row per published day in [start, end], replacing what was there. Returns
    the number of days stored. Idempotent."""
    rows = [
        FxDay(date=day, rates={"USD": 1.0} | {cur: 1 / per_usd for cur, per_usd in rates.items()})
        for day, rates in fetch(start, end, transport).items()
    ]
    with transaction.atomic():
        FxDay.objects.filter(date__range=(start, end)).delete()
        FxDay.objects.bulk_create(rows)
    return len(rows)


class Converter:
    """As-of lookup over the stored rates: the latest rate on or before the day."""

    def __init__(self, since: date | None = None, currencies: set[str] | None = None):
        """Load only what a caller needs: days from ``since`` on, rates for ``currencies``."""
        self._dates: dict[str, list[date]] = defaultdict(list)
        self._rates: dict[str, list[float]] = defaultdict(list)
        days = FxDay.objects.all() if since is None else FxDay.objects.filter(date__gte=since)
        for day, rates in days.order_by("date").values_list("date", "rates"):
            for cur, rate in rates.items():
                if currencies is None or cur in currencies:
                    self._dates[cur].append(day)
                    self._rates[cur].append(rate)

    def usd(self, amount: Decimal, currency: str, day: date) -> Decimal | None:
        if currency == "USD":
            return amount
        i = bisect.bisect_right(self._dates.get(currency, []), day) - 1
        return amount * Decimal(repr(self._rates[currency][i])) if i >= 0 else None
