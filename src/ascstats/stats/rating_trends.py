"""One app's star ratings by country and over time, for its Ratings tab.

Built from the daily public snapshots (``RatingSnapshot``). A quiet storefront is only asked
weekly, so a day's total carries each storefront's latest snapshot forward, and its new ratings
are the differences from each storefront's previous snapshot (a storefront's first snapshot
is history, not new ratings).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import groupby

from django.db.models import Max

from .charts import days, fmt_day, month_ticks, nice_axis, nice_step
from .models import App, RatingSnapshot
from .territories import flag, name

# Time frames the chart can show, by URL slug; None is everything snapshotted
WINDOWS = {"30d": 30, "90d": 90, "1y": 365, "all": None}
WINDOW_LABELS = {"30d": "30 days", "90d": "90 days", "1y": "1 year", "all": "All"}
DEFAULT_WINDOW = "90d"
ALL_COUNTRIES = "all"
FRESH_DAYS = 14  # a storefront with no snapshot this long before the latest has dropped out

W, H, PAD_L, PAD_R, PAD_T, PAD_B = 640, 240, 8, 48, 12, 24


def _grade(average: float | None) -> str:
    """The star's color class: green from 4, amber from 3, red below."""
    if average is None:
        return "none"
    return "good" if average >= 4 else "fair" if average >= 3 else "poor"


@dataclass
class CountryRow:
    code: str
    count: int
    average: float | None
    new: int  # ratings given since this storefront's previous snapshot, on the latest day

    @property
    def name(self) -> str:
        return name(self.code)

    @property
    def flag(self) -> str:
        return flag(self.code)

    @property
    def grade(self) -> str:
        return _grade(self.average)


def countries(app: App) -> dict | None:
    """Each storefront's latest totals, most ratings first, with the app's totals across
    them and the ratings given on the latest day. None before the first snapshot."""
    latest = RatingSnapshot.objects.filter(app=app).aggregate(d=Max("date"))["d"]
    if latest is None:
        return None
    last: dict[str, tuple[date, int, int]] = {}
    new: dict[str, int] = {}
    for front, day, count, stars in (
        RatingSnapshot.objects.filter(app=app)
        .order_by("date")
        .values_list("storefront", "date", "count", "star_sum")
    ):
        if front in last:
            new[front] = count - last[front][1]
        last[front] = (day, count, stars)
    rows = [
        CountryRow(
            code=front,
            count=count,
            average=stars / count if count else None,
            new=new.get(front, 0) if day == latest else 0,
        )
        for front, (day, count, stars) in last.items()
        if day > latest - timedelta(days=FRESH_DAYS)
    ]
    rows.sort(key=lambda r: (-r.count, r.name))
    count = sum(r.count for r in rows)
    stars = sum(last[r.code][2] for r in rows)
    average = stars / count if count else None
    return {
        "rows": rows,
        "count": count,
        "average": average,
        "grade": _grade(average),
        "new": sum(r.new for r in rows),
        "date": latest,
    }


@dataclass
class Point:
    day: date
    count: int  # ratings to date
    stars: int  # their total stars
    new: int | None  # ratings given since the previous snapshot; None on the first one
    new_stars: int | None

    @property
    def average(self) -> float | None:
        return self.stars / self.count if self.count else None


def _points(app: App, country: str) -> list[Point]:
    snaps = RatingSnapshot.objects.filter(app=app).order_by("date", "storefront")
    if country != ALL_COUNTRIES:
        snaps = snaps.filter(storefront=country)
    current: dict[str, tuple[int, int]] = {}
    out = []
    for day, group in groupby(
        snaps.values_list("date", "storefront", "count", "star_sum"), key=lambda s: s[0]
    ):
        new = new_stars = 0
        seen_before = bool(current)
        for _, front, count, stars in group:
            if front in current:
                new += count - current[front][0]
                new_stars += stars - current[front][1]
            current[front] = (count, stars)
        out.append(
            Point(
                day=day,
                count=sum(c for c, _ in current.values()),
                stars=sum(s for _, s in current.values()),
                new=new if seen_before else None,
                new_stars=new_stars if seen_before else None,
            )
        )
    return out


