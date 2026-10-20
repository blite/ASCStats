"""Small helpers every server-rendered chart shares: date ranges, axis steps, sparklines,
month labels and percent change. One copy each, so every chart rounds and labels alike."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta


def days(start: date, end: date) -> list[date]:
    """Every day in [start, end]."""
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def smooth(values: Sequence[float], width: int = 7) -> list[float]:
    """Trailing moving average (shorter at the start)."""
    out, total = [], 0.0
    for i, v in enumerate(values):
        total += v
        if i >= width:
            total -= values[i - width]
        out.append(total / min(i + 1, width))
    return out


def nice_step(peak: float, max_ticks: int = 5, smallest: float = 1) -> float:
    """The smallest 1-2-5 step (from ``smallest`` up) covering ``peak`` in ``max_ticks``."""
    magnitude = smallest
    while True:
        for unit in (1, 2, 5):
            if peak / (unit * magnitude) <= max_ticks:
                return unit * magnitude
        magnitude *= 10


def nice_axis(peak: float, max_ticks: int = 5, smallest: float = 1) -> tuple[float, list[float]]:
    """(axis top, tick values from 0): the top is ``peak`` rounded up to a whole tick."""
    step = nice_step(peak, max_ticks, smallest)
    n = max(1, -(-peak // step))
    return step * n, [round(i * step, 10) for i in range(int(n) + 1)]


def spark(values: Sequence[float], width: int, height: int) -> str:
    """Polyline points for a sparkline; negatives sit on the baseline."""
    peak = max([*values, 0]) or 1
    step = width / max(len(values) - 1, 1)
    return " ".join(
        f"{i * step:.1f},{height - 2 - (height - 4) * max(v, 0) / peak:.1f}"
        for i, v in enumerate(values)
    )


def month_ticks(
    starts: Sequence[date], x: Callable[[int], float], min_gap: float = 30
) -> list[dict]:
    """A label wherever the month changes between consecutive buckets, skipping labels that
    would collide with the previous one."""
    ticks: list[dict] = []
    for i, d in enumerate(starts):
        if (i == 0 or d.month != starts[i - 1].month) and (
            not ticks or x(i) - ticks[-1]["x"] >= min_gap
        ):
            ticks.append({"x": x(i), "label": f"{d:%b}" if d.month != 1 else f"{d:%b %Y}"})
    return ticks


def pct_change(before: float | None, after: float | None) -> float | None:
    """Percent change from ``before`` to ``after``; None without a usable baseline."""
    if before is None or after is None or before == 0:
        return None
    return 100 * (after - before) / before


def fmt_day(day: date) -> str:
    return f"{day:%b} {day.day}, {day.year}"
