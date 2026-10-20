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

from .models import FxRate

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
    """Replace the stored rates for [start, end]. Idempotent."""
    rows = [
        FxRate(date=day, currency=cur, usd_per_unit=Decimal(1) / Decimal(str(per_usd)))
        for day, rates in fetch(start, end, transport).items()
        for cur, per_usd in rates.items()
    ]
    rows += [
        FxRate(date=day, currency="USD", usd_per_unit=Decimal(1)) for day in {r.date for r in rows}
    ]
    with transaction.atomic():
        FxRate.objects.filter(date__range=(start, end)).delete()
        FxRate.objects.bulk_create(rows, batch_size=2000)
    return len(rows)


class Converter:
    """As-of lookup over the stored rates: the latest rate on or before the day."""

    def __init__(self):
        self._dates: dict[str, list[date]] = defaultdict(list)
        self._rates: dict[str, list[Decimal]] = defaultdict(list)
        for day, cur, rate in FxRate.objects.order_by("currency", "date").values_list(
            "date", "currency", "usd_per_unit"
        ):
            self._dates[cur].append(day)
            self._rates[cur].append(rate)

    def usd(self, amount: Decimal, currency: str, day: date) -> Decimal | None:
        if currency == "USD":
            return amount
        i = bisect.bisect_right(self._dates.get(currency, []), day) - 1
        return amount * self._rates[currency][i] if i >= 0 else None
