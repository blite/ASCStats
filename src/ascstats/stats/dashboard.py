"""Numbers and SVG geometry for the dashboard. Views stay thin and templates only place the
shapes computed here, so every chart is plain server-rendered SVG (no JS chart library)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Max, Min, Q, Sum

from .charts import Period, nice_axis, pct_change, smooth, spark
from .charts import days as _days
from .charts import fmt_day as _fmt_day
from .crashes import version_crashes
from .models import ACTIVE_ROWS, IOS, App, AppEvent, IngestionRun, Report, UtcDay
from .ratings import current, daily_new_ratings, version_ratings
from .revenue import _lines as revenue_lines
from .revenue import money
from .versions import VersionImpact, version_impact


def last_day() -> date | None:
    """The newest UTC day with downloads. Apple's purchases report reaches a day further than
    its downloads report, so a day with only purchases is not shown yet: it would read as a
    day with no downloads."""
    downloads = Q(downloads__gt=0) | Q(redownloads__gt=0) | Q(updates__gt=0)
    return UtcDay.objects.filter(ACTIVE_ROWS, downloads).aggregate(last=Max("date"))["last"]


def first_day() -> date | None:
    return UtcDay.objects.filter(ACTIVE_ROWS).aggregate(first=Min("date"))["first"]


def _daily(field: str, start: date, end: date, **filters) -> dict[date, int]:
    """One UtcDay measure per day (all platforms) for active apps."""
    return dict(
        UtcDay.objects.filter(ACTIVE_ROWS, date__range=(start, end), **filters)
        .values_list("date")
        .annotate(Sum(field))
    )


def _effect(impact: VersionImpact) -> tuple[str, str]:
    """(css class, one-line verdict) for a release, from its trend-adjusted delta."""
    trend = impact.trend
    if trend.change_pct is None:
        return "unknown", "not enough history for a trend"
    verdict = f"{trend.change_pct:+.0f}% vs trend (t {trend.t_score})"
    if not trend.significant:
        return "neutral", verdict + ", within noise"
    return ("up" if trend.change_pct > 0 else "down"), verdict


# --- stat tiles ----------------------------------------------------------------------------


@dataclass
class Tile:
    label: str
    value: float
    previous: float
    spark: str = ""  # polyline points, 120x32
    note: str = ""
    usd: bool = False  # a dollar amount, not a count

    @property
    def change_pct(self) -> float | None:
        return 100 * (self.value - self.previous) / self.previous if self.previous else None

    @property
    def direction(self) -> str:
        if self.change_pct is None or abs(self.change_pct) < 0.5:
            return "flat"
        return "up" if self.change_pct > 0 else "down"

    @property
    def shown(self) -> str:
        return money(self.value) if self.usd else f"{self.value:,}"


def tiles(end: date, period: Period | None = None, app: App | None = None) -> list[Tile]:
    """The headline measures for every active app (downloads, in-app purchases, proceeds), or
    for one (downloads and in-app purchases: its page has a Revenue section of its own)."""
    period = period or Period(end)
    only = {"app": app} if app else {}
    start, prev_start = period.start, period.prev_start
    spark_days = _days(end - timedelta(days=89), end)
    note = f"{period.label} {period.versus}"
    out = []
    for label, measure in (("Downloads", "downloads"), ("In-app purchases", "iap_units")):
        daily = _daily(measure, min(spark_days[0], start), end, **only)  # all time reaches further
        current = sum(v for d, v in daily.items() if d >= start)
        previous = sum(_daily(measure, prev_start, start - timedelta(days=1), **only).values())
        series = smooth([daily.get(d, 0) for d in spark_days])
        out.append(Tile(label, current, previous, spark(series, 120, 32), note))
    if app is None:
        proceeds: dict[date, float] = defaultdict(float)
        for ln in revenue_lines(min(spark_days[0], prev_start), end):
            if ln.usd is not None:  # the Revenue page lists what has no rate yet
                proceeds[ln.day] += float(ln.usd)
        current = sum(v for d, v in proceeds.items() if d >= start)
        previous = sum(v for d, v in proceeds.items() if prev_start <= d < start)
        series = smooth([proceeds.get(d, 0.0) for d in spark_days])
        out.append(
            Tile("Proceeds", current, previous, spark(series, 120, 32), note + " · USD", usd=True)
        )
    return out


# --- week by week ---------------------------------------------------------------------------

WEEK_ROWS = 52  # weeks kept; the page shows the newest few and folds the rest away


@dataclass
class Week:
    start: date  # a Monday
    days: int  # days with data so far: 7, or fewer for the week ``end`` falls in
    downloads: int
    revenue: float  # USD
    downloads_change: float | None = None  # vs the same days of the week before
    revenue_change: float | None = None
    downloads_w: float = 0  # inline bar widths, 0-100
    revenue_w: float = 0

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.days - 1)

    @property
    def partial(self) -> bool:
        return self.days < 7

    @property
    def per_1k(self) -> float | None:
        return 1000 * self.revenue / self.downloads if self.downloads > 0 else None

    @property
    def revenue_label(self) -> str:
        return money(self.revenue)


def weekly(end: date, weeks: int = WEEK_ROWS) -> dict:
    """Downloads and proceeds (USD) per calendar week, Monday to Sunday, newest first, for
    every active app. The week ``end`` falls in is usually unfinished: it is compared with
    the same weekdays of the week before, never with a whole week."""
    this_monday = end - timedelta(days=end.weekday())
    first = this_monday - timedelta(weeks=weeks)  # one more week, for the oldest's change
    downloads = _daily("downloads", first, end)
    proceeds: dict[date, float] = defaultdict(float)
    unconverted: set[str] = set()
    for ln in revenue_lines(first, end):
        if ln.usd is None:
            unconverted.add(ln.currency)
        else:
            proceeds[ln.day] += float(ln.usd)

    def totals(monday: date, n: int) -> tuple[int, float]:
        span = _days(monday, monday + timedelta(days=n - 1))
        return sum(downloads.get(d, 0) for d in span), sum(proceeds.get(d, 0.0) for d in span)

    rows = []
    for i in range(weeks):
        monday = this_monday - timedelta(weeks=i)
        n = (end - monday).days + 1 if i == 0 else 7
        dl, usd = totals(monday, n)
        dl_before, usd_before = totals(monday - timedelta(weeks=1), n)
        week = Week(monday, n, dl, usd, pct_change(dl_before, dl), pct_change(usd_before, usd))
        rows.append(week)
    # the oldest weeks precede the first import: drop them rather than show a run of zeros
    since = first_day()
    rows = [w for w in rows if since is None or w.end >= since]
    dl_peak = max([w.downloads for w in rows] + [1])
    usd_peak = max([w.revenue for w in rows] + [1.0])
    for w in rows:
        w.downloads_w = round(100 * max(w.downloads, 0) / dl_peak, 1)
        w.revenue_w = round(100 * max(w.revenue, 0) / usd_peak, 1)
    full = [w for w in rows if not w.partial]
    return {
        "weeks": rows,
        "best_downloads": max(full, key=lambda w: w.downloads, default=None),
        "best_revenue": max(full, key=lambda w: w.revenue, default=None),
        "unconverted": sorted(unconverted),
    }


# --- the charts over the period: release lanes, and every app on one chart ----------------

LANE_X0, LANE_X1, LANE_H, LANE_TOP = 200, 1020, 50, 28
MIN_CHART_DAYS = 7  # a one-day period is drawn with the week to it: one point is no line
SMOOTH_FROM = 32  # spans this long or longer are drawn as a 7-day average, shorter ones raw
WARM_UP = 6  # days before the span the 7-day average reads, so its first days are whole


def chart_start(end: date, period: Period) -> date:
    """The first day the Overview's charts draw for ``period``."""
    return min(period.start, end - timedelta(days=MIN_CHART_DAYS - 1))


