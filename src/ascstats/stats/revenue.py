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

from .charts import Period, month_ticks, nice_axis, pct_change, spark
from .fx import Converter
from .models import ACTIVE_ROWS, App, SalesDay, SalesRow

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


def _lines(start: date, end: date, app: App | None = None) -> list[Line]:
    rows = SalesRow.objects.filter(ACTIVE_ROWS, report_date__range=(start, end))
    if app is not None:
        rows = rows.filter(app=app)
    rows = list(
        rows.exclude(developer_proceeds=0)
        .values("report_date", "app_id", "sku", "title", "country_code", "currency_of_proceeds")
        .annotate(n_units=Sum("units"), local=Sum(F("units") * F("developer_proceeds")))
    )
    # a week of earlier rates covers a range that starts on a weekend or holiday
    convert = Converter(
        since=start - timedelta(days=7), currencies={r["currency_of_proceeds"] for r in rows}
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


def money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}" if abs(value) < 10_000 else f"{sign}${abs(value):,.0f}"


# stacked weekly chart geometry
W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 280, 56, 8, 12, 26
WEEKS = 52


def revenue(end: date, period: Period | None = None) -> dict:
    period = period or Period(end)
    year_start = end - timedelta(days=364)
    since, before = period.start, period.prev_start
    # The year's charts and tables plus the period and the one before it; all time can reach
    # further back than the year, and those older rows count only towards the period.
    lines = _lines(min(year_start, before), end)
    apps = {a.pk: a for a in App.objects.all()}
    # A product keeps its SKU when renamed; show the latest name it sold under.
    name_of = {ln.sku: ln.title for ln in sorted(lines, key=lambda ln: ln.day)}

    # One pass over the rows: every total below is read from these buckets.
    by_week = defaultdict(float)  # (week index, app id) -> usd; week 51 ends on ``end``
    year_by_app, now_by_app, before_by_app = (
        defaultdict(float),
        defaultdict(float),
        defaultdict(float),
    )
    by_app_product = defaultdict(lambda: defaultdict(float))
    products = defaultdict(lambda: {"usd": 0.0, "units": 0, "recent": 0.0})
    countries = defaultdict(float)
    unconverted = defaultdict(Decimal)
    paid_units = 0
    for ln in lines:
        if ln.day >= since:
            paid_units += ln.units
        if ln.usd is None:
            unconverted[ln.currency] += ln.local
            continue
        usd = float(ln.usd)
        name = name_of[ln.sku]
        p = products[(ln.app_id, ln.sku, name)]
        if ln.day >= since:
            now_by_app[ln.app_id] += usd
            p["recent"] += usd
        elif ln.day >= before:
            before_by_app[ln.app_id] += usd
        if ln.day < year_start:
            continue
        week = WEEKS - 1 - (end - ln.day).days // 7
        if week >= 0:  # the year has a day more than 52 whole weeks
            by_week[(week, ln.app_id)] += usd
        year_by_app[ln.app_id] += usd
        by_app_product[ln.app_id][name] += usd
        p["usd"] += usd
        p["units"] += ln.units
        countries[ln.country] += usd

    # --- apps, ranked by the year's proceeds; the top SLOTS keep a color everywhere
    ranked = sorted(year_by_app, key=lambda a: -year_by_app[a])
    color = {app_id: f"c{i + 1}" for i, app_id in enumerate(ranked[:SLOTS])}
    other_ids = set(ranked[SLOTS:])

    # --- weekly stacks
    week_starts = [end - timedelta(days=7 * (WEEKS - 1 - i) + 6) for i in range(WEEKS)]
    series = [(apps[a].name, color[a], {a}) for a in ranked[:SLOTS]]
    if other_ids:
        n = len(other_ids)
        series.append((f"Other ({n} app{'s' if n != 1 else ''})", "other", other_ids))
    stacks = [
        [(name, cls, sum(by_week[(w, a)] for a in ids)) for name, cls, ids in series]
        for w in range(WEEKS)
    ]
    top, tick_values = nice_axis(max([*(sum(v for *_, v in seg) for seg in stacks), 1.0]), 4, 0.01)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    slot = plot_w / WEEKS
    base = PAD_T + plot_h
    bars = []
    for i, (ws, seg) in enumerate(zip(week_starts, stacks, strict=True)):
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
    grid = [
        {"y": round(base - plot_h * v / top, 1), "label": money(v).replace(".00", "")}
        for v in tick_values
    ]
    xticks = month_ticks(week_starts, lambda i: round(PAD_L + (i + 0.5) * slot, 1))

    # --- per-app table
    downloads = dict(
        SalesDay.objects.filter(ACTIVE_ROWS, date__range=(since, end))
        .values_list("app_id")
        .annotate(Sum("downloads"))
    )
    app_rows = []
    year_total = sum(year_by_app.values())
    for app_id in ranked:
        now = now_by_app[app_id]
        product = by_app_product[app_id]
        dl = downloads.get(app_id, 0)
        share = 100 * year_by_app[app_id] / year_total if year_total else 0
        weekly = [by_week[(w, app_id)] for w in range(WEEKS - 12, WEEKS)]
        app_rows.append({
            "app": apps[app_id], "cls": color.get(app_id, "other"),
            "recent": now, "change": pct_change(before_by_app[app_id], now),
            "year": year_by_app[app_id], "share": share, "share_w": round(1.2 * share, 1),
            "per_1k": 1000 * now / dl if dl else None,
            "top_product": max(product, key=product.get) if product else "",
            "spark": spark(weekly, 96, 24),
        })  # fmt: skip

    # --- products and countries (the year)
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
    total, total_before = sum(now_by_app.values()), sum(before_by_app.values())
    dl_total = sum(downloads.values())
    return {
        "total": total,
        "change": pct_change(total_before, total),
        "year": year_total,
        "paid_units": paid_units,
        "per_1k": 1000 * total / dl_total if dl_total else None,
        "best": app_rows[0] if app_rows else None,
        "series": [{"name": n, "cls": c} for n, c, _ in series],
        "bars": bars,
        "grid": grid,
        "xticks": xticks,
        "apps": app_rows,
        "products": top_products,
        "countries": country_bars,
        "unconverted": [(c, round(v, 2)) for c, v in sorted(unconverted.items())],
        "width": W, "height": H, "pad_l": PAD_L, "plot_r": W - PAD_R,
        "pad_t": PAD_T, "plot_h": plot_h,
    }  # fmt: skip


