"""Revenue at a glance: developer proceeds from every app, converted to USD.

Proceeds are what Apple pays the developer per unit (after its commission, before tax), in
the storefront's currency; a row's proceeds are units x per-unit proceeds, so refunds
(negative units) net out. Each day converts at that day's ECB reference rate (see ``fx``),
so totals are estimates of the eventual payout, not the payout itself.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import F, Sum

from .fx import Converter
from .models import DOWNLOAD_ROWS, App, SalesRow

SLOTS = 5  # apps with their own color; the rest fold into "Other"


@dataclass
class Line:
    day: date
    app_id: int
    sku: str
    title: str
    country: str
    currency: str
    units: int
    local: Decimal
    usd: Decimal | None


def _lines(start: date, end: date) -> list[Line]:
    convert = Converter()
    rows = (
        SalesRow.objects.filter(report_date__range=(start, end))
        .exclude(developer_proceeds=0)
        .values("report_date", "app_id", "sku", "title", "country_code", "currency_of_proceeds")
        .annotate(n_units=Sum("units"), local=Sum(F("units") * F("developer_proceeds")))
    )
    return [
        Line(
            r["report_date"],
            r["app_id"],
            r["sku"],
            r["title"],
            r["country_code"],
            r["currency_of_proceeds"],
            r["n_units"],
            r["local"],
            convert.usd(r["local"], r["currency_of_proceeds"], r["report_date"]),
        )
        for r in rows
    ]


def _usd(lines, **match) -> float:
    return float(
        sum(
            (
                ln.usd
                for ln in lines
                if ln.usd is not None and all(getattr(ln, k) == v for k, v in match.items())
            ),
            Decimal(0),
        )
    )


def money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}" if abs(value) < 10_000 else f"{sign}${abs(value):,.0f}"


def _pct(now: float, before: float) -> float | None:
    return 100 * (now - before) / before if before else None


def _spark(values: list[float], width: int = 96, height: int = 24) -> str:
    peak = max([*values, 0.01])
    step = width / max(len(values) - 1, 1)
    return " ".join(
        f"{i * step:.1f},{height - 2 - (height - 4) * max(v, 0) / peak:.1f}"
        for i, v in enumerate(values)
    )


# stacked weekly chart geometry
W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 280, 56, 8, 12, 26


def revenue(end: date) -> dict:
    year_start = end - timedelta(days=364)
    lines = _lines(year_start, end)
    d30, d60 = end - timedelta(days=29), end - timedelta(days=59)
    last30 = [ln for ln in lines if ln.day >= d30]
    prev30 = [ln for ln in lines if d60 <= ln.day < d30]
    apps = {a.pk: a for a in App.objects.all()}
    # A product keeps its SKU when renamed; show the latest name it sold under.
    name_of = {ln.sku: ln.title for ln in sorted(lines, key=lambda ln: ln.day)}

    # --- apps, ranked by the year's proceeds; the top SLOTS keep a color everywhere
    year_by_app = defaultdict(float)
    for ln in lines:
        if ln.usd is not None:
            year_by_app[ln.app_id] += float(ln.usd)
    ranked = sorted(year_by_app, key=lambda a: -year_by_app[a])
    color = {app_id: f"c{i + 1}" for i, app_id in enumerate(ranked[:SLOTS])}
    other_ids = set(ranked[SLOTS:])

    # --- weekly stacks (52 weeks ending on ``end``)
    weeks = [
        (end - timedelta(days=7 * (51 - i) + 6), end - timedelta(days=7 * (51 - i)))
        for i in range(52)
    ]
    series = [*[(apps[a].name, color[a], {a}) for a in ranked[:SLOTS]]]
    if other_ids:
        n = len(other_ids)
        series.append((f"Other ({n} app{'s' if n != 1 else ''})", "other", other_ids))
    week_totals = []
    stacks = []
    for ws, we in weeks:
        seg = []
        for name, cls, ids in series:
            v = sum(
                float(ln.usd)
                for ln in lines
                if ln.usd is not None and ln.app_id in ids and ws <= ln.day <= we
            )
            seg.append((name, cls, v))
        week_totals.append(sum(v for *_, v in seg))
        stacks.append((ws, we, seg))
    peak = max([*week_totals, 1.0])
    tick = _nice_step(peak)
    top = tick * -(-peak // tick)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    slot = plot_w / len(weeks)
    base = PAD_T + plot_h
    bars = []
    for i, (ws, _we, seg) in enumerate(stacks):
        y = base
        x = PAD_L + i * slot + 2
        lines_tip = [f"Week of {ws:%b} {ws.day}", f"{money(sum(v for *_, v in seg))} total"]
        rects = []
        for name, cls, v in seg:
            if v > 0:
                h = plot_h * v / top
                rects.append({"x": round(x, 1), "y": round(y - h + 1, 1), "w": round(slot - 4, 1),
                              "h": round(max(h - 2, 0.8), 1), "cls": cls})  # fmt: skip
                y -= h
            if v:
                lines_tip.append(f"{name}: {money(v)}")
        bars.append(
            {
                "rects": rects,
                "tip": "\n".join(lines_tip),
                "hit_x": round(PAD_L + i * slot, 1),
                "hit_w": round(slot, 1),
            }
        )
    grid = [{"y": round(base - plot_h * v / top, 1), "label": money(v).replace(".00", "")}
            for v in _ticks(top, tick)]  # fmt: skip
    xticks = []
    for i, (ws, _we, _seg) in enumerate(stacks):
        if i == 0 or ws.month != stacks[i - 1][0].month:
            xticks.append({"x": round(PAD_L + (i + 0.5) * slot, 1), "label": f"{ws:%b}"})

    # --- per-app table
    downloads30 = dict(
        SalesRow.objects.filter(DOWNLOAD_ROWS, report_date__range=(d30, end))
        .values_list("app_id")
        .annotate(Sum("units"))
    )
    app_rows = []
    year_total = sum(year_by_app.values())
    for app_id in ranked:
        now, before = _usd(last30, app_id=app_id), _usd(prev30, app_id=app_id)
        product = defaultdict(float)
        for ln in lines:
            if ln.app_id == app_id and ln.usd is not None:
                product[name_of[ln.sku]] += float(ln.usd)
        mine = [ln for ln in lines if ln.app_id == app_id and ln.usd is not None]
        weekly = [sum(float(ln.usd) for ln in mine if ws <= ln.day <= we) for ws, we in weeks[-12:]]
        dl = downloads30.get(app_id, 0)
        share = 100 * year_by_app[app_id] / year_total if year_total else 0
        app_rows.append({
            "app": apps[app_id], "cls": color.get(app_id, "other"),
            "last30": now, "change": _pct(now, before), "year": year_by_app[app_id],
            "share": share, "share_w": round(1.2 * share, 1),
            "per_1k": 1000 * now / dl if dl else None, "downloads": dl,
            "top_product": max(product, key=product.get) if product else "",
            "spark": _spark(weekly),
        })  # fmt: skip

    # --- products and countries (the year)
    products = defaultdict(lambda: {"usd": 0.0, "units": 0, "last30": 0.0})
    countries = defaultdict(float)
    for ln in lines:
        if ln.usd is None:
            continue
        p = products[(ln.app_id, ln.sku, name_of[ln.sku])]
        p["usd"] += float(ln.usd)
        p["units"] += ln.units
        if ln.day >= d30:
            p["last30"] += float(ln.usd)
        countries[ln.country] += float(ln.usd)
    top_products = sorted(
        (
            {"app": apps[a], "title": t, "cls": color.get(a, "other"), **v}
            for (a, _s, t), v in products.items()
        ),
        key=lambda p: -p["usd"],
    )[:8]
    country_rows = sorted(countries.items(), key=lambda kv: -kv[1])[:10]
    cmax = max([v for _, v in country_rows] or [1])
    country_bars = [
        {"code": c, "usd": v, "w": round(100 * max(v, 0) / cmax, 1)} for c, v in country_rows
    ]

    unconverted = defaultdict(Decimal)
    for ln in lines:
        if ln.usd is None:
            unconverted[ln.currency] += ln.local
    total30, totalprev = _usd(last30), _usd(prev30)
    paid_units30 = sum(ln.units for ln in last30)
    dl_total30 = sum(downloads30.values())
    return {
        "end": end,
        "total30": total30,
        "change30": _pct(total30, totalprev),
        "year": year_total,
        "paid_units30": paid_units30,
        "per_1k": 1000 * total30 / dl_total30 if dl_total30 else None,
        "best": app_rows[0] if app_rows else None,
        "series": [{"name": n, "cls": c} for n, c, _ in series],
        "bars": bars,
        "grid": grid,
        "xticks": xticks,
        "apps": app_rows,
        "products": top_products,
        "countries": country_bars,
        "unconverted": [(c, round(v, 2)) for c, v in sorted(unconverted.items())],
        "width": W, "height": H, "pad_l": PAD_L, "plot_r": W - PAD_R, "base": base,
        "pad_t": PAD_T, "plot_h": plot_h,
    }  # fmt: skip


def _nice_step(peak: float, max_ticks: int = 4) -> float:
    magnitude = 0.01
    while True:
        for unit in (1, 2, 5):
            if peak / (unit * magnitude) <= max_ticks:
                return unit * magnitude
        magnitude *= 10


def _ticks(top: float, tick: float) -> list[float]:
    n = round(top / tick)
    return [round(i * tick, 2) for i in range(n + 1)]