def _chart(points: list[Point], value, top: float, bottom: float, ticks: list[float], fmt) -> dict:
    """Line, dots, hover columns and axes for one measure of ``points`` (oldest first)."""
    first, last = points[0].day, points[-1].day
    span = max((last - first).days, 1)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    base = PAD_T + plot_h

    def x(day: date) -> float:
        return round(PAD_L + plot_w * (day - first).days / span, 1)

    def y(v: float) -> float:
        return round(base - plot_h * (v - bottom) / ((top - bottom) or 1), 1)

    coords = [(x(p.day), y(value(p))) for p in points if value(p) is not None]
    xs = [x(p.day) for p in points]
    cols = []
    for i, p in enumerate(points):
        left = PAD_L if i == 0 else (xs[i - 1] + xs[i]) / 2
        right = PAD_L + plot_w if i == len(points) - 1 else (xs[i] + xs[i + 1]) / 2
        lines = [fmt_day(p.day), f"{p.count:,} ratings" + (f" (+{p.new:,})" if p.new else "")]
        if p.average is not None:
            lines.append(f"{p.average:.2f} ★ average")
        cols.append({"x": round(left, 1), "w": round(right - left, 1), "tip": "\n".join(lines)})
    every = days(first, last)
    return {
        "line": " ".join(f"{cx},{cy}" for cx, cy in coords),
        "area": (
            f"M{coords[0][0]},{base} L"
            + " L".join(f"{cx},{cy}" for cx, cy in coords)
            + f" L{coords[-1][0]},{base}Z"
        )
        if coords
        else "",
        "dots": [{"x": cx, "y": cy} for cx, cy in coords] if len(coords) <= 120 else [],
        "end": {"x": coords[-1][0], "y": coords[-1][1]} if coords else None,
        "cols": cols,
        "grid": [{"y": y(v), "label": fmt(v)} for v in ticks],
        "xticks": [  # a label at the very edge is anchored inward, so it is not cut off
            t | {"anchor": "start" if t["x"] < PAD_L + 16 else "middle"}
            for t in month_ticks(every, lambda i: x(every[i]), 56)
        ],
    }


def _short(n: float) -> str:
    return f"{n / 1000:.1f}K".replace(".0K", "K") if n >= 1000 else f"{n:.0f}"


def history(app: App, country: str, window: str) -> dict:
    """Ratings to date and their average over a time frame, in one storefront or all of
    them: two charts, the table under them, and the ratings given in the time frame."""
    points = _points(app, country)
    if WINDOWS[window] and points:
        start = points[-1].day - timedelta(days=WINDOWS[window] - 1)
        points = [p for p in points if p.day >= start]
    picked = None if country == ALL_COUNTRIES else {"name": name(country), "flag": flag(country)}
    out = {
        "country": picked,
        "window_label": WINDOW_LABELS[window],
        "rows": list(reversed(points)),
        "width": W, "height": H, "pad_l": PAD_L, "plot_r": W - PAD_R,
    }  # fmt: skip
    if not points:
        return out

    top, ticks = nice_axis(max(p.count for p in points) or 1, 4)
    out["count_chart"] = _chart(points, lambda p: p.count, top, 0, ticks, _short)

    averages = [p.average for p in points if p.average is not None]
    if averages:
        lo, hi = min(averages), max(averages)
        step = nice_step(max(hi - lo, 0.05), 4, 0.01)
        bottom = max(1.0, step * int(lo / step) - step)
        top = min(5.0, step * -(-hi // step) + step)
        ticks = []
        v = bottom
        while v <= top + 1e-9:
            ticks.append(round(v, 2))
            v += step
        out["average_chart"] = _chart(
            points, lambda p: p.average, top, bottom, ticks, lambda v: f"{v:.2f}"
        )

    new = sum(p.new or 0 for p in points)
    new_stars = sum(p.new_stars or 0 for p in points)
    out |= {
        "new": None if all(p.new is None for p in points) else new,
        "new_average": new_stars / new if new > 0 else None,
        "new_grade": _grade(new_stars / new if new > 0 else None),
        "first": points[0].day,
        "last": points[-1].day,
    }
    return out