# --- one app, for its page ------------------------------------------------------------------

APP_H = 200


def app_revenue(app: App, end: date) -> dict | None:
    """One app's proceeds: the last 30 days against the 30 before, the past year by week,
    and its products and storefronts over the year. None when it never earned anything."""
    year_start = end - timedelta(days=364)
    lines = _lines(year_start, end, app=app)
    if not lines:
        return None
    recent = Period(end, 30)
    name_of = {ln.sku: ln.title for ln in sorted(lines, key=lambda ln: ln.day)}
    by_week = defaultdict(float)
    products = defaultdict(lambda: {"usd": 0.0, "units": 0, "recent": 0.0})
    countries = defaultdict(float)
    unconverted = defaultdict(Decimal)
    now = before = year = 0.0
    for ln in lines:
        if ln.usd is None:
            unconverted[ln.currency] += ln.local
            continue
        usd = float(ln.usd)
        year += usd
        week = WEEKS - 1 - (end - ln.day).days // 7
        if week >= 0:
            by_week[week] += usd
        p = products[name_of[ln.sku]]
        p["usd"] += usd
        p["units"] += ln.units
        if ln.day >= recent.start:
            now += usd
            p["recent"] += usd
        elif ln.day >= recent.prev_start:
            before += usd
        countries[ln.country] += usd

    week_starts = [end - timedelta(days=7 * (WEEKS - 1 - i) + 6) for i in range(WEEKS)]
    top, tick_values = nice_axis(max([*by_week.values(), 1.0]), 4, 0.01)
    plot_w, plot_h = W - PAD_L - PAD_R, APP_H - PAD_T - PAD_B
    slot = plot_w / WEEKS
    base = PAD_T + plot_h
    bars = []
    for i, ws in enumerate(week_starts):
        v = by_week[i]
        h = plot_h * max(v, 0) / top
        bars.append({
            "x": round(PAD_L + i * slot + 2, 1), "y": round(base - h + 1, 1),
            "w": round(slot - 4, 1), "h": round(max(h - 2, 0.8 if v else 0), 1),
            "hit_x": round(PAD_L + i * slot, 1), "hit_w": round(slot, 1),
            "tip": f"Week of {ws:%b} {ws.day}\n{money(v)}",
        })  # fmt: skip
    grid = [
        {"y": round(base - plot_h * v / top, 1), "label": money(v).replace(".00", "")}
        for v in tick_values
    ]
    downloads = (
        SalesDay.objects.filter(app=app, date__range=(recent.start, end)).aggregate(
            n=Sum("downloads")
        )["n"]
        or 0
    )
    country_rows = sorted(countries.items(), key=lambda kv: -kv[1])[:6]
    cmax = max([v for _, v in country_rows] or [1])
    return {
        "recent": now,
        "change": pct_change(before, now),
        "year": year,
        "per_1k": 1000 * now / downloads if downloads else None,
        "spark": spark([by_week[i] for i in range(WEEKS - 13, WEEKS)], 120, 32),  # 13 weeks
        "bars": bars,
        "grid": grid,
        "xticks": month_ticks(week_starts, lambda i: round(PAD_L + (i + 0.5) * slot, 1)),
        "products": sorted(
            ({"title": t, **v} for t, v in products.items()), key=lambda p: -p["usd"]
        )[:6],
        "countries": [
            {"code": c, "usd": v, "w": round(100 * max(v, 0) / cmax, 1)} for c, v in country_rows
        ],
        "unconverted": [(c, round(v, 2)) for c, v in sorted(unconverted.items())],
        "width": W, "height": APP_H, "pad_l": PAD_L, "plot_r": W - PAD_R,
        "pad_t": PAD_T, "plot_h": plot_h,
    }  # fmt: skip