def _per_app_daily(apps: list[App], start: date, end: date) -> dict[int, dict[date, int]]:
    """Downloads per app per day (all platforms), in one query."""
    out: dict[int, dict[date, int]] = defaultdict(dict)
    rows = (
        UtcDay.objects.filter(ACTIVE_ROWS, app__in=apps, date__range=(start, end))
        .values_list("app", "date")
        .annotate(Sum("downloads"))
    )
    for app_id, day, n in rows:
        out[app_id][day] = n or 0
    return out


def _drawn(daily: dict[date, int], span: list[date]) -> list[float]:
    """The values a chart draws over ``span``: raw for a short span, else a 7-day average
    that reads the days before the span, so it does not start low (none before the app's
    first day with data, so it does not start low on all time either)."""
    if len(span) < SMOOTH_FROM:
        return [float(daily.get(d, 0)) for d in span]
    first = min(daily, default=span[0])  # before an app's first day there is nothing to average
    lead = max(span[0] - timedelta(days=WARM_UP), min(first, span[0]))
    warm = (span[0] - lead).days
    return smooth([daily.get(d, 0) for d in _days(lead, span[-1])])[warm:]


def _xticks(start: date, end: date, x) -> list[dict]:
    """Day labels for a short span, Mondays for a month, months for a year, quarters beyond."""
    n = (end - start).days + 1
    if n <= 10:
        return [{"x": x(d), "label": f"{d:%b} {d.day}"} for d in _days(start, end)]
    if n <= 31:
        return [
            {"x": x(d), "label": f"{d:%b} {d.day}"} for d in _days(start, end) if d.weekday() == 0
        ]
    months = 1 if n <= 400 else 3
    ticks, month = [], date(start.year, start.month, 1)
    while month <= end:
        if month >= start and (month.month - 1) % months == 0:
            label = f"{month:%b}" if month.month != 1 else f"{month:%b %Y}"
            ticks.append({"x": x(month), "label": label})
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    return ticks


