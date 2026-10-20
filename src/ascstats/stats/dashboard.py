"""Numbers and SVG geometry for the dashboard. Views stay thin and templates only place the
shapes computed here, so every chart is plain server-rendered SVG (no JS chart library)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Max, Sum

from .charts import days as _days
from .charts import fmt_day as _fmt_day
from .charts import nice_axis, smooth, spark
from .crashes import version_crashes
from .models import ACTIVE_ROWS, IOS, App, IngestionRun, Report, SalesDay
from .ratings import current, daily_new_ratings, version_ratings
from .versions import VersionImpact, version_impact


def last_day() -> date | None:
    return SalesDay.objects.filter(ACTIVE_ROWS).aggregate(last=Max("date"))["last"]


def _daily(field: str, start: date, end: date, **filters) -> dict[date, int]:
    """One SalesDay measure per day (all platforms) for active apps."""
    return dict(
        SalesDay.objects.filter(ACTIVE_ROWS, date__range=(start, end), **filters)
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
    value: int
    previous: int
    spark: str = ""  # polyline points, 120x32
    note: str = ""

    @property
    def change_pct(self) -> float | None:
        return 100 * (self.value - self.previous) / self.previous if self.previous else None

    @property
    def direction(self) -> str:
        if self.change_pct is None or abs(self.change_pct) < 0.5:
            return "flat"
        return "up" if self.change_pct > 0 else "down"


def tiles(end: date) -> list[Tile]:
    start, prev_start = end - timedelta(days=29), end - timedelta(days=59)
    spark_days = _days(end - timedelta(days=89), end)
    out = []
    for label, measure in (
        ("Downloads", "downloads"),
        ("In-app purchases", "iap_units"),
        ("Updates installed", "updates"),
    ):
        daily = _daily(measure, spark_days[0], end)
        current = sum(v for d, v in daily.items() if d >= start)
        previous = sum(_daily(measure, prev_start, start - timedelta(days=1)).values())
        series = smooth([daily.get(d, 0) for d in spark_days])
        out.append(
            Tile(label, current, previous, spark(series, 120, 32), "last 30 days vs the 30 before")
        )
    return out


# --- release scoreboard (one lane per app) -------------------------------------------------

LANE_X0, LANE_X1, LANE_H, LANE_TOP = 200, 1020, 50, 28


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


def scoreboard(end: date, days: int = 365) -> Scoreboard:
    start = end - timedelta(days=days - 1)
    span = _days(start, end)
    step = (LANE_X1 - LANE_X0) / max(len(span) - 1, 1)

    def x(day: date) -> float:
        return round(LANE_X0 + (day - start).days * step, 1)

    totals = dict(
        SalesDay.objects.filter(ACTIVE_ROWS, date__range=(start, end))
        .values_list("app")
        .annotate(Sum("downloads"))
    )
    apps = sorted(
        (a for a in App.objects.active() if totals.get(a.pk, 0) > 0),
        key=lambda a: -totals[a.pk],
    )
    lanes, releases = [], []
    counts = {"up": 0, "down": 0, "neutral": 0, "unknown": 0}
    for n, app in enumerate(apps):
        top = LANE_TOP + n * LANE_H
        base, height = top + LANE_H - 10, LANE_H - 18
        daily = _daily("downloads", start, end, app=app)
        series = smooth([daily.get(d, 0) for d in span])
        peak = max(series) or 1
        pts = [(LANE_X0 + i * step, base - height * v / peak) for i, v in enumerate(series)]
        line = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        area = f"M{LANE_X0},{base} L" + line.replace(" ", " L") + f" L{LANE_X1},{base} Z"
        lane = Lane(app, top, area, line, totals[app.pk])
        for impact in version_impact(app, platform=IOS, since=start):
            if not impact.before_days:
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
            releases.append({"app": app.name, "impact": impact, "kind": kind, "verdict": verdict})
        lanes.append(lane)
    ticks = []
    month = date(start.year, start.month, 1)
    while month <= end:
        if month >= start:
            label = f"{month:%b}" if month.month != 1 else f"{month:%b %Y}"
            ticks.append({"x": x(month), "label": label})
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    releases.sort(key=lambda r: r["impact"].released_on, reverse=True)
    return Scoreboard(lanes, ticks, LANE_TOP + len(lanes) * LANE_H + 24, releases, counts)


# --- pipeline health calendar --------------------------------------------------------------

CELL = 13  # 11px square + 2px gap


@dataclass
class Calendar:
    cells: list[dict]
    months: list[dict]
    width: int
    counts: dict[str, int]


def pipeline_calendar(end: date, days: int = 371) -> Calendar:
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
