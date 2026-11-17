"""In-app purchase sales per product: which packs, upgrades and items sell best.

Read from the sales report (SalesRow), where every in-app purchase line carries its parent
app's SKU. Products are grouped by their own SKU, never by title: Apple can report one
product under two titles (its display name on some days, the bare SKU on others), and a
title group would split it in two. Days are the sales report's Pacific days; proceeds are
summed per currency and converted to USD with the ECB rate of each day (fx.Converter), so
they can differ by a little from the UTC, Apple-converted totals of the Revenue card.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Max, Min, Sum

from .fx import Converter
from .models import IAP_ROWS, App, SalesRow


@dataclass
class Product:
    sku: str
    name: str
    units: int = 0  # net of refunds
    refunds: int = 0  # units refunded, as a positive count
    usd: Decimal = Decimal(0)  # proceeds converted to USD, net of refunds
    unconverted: dict[str, Decimal] = field(default_factory=dict)  # currency -> amount
    first_sale: date | None = None
    last_sale: date | None = None
    titles: list[str] = field(default_factory=list)  # every title the report used


def display_name(sku: str, titles: list[tuple[date, str]]) -> str:
    """The product's newest title that is not just its SKU; the SKU when it has no other."""
    named = [t for _, t in sorted(titles) if t and t != sku]
    return named[-1] if named else sku


def iap_sales(app: App, days: int | None = None) -> dict:
    """Each in-app product of ``app`` over the last ``days`` days of its sales data (all time
    when None), best-selling first: {"start", "end", "products": [Product], totals}."""
    rows = SalesRow.objects.filter(IAP_ROWS, app=app)
    end = rows.aggregate(d=Max("report_date"))["d"]
    if end is None:
        return {"start": None, "end": None, "days": days, "products": [], "units": 0,
                "usd": Decimal(0), "unconverted": {}}  # fmt: skip
    if days is not None:
        rows = rows.filter(report_date__gt=end - timedelta(days=days))

    # Developer Proceeds is per unit; a refund line's negative units net it out
    money = DecimalField(max_digits=20, decimal_places=4)
    amount = ExpressionWrapper(F("units") * F("developer_proceeds"), output_field=money)
    by_day = list(
        rows.values("sku", "title", "report_date", "currency_of_proceeds").annotate(
            n=Sum("units"), amount=Sum(amount)
        )
    )
    refunds = {
        r["sku"]: r["n"] for r in rows.filter(units__lt=0).values("sku").annotate(n=Sum("units"))
    }
    sold = {
        r["sku"]: (r["first"], r["last"])
        for r in rows.filter(units__gt=0)
        .values("sku")
        .annotate(first=Min("report_date"), last=Max("report_date"))
    }
    start = min((r["report_date"] for r in by_day), default=None)
    fx = Converter(since=start - timedelta(days=7) if start else None,
                   currencies={r["currency_of_proceeds"] for r in by_day})  # fmt: skip

    titles: dict[str, list[tuple[date, str]]] = defaultdict(list)
    products: dict[str, Product] = {}
    for r in by_day:
        sku = r["sku"]
        p = products.setdefault(sku, Product(sku, sku))
        titles[sku].append((r["report_date"], r["title"]))
        p.units += r["n"]
        usd = fx.usd(r["amount"], r["currency_of_proceeds"], r["report_date"])
        if usd is None:
            cur = r["currency_of_proceeds"]
            p.unconverted[cur] = p.unconverted.get(cur, Decimal(0)) + r["amount"]
        else:
            p.usd += usd
    for sku, p in products.items():
        p.name = display_name(sku, titles[sku])
        p.titles = sorted({t for _, t in titles[sku]})
        p.refunds = -(refunds.get(sku) or 0)
        p.first_sale, p.last_sale = sold.get(sku, (None, None))
        p.usd = p.usd.quantize(Decimal("0.01"))
        p.unconverted = {c: v.quantize(Decimal("0.01")) for c, v in sorted(p.unconverted.items())}

    ranked = sorted(products.values(), key=lambda p: (-p.units, -p.usd, p.name))
    unconverted: dict[str, Decimal] = defaultdict(Decimal)
    for p in ranked:
        for c, v in p.unconverted.items():
            unconverted[c] += v
    return {
        "start": end - timedelta(days=days - 1) if days is not None else start,
        "end": end,
        "days": days,
        "products": ranked,
        "units": sum(p.units for p in ranked),
        "usd": sum((p.usd for p in ranked), Decimal(0)),
        "unconverted": dict(sorted(unconverted.items())),
    }