@dataclass
class Lane:
    app: App
    y: float
    area: str
    line: str
    total: int
    dots: list[dict] = field(default_factory=list)


@dataclass
class Scoreboard:
    lanes: list[Lane]
    ticks: list[dict]
    height: int
    releases: list[dict]  # flat list for the table view, newest first
    counts: dict[str, int]
    smoothed: bool = True


def scoreboard(end: date, start: date | None = None, apps: list[App] | None = None) -> Scoreboard:
    """One lane per app with downloads from ``start`` (a year before ``end`` by default) to
    ``end``, busiest first, and a dot on each iOS release in it. ``apps``: the active apps
    by default."""
    start = start or end - timedelta(days=364)
    span = _days(start, end)
    step = (LANE_X1 - LANE_X0) / max(len(span) - 1, 1)

    def x(day: date) -> float:
        return round(LANE_X0 + (day - start).days * step, 1)

    pool = list(App.objects.active()) if apps is None else apps
    lead = start - timedelta(days=WARM_UP)
    per_app = _per_app_daily(pool, lead, end)
    totals = {a.pk: sum(v for d, v in per_app[a.pk].items() if d >= start) for a in pool}
    apps = sorted((a for a in pool if totals[a.pk] > 0), key=lambda a: -totals[a.pk])
    lanes, releases = [], []
    counts = {"up": 0, "down": 0, "neutral": 0, "unknown": 0}
    for n, app in enumerate(apps):
        top = LANE_TOP + n * LANE_H
        base, height = top + LANE_H - 10, LANE_H - 18
        series = _drawn(per_app[app.pk], span)
        peak = max(series) or 1
        pts = [(LANE_X0 + i * step, base - height * max(v, 0) / peak) for i, v in enumerate(series)]
        line = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        area = f"M{LANE_X0},{base} L" + line.replace(" ", " L") + f" L{LANE_X1},{base} Z"
        lane = Lane(app, top, area, line, totals[app.pk])
        for impact in version_impact(app, platform=IOS, since=start):
            if not impact.before_days or impact.released_on > end:
                continue
            kind, verdict = _effect(impact)
            counts[kind] += 1
            naive = impact.naive.change_pct
            tip = (
                f"{app.name} {impact.version}\n{_fmt_day(impact.released_on)}\n"
                f"{verdict}\nnaive before/after: " + ("n/a" if naive is None else f"{naive:+.0f}%")
            )
            dot = {"x": x(impact.released_on), "y": base - height - 4, "kind": kind, "tip": tip}
            lane.dots.append(dot)
            releases.append(
                {
                    "app": app.name,
                    "apple_id": app.apple_id,
                    "impact": impact,
                    "kind": kind,
                    "verdict": verdict,
                }
            )
        lanes.append(lane)
    releases.sort(key=lambda r: r["impact"].released_on, reverse=True)
    height = LANE_TOP + len(lanes) * LANE_H + 24
    return Scoreboard(
        lanes, _xticks(start, end, x), height, releases, counts, len(span) >= SMOOTH_FROM
    )


TW, TH, T_L, T_R, T_T, T_B = 1040, 300, 52, 8, 12, 28
SLOTS = 5  # apps with a color of their own, as on Revenue; the rest stack as "Other"
DAILY_UP_TO = 92  # days drawn a bar a day; a longer span is drawn a bar a week


def together(end: date, start: date, apps: list[App]) -> dict:
    """Every app's downloads in one bar chart, each bar stacked by app as on the Revenue
    page: the busiest apps at the bottom in a color each, the rest together as "Other". A
    bar a day, or a week (the newest ending on ``end``) for a span longer than a quarter."""
    span = _days(start, end)
    per_app = _per_app_daily(apps, start, end)
    totals = {a.pk: sum(per_app[a.pk].values()) for a in apps}
    ranked = sorted((a for a in apps if totals[a.pk] > 0), key=lambda a: -totals[a.pk])
    series = [
        {"name": a.name, "cls": f"c{i + 1}", "ids": {a.pk}, "app": a}
        for i, a in enumerate(ranked[:SLOTS])
    ]
    if rest := ranked[SLOTS:]:
        n = len(rest)
        series.append(
            {
                "name": f"Other ({n} app{'s' if n != 1 else ''})",
                "cls": "other",
                "ids": {a.pk for a in rest},
                "app": None,
            }
        )
    size = 1 if len(span) <= DAILY_UP_TO else 7
    buckets = [span[max(i - size + 1, 0) : i + 1] for i in range(len(span) - 1, -1, -size)][::-1]
    stacks = [
        [sum(per_app[pk].get(d, 0) for pk in s["ids"] for d in days) for s in series]
        for days in buckets
    ]
    top, tick_values = nice_axis(max([*(sum(st) for st in stacks), 1]))
    plot_w, plot_h = TW - T_L - T_R, TH - T_T - T_B
    slot = plot_w / len(buckets)
    gap = min(2.0, slot * 0.15)
    base = T_T + plot_h
    bars = []
    for i, (days, stack) in enumerate(zip(buckets, stacks, strict=True)):
        x, y = T_L + i * slot, base
        when = (
            _fmt_day(days[0]) if len(days) == 1 else f"{_fmt_day(days[0])} to {_fmt_day(days[-1])}"
        )
        tip = [when, f"{sum(stack):,} downloads"]
        rects = []
        for s, v in zip(series, stack, strict=True):
            if v > 0:
                h = plot_h * v / top
                rects.append({"x": round(x + gap, 1), "y": round(y - h + 0.5, 1),
                              "w": round(max(slot - 2 * gap, 0.6), 1),
                              "h": round(max(h - 1, 0.8), 1), "cls": s["cls"]})  # fmt: skip
                y -= h
                tip.append(f"{s['name']}: {v:,}")
        bars.append(
            {"rects": rects, "tip": "\n".join(tip), "hit_x": round(x, 1), "hit_w": round(slot, 2)}
        )

    def xc(day: date) -> float:  # the middle of the bar holding ``day``
        i = next(n for n, days in enumerate(buckets) if days[-1] >= day)
        return round(T_L + (i + 0.5) * slot, 1)

    return {
        "series": series,
        "bars": bars,
        "weekly": size == 7,
        "grid": [
            {"y": round(base - plot_h * v / top, 1), "label": f"{v:,.0f}"} for v in tick_values
        ],
        "xticks": _xticks(start, end, xc),
        "width": TW,
        "height": TH,
        "pad_l": T_L,
        "plot_r": TW - T_R,
        "pad_t": T_T,
        "plot_h": plot_h,
    }


# --- pipeline health calendar --------------------------------------------------------------

CELL = 13  # 11px square + 2px gap


@dataclass
class Calendar:
    cells: list[dict]
    months: list[dict]
    width: int
    counts: dict[str, int]


def pipeline_calendar(
    end: date, days: int = 371, first: date | None = None, selected: date | None = None
) -> Calendar:
    """One square per day up to ``end``. With ``first``, each day from it to ``end`` carries
    ``day`` so its square opens the Overview for that day; ``selected`` is the day shown."""
    start = end - timedelta(days=days - 1)
    start -= timedelta(days=(start.weekday() + 1) % 7)  # columns start on Sunday
    kinds = {
        IngestionRun.Status.SUCCEEDED: "loaded",
        IngestionRun.Status.NOT_READY: "empty",
        IngestionRun.Status.FAILED: "failed",
    }
    status: dict[date, str] = {}  # the latest run for a day decides
    for day, st in (
        IngestionRun.objects.filter(report=Report.SALES, report_date__range=(start, end))
        .order_by("started_at")
        .values_list("report_date", "status")
    ):
        if st in kinds:
            status[day] = kinds[st]
    labels = {
        "loaded": "report loaded",
        "empty": "no sales that day",
        "failed": "import failed",
        "none": "not imported",
    }
    cells, months, counts = [], [], {k: 0 for k in labels}
    for i, day in enumerate(_days(start, end)):
        col, row = i // 7, (day.weekday() + 1) % 7
        kind = status.get(day, "none")
        counts[kind] += 1
        cells.append(
            {
                "x": 28 + col * CELL,
                "y": 18 + row * CELL,
                "kind": kind,
                "tip": f"{_fmt_day(day)}\n{labels[kind]}",
                "day": day if first is not None and day >= first else None,
                "selected": day == selected,
            }
        )
        if day.day == 1:
            months.append({"x": 28 + col * CELL, "label": f"{day:%b}"})
    return Calendar(cells, months, 28 + (len(cells) // 7 + 1) * CELL, counts)


# --- app deep-dive -------------------------------------------------------------------------

W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 300, 44, 12, 34, 28


def app_detail(app: App, end: date, days: int) -> dict:
    start = end - timedelta(days=days - 1)
    span = _days(start, end)
    daily = _daily("downloads", start, end, app=app)
    values = [daily.get(d, 0) for d in span]
    avg = smooth(values)
    impacts = version_impact(app)
    projections = []
    for imp in impacts:
        if imp.platform == IOS and imp.projection and imp.released_on >= start:
            days_live = _days(imp.released_on, imp.released_on + timedelta(len(imp.projection) - 1))
            projections.append(list(zip(days_live, imp.projection, strict=True)))
    peak = max([*values, *(v for p in projections for _, v in p), 1])
    nice, tick_values = nice_axis(peak)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    step = plot_w / len(span)
    base = PAD_T + plot_h

    def y(v: float) -> float:
        return round(base - plot_h * v / nice, 1)

    def xc(day: date) -> float:
        return round(PAD_L + ((day - start).days + 0.5) * step, 1)

    version_on = {}
    for imp in sorted(impacts, key=lambda i: i.released_on):
        if imp.platform == IOS:
            for d in span:
                if d >= imp.released_on:
                    version_on[d] = imp.version
    bars = []
    for i, (day, v) in enumerate(zip(span, values, strict=True)):
        bx = PAD_L + i * step
        bars.append(
            {
                "x": round(bx + min(1, step * 0.15), 1),
                "w": round(max(step - 2 * min(1, step * 0.15), 0.6), 1),
                "y": y(v),
                "h": round(base - y(v), 1),
                "hit_x": round(bx, 1),
                "hit_w": round(step, 2),
                "tip": f"{_fmt_day(day)}\n{v} downloads · 7-day avg {avg[i]:.1f}"
                + (f"\niOS {version_on[day]}" if day in version_on else ""),
            }
        )
    markers = []
    for imp in impacts:  # iOS only: Mac versions often ship the same day and are in the table
        if imp.platform == IOS and start < imp.released_on <= end and imp.before_days:
            kind, verdict = _effect(imp)
            markers.append(
                {
                    "x": xc(imp.released_on) - step / 2,
                    "label": imp.version,
                    "kind": kind,
                    "tip": f"iOS {imp.version} · {_fmt_day(imp.released_on)}\n{verdict}",
                }
            )
    for ev in AppEvent.objects.filter(app=app, date__range=(start, end)):
        markers.append(
            {
                "x": xc(ev.date) - step / 2,
                "label": ev.label,
                "kind": "event",
                "tip": f"{ev.label} · {_fmt_day(ev.date)}" + (f"\n{ev.note}" if ev.note else ""),
            }
        )
    grid = [{"y": y(v), "label": f"{v:g}"} for v in tick_values]
    xticks = [
        {"x": xc(d), "label": f"{d:%b} {d.day}"}
        for d in span
        if (days <= 31 and d.weekday() == 0) or (days > 31 and d.day == 1)
    ]
    rows = sorted(
        (i for i in impacts if i.before_days),
        key=lambda i: i.released_on,
        reverse=True,
    )
    new_ratings = daily_new_ratings(app)  # one pass over the snapshots for both uses
    rated = {v.version: v for v in version_ratings(app, new_ratings)}
    crashed = version_crashes(app)
    table = []
    for imp in rows:
        kind, verdict = _effect(imp)
        trend = imp.trend
        pct = max(-100.0, min(100.0, trend.change_pct or 0))
        table.append(
            {
                "impact": imp,
                "kind": kind if imp.platform == IOS or trend.change_pct is not None else "unknown",
                "naive": imp.naive,
                "trend": trend,
                "iap": imp.iap,
                "ratings": rated.get(imp.version) if imp.platform == IOS else None,
                "crashes": crashed.get(imp.version) if imp.platform == IOS else None,
                "bar_x": 60 if pct >= 0 else round(60 + 0.58 * pct, 1),
                "bar_w": round(abs(0.58 * pct), 1),
            }
        )
    return {
        "app": app,
        "days": days,
        "start": start,
        "end": end,
        "total": sum(values),
        "best_day": max(zip(values, span, strict=True)) if any(values) else None,
        "bars": bars,
        "avg_line": " ".join(f"{xc(d)},{y(v)}" for d, v in zip(span, avg, strict=True)),
        "projections": [" ".join(f"{xc(d)},{y(v)}" for d, v in p) for p in projections],
        "markers": markers,
        "has_events": any(m["kind"] == "event" for m in markers),
        "grid": grid,
        "xticks": xticks,
        "table": table,
        "width": W,
        "height": H,
        "pad_l": PAD_L,
        "plot_r": W - PAD_R,
        "pad_t": PAD_T,
        "base": base,
        "plot_h": plot_h,
        "rating": current(app, new_ratings),
    }
