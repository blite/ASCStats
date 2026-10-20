"""The acquisition funnel, built to answer "what should I fix this week?" quickly.

Impressions -> product-page views -> first-time downloads -> in-app purchases. The App Store
funnel is not linear: many downloads come straight from a search result without a page view,
so the headline rate is downloads / unique impressions (install rate) and page tap-through is
a diagnostic beside it, never a step everything flows through.

Small numbers are the norm for these apps, so every rate here is:
- a ratio of sums over a window (never an average of daily ratios);
- shown with a 90% interval (Wilson for proportions, a Poisson analogue for purchases per
  download, which can exceed 1);
- shrunk toward the step's portfolio-wide rate for ranking (empirical-Bayes style), so a
  2-of-3 fluke cannot top the list;
- flagged as low-volume under 30 in the denominator or 5 in the numerator;
- missing (a gap) when Apple published nothing, never a zero.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Max, Sum

from .charts import days, month_ticks, nice_axis
from .models import ACTIVE_ROWS, IOS, App, AppVersion, ConversionMetric, FunnelDay

Z90 = 1.645
WINDOW = 28  # days in the leak strip's current window
REFERENCE = 90  # days before the window that form the app's own "usual"
WEEKS = 26
MIN_DENOMINATOR, MIN_NUMERATOR = 30, 5


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    numerator: str
    denominator: str
    per: int  # display multiplier: 100 for "%", or "per 100 downloads"
    prior_strength: float  # pseudo-observations pulling a small app toward the portfolio
    poisson: bool = False  # a count per unit (can exceed 1), not a proportion


UNIQUE_IMP = "unique_impressions"
STEPS = [
    Step("tap", "Impression → page view", "unique_page_views", UNIQUE_IMP, 100, 200),
    Step("install", "Impression → download", "first_time_downloads", UNIQUE_IMP, 100, 200),
    Step("iap", "Download → purchase", "iap_units", "units", 100, 30, poisson=True),
]
HEADLINE = "install"
HEADLINE_STEP = next(s for s in STEPS if s.key == HEADLINE)


def wilson(k: float, n: float, z: float = Z90) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def poisson_rate(k: float, n: float, z: float = Z90) -> tuple[float, float]:
    """Interval for k events over n units (a rate that may exceed 1)."""
    if n <= 0:
        return 0.0, float("inf")
    centre, half = k + z * z / 2, z * math.sqrt(k + z * z / 4)
    return max(0.0, (centre - half) / n), (centre + half) / n


@dataclass
class Rate:
    k: float
    n: float
    step: Step
    prior: float | None = None  # portfolio rate this one is shrunk toward

    @property
    def raw(self) -> float | None:
        return self.k / self.n if self.n else None

    @property
    def shrunk(self) -> float | None:
        if not self.n:
            return self.prior
        if self.prior is None:
            return self.raw
        m = self.step.prior_strength
        return (self.k + m * self.prior) / (self.n + m)

    @property
    def interval(self) -> tuple[float, float]:
        return (poisson_rate if self.step.poisson else wilson)(self.k, self.n)

    @property
    def low_volume(self) -> bool:
        return self.n < MIN_DENOMINATOR or self.k < MIN_NUMERATOR

    def fmt(self, value: float | None) -> str:
        if value is None:
            return "\u2013"
        scaled = value * self.step.per
        return f"{scaled:.1f}" + ("" if self.step.poisson else "%")


def _sums(apps, start: date, end: date) -> dict[int, dict[str, int]]:
    fields = {s for step in STEPS for s in (step.numerator, step.denominator)}
    rows = (
        ConversionMetric.objects.filter(app__in=apps, date__range=(start, end))
        .values("app_id")
        .annotate(**{f: Sum(f) for f in fields})
    )
    return {r["app_id"]: {f: r[f] or 0 for f in fields} for r in rows}


def last_day() -> date | None:
    """Latest day with App Store Analytics data for an active app."""
    return ConversionMetric.objects.filter(ACTIVE_ROWS, unique_impressions__gt=0).aggregate(
        d=Max("date")
    )["d"]


# --- 1. leak strip -----------------------------------------------------------------------------

CELL_W = 220


@dataclass
class Cell:
    step: Step
    rate: Rate
    reference: float | None
    reference_label: str
    verdict: str  # "below" | "above" | "usual" | "thin" | "none" (no purchases at all)
    view: dict = field(default_factory=dict)  # positions and labels, filled in by leak_strip


@dataclass
class LeakRow:
    app: App
    cells: dict[str, Cell]  # in STEPS order
    lost: float  # downloads/month lost vs the app's usual install rate (0 if none)
    worst: str | None  # step key to highlight


def leak_strip(end: date) -> dict:
    apps = list(App.objects.active())
    start = end - timedelta(days=WINDOW - 1)
    ref_end = start - timedelta(days=1)
    now, before = (
        _sums(apps, start, end),
        _sums(apps, ref_end - timedelta(days=REFERENCE - 1), ref_end),
    )
    portfolio = {
        s.key: (
            sum(v[s.numerator] for v in now.values())
            / max(sum(v[s.denominator] for v in now.values()), 1)
        )
        for s in STEPS
    }
    rows = []
    for app in apps:
        cur, ref = now.get(app.pk), before.get(app.pk, {})
        if not cur or not any(cur.values()):
            continue
        cells, worst, worst_gap = {}, None, 0.0
        for s in STEPS:
            rate = Rate(cur[s.numerator], cur[s.denominator], s, portfolio[s.key])
            ref_n = ref.get(s.denominator, 0)
            if ref_n >= MIN_DENOMINATOR:
                reference, ref_label = ref[s.numerator] / ref_n, "your previous 90 days"
            else:
                reference, ref_label = portfolio[s.key], "all your apps, same 28 days"
            lo, hi = rate.interval
            if s.poisson and rate.k == 0:
                verdict = "none"
            elif rate.low_volume:
                verdict = "thin"
            elif hi < reference:
                verdict = "below"
            elif lo > reference:
                verdict = "above"
            else:
                verdict = "usual"
            cells[s.key] = Cell(s, rate, reference, ref_label, verdict)
            if verdict == "below" and reference:
                gap = (reference - (rate.shrunk or 0)) / reference
                if gap > worst_gap:
                    worst, worst_gap = s.key, gap
        head = cells[HEADLINE]
        lost = 0.0
        if head.reference is not None and head.rate.shrunk is not None:
            per_month = head.rate.n * 30 / WINDOW
            lost = max(0.0, per_month * (head.reference - head.rate.shrunk))
        rows.append(LeakRow(app, cells, lost, worst))
    rows.sort(key=lambda r: (-r.lost, r.app.name))
    # One zoomed scale per column, shared by every app, so dots compare down a column. It is a
    # dot plot (position, not length), so the axis need not start at zero.
    for s in STEPS:
        values = [
            v
            for r in rows
            for c in [r.cells[s.key]]
            if c.rate.n and not c.rate.low_volume
            for v in (*c.rate.interval, c.reference or 0, c.rate.shrunk or 0)
        ]
        low, high = (min(values), max(values)) if values else (0.0, 1.0)
        pad = (high - low) * 0.15 or high * 0.1 or 0.01
        low, high = max(0.0, low - pad), high + pad

        def px(v, low=low, high=high):
            return round(8 + (CELL_W - 16) * (min(max(v, low), high) - low) / (high - low), 1)

        for r in rows:
            c = r.cells[s.key]
            lo, hi = c.rate.interval
            c.view = {
                "lo": px(lo),
                "hi": px(hi),
                "dot": px(c.rate.shrunk or 0),
                "ref": px(c.reference or 0),
                "value": c.rate.fmt(c.rate.shrunk),
                "usual": c.rate.fmt(c.reference),
                "count": f"{int(c.rate.k)} of {int(c.rate.n)}",
                "tip": (
                    f"{r.app.name}: {s.label}\n"
                    f"{c.rate.fmt(c.rate.raw)} raw ({int(c.rate.k)} of {int(c.rate.n)}), "
                    f"{c.rate.fmt(c.rate.shrunk)} after shrinkage\n"
                    f"90% interval {c.rate.fmt(lo)} to {c.rate.fmt(hi)}\n"
                    f"usual: {c.rate.fmt(c.reference)} ({c.reference_label})"
                ),
            }
    return {
        "rows": rows,
        "steps": STEPS,
        "start": start,
        "end": end,
        "cell_w": CELL_W,
    }


# --- 2. step rates over time, with releases ---------------------------------------------------

PANEL_W, PANEL_H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 120, 48, 12, 10, 18


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _weeks(end: date) -> list[tuple[date, date]]:
    last = _week_start(end)
    starts = [last - timedelta(weeks=WEEKS - 1 - i) for i in range(WEEKS)]
    return [(ws, ws + timedelta(days=6)) for ws in starts]


def _axis_top(peak: float) -> float:
    return nice_axis(peak, 4, 0.001)[0]


def _band(top: list[tuple[float, float]], bottom: list[tuple[float, float]]) -> str:
    """Closed SVG path between two polylines (bottom drawn back right-to-left)."""
    return (
        "M"
        + " L".join(f"{x:.1f},{y:.1f}" for x, y in top)
        + " L"
        + " L".join(f"{x:.1f},{y:.1f}" for x, y in reversed(bottom))
        + " Z"
    )


def step_trends(app: App, end: date) -> dict:
    weeks = _weeks(end)
    first = weeks[0][0]
    fields = [f for s in STEPS for f in (s.numerator, s.denominator)]
    # 28 extra days so release markers can compare the four weeks before the first week
    daily = {
        row[0]: dict(zip(fields, row[1:], strict=True))
        for row in ConversionMetric.objects.filter(
            app=app, date__range=(first - timedelta(days=28), end)
        ).values_list("date", *fields)
    }
    weekly = defaultdict(lambda: defaultdict(int))  # week index -> field -> sum
    for d, values in daily.items():
        if d >= first:
            for f, v in values.items():
                weekly[(d - first).days // 7][f] += v
    plot_w = PANEL_W - PAD_L - PAD_R
    slot = plot_w / len(weeks)

    def xc(i: int) -> float:
        return round(PAD_L + (i + 0.5) * slot, 1)

    panels = []
    for s in STEPS:
        points = []
        for i, (ws, we) in enumerate(weeks):
            n = weekly[i][s.denominator] if i in weekly else 0
            if n == 0:
                points.append(None)  # Apple published nothing: a gap, not a zero
                continue
            rate = Rate(weekly[i][s.numerator], n, s)
            points.append({"i": i, "rate": rate, "partial": we > end, "week": ws})
        present = [p for p in points if p]
        if s.poisson and not any(p["rate"].k for p in present):
            continue  # an app without in-app purchases has no purchase step to draw
        cap = 99 if s.poisson else 1.0
        top = _axis_top(max([min(p["rate"].interval[1], cap) for p in present] or [0.01]))
        base = PAD_T + PANEL_H

        def y(v, top=top, base=base):
            return round(base - PANEL_H * min(v, top) / top, 1)

        segments, current = [], []
        for p in points:
            if p is None:
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(p)
        if current:
            segments.append(current)
        lines, dashed, bands, dots = [], [], [], []
        for seg in segments:
            solid = [p for p in seg if not p["partial"]]
            if len(solid) > 1:
                lines.append(" ".join(f"{xc(p['i'])},{y(p['rate'].raw)}" for p in solid))
            if len(seg) > 1:
                bands.append(
                    _band(
                        [(xc(p["i"]), y(p["rate"].interval[1])) for p in seg],
                        [(xc(p["i"]), y(p["rate"].interval[0])) for p in seg],
                    )
                )
            for p in seg:
                lo, hi = p["rate"].interval
                dots.append(
                    {
                        "x": xc(p["i"]),
                        "y": y(p["rate"].raw),
                        "partial": p["partial"],
                        "thin": p["rate"].low_volume,
                        "tip": (
                            f"Week of {p['week']:%b} {p['week'].day}: "
                            f"{p['rate'].fmt(p['rate'].raw)}"
                            f" ({int(p['rate'].k)} of {int(p['rate'].n)})\n"
                            f"90% interval {p['rate'].fmt(lo)} to {p['rate'].fmt(hi)}"
                            + ("\nweek still in progress" if p["partial"] else "")
                        ),
                    }
                )
            if seg[-1]["partial"] and len(seg) > 1:
                a, b = seg[-2], seg[-1]
                dashed.append(f"{xc(a['i'])},{y(a['rate'].raw)} {xc(b['i'])},{y(b['rate'].raw)}")
        grid = [{"y": y(v), "label": Rate(0, 1, s).fmt(v)} for v in (0, top / 2, top)]
        panels.append(
            {
                "step": s,
                "lines": lines,
                "dashed": dashed,
                "bands": bands,
                "dots": dots,
                "grid": grid,
                "base": base,
            }
        )

    def window(start: date, stop: date) -> Rate | None:
        k = n = 0
        for d in days(start, stop):
            if d in daily:
                k += daily[d][HEADLINE_STEP.numerator]
                n += daily[d][HEADLINE_STEP.denominator]
        return Rate(k, n, HEADLINE_STEP) if n else None

    markers = []
    for v in AppVersion.objects.filter(
        app=app, platform=IOS, first_seen__range=(first, end)
    ).order_by("first_seen"):
        i = (_week_start(v.first_seen) - first).days // 7
        x = round(PAD_L + i * slot + slot * v.first_seen.weekday() / 7, 1)
        b = window(v.first_seen - timedelta(days=28), v.first_seen - timedelta(days=1))
        a = window(v.first_seen, min(v.first_seen + timedelta(days=27), end))
        label = f"v{v.version}"
        if a and b and not (a.low_volume or b.low_volume):
            label += f": {b.fmt(b.raw)} → {a.fmt(a.raw)}"
        markers.append({"x": x, "label": label})
    return {
        "panels": panels,
        "markers": markers,
        "months": month_ticks([ws for ws, _ in weeks], xc),
        "width": PANEL_W,
        "panel_h": PANEL_H + PAD_T + PAD_B,
        "pad_l": PAD_L,
        "plot_r": PANEL_W - PAD_R,
    }


# --- 3. source mix vs rate ---------------------------------------------------------------------

SOURCES = ["App Store search", "App Store browse", "Web referrer", "App referrer"]
SOURCE_CLASS = {name: f"c{i + 1}" for i, name in enumerate(SOURCES)} | {"Other": "other"}
MIX_W, MIX_H = 470, 150


def _source(name: str) -> str:
    return name if name in SOURCES else "Other"


def source_mix(app: App, end: date) -> dict:
    """Weekly share of unique impressions by source (left) next to each source's install rate
    (right): a blended rate can fall while every source improves if the mix shifts toward
    browse, which converts worse than search."""
    weeks = _weeks(end)
    imp = defaultdict(float)
    dl = defaultdict(float)
    for day, source, impressions, downloads in FunnelDay.objects.filter(
        app=app, date__range=(weeks[0][0], end)
    ).values_list("date", "source_type", "unique_impressions", "first_time_downloads"):
        key = ((_week_start(day) - weeks[0][0]).days // 7, _source(source))
        imp[key] += impressions
        dl[key] += downloads
    totals = defaultdict(float)
    for (_i, src), v in dl.items():
        totals[src] += v
    shown = [s for s in [*SOURCES, "Other"] if any(src == s for (_, src) in imp)]
    # sources with too few conversions to say anything fold into Other
    folded = {s for s in shown if s != "Other" and totals[s] < MIN_NUMERATOR}
    if folded:
        for store in (imp, dl):
            for i, src in list(store):
                if src in folded:
                    store[(i, "Other")] += store.pop((i, src))
        shown = [s for s in shown if s not in folded]
        if "Other" not in shown:
            shown.append("Other")
    slot = (MIX_W - 40) / len(weeks)

    def xc(i):
        return round(36 + (i + 0.5) * slot, 1)

    # left: 100% stacked area of impression share
    recent = range(len(weeks) - 4, len(weeks))
    recent_all = sum(imp.get((i, s), 0) for s in shown for i in recent)
    stacks, below = [], [0.0] * len(weeks)
    for src in shown:
        shares = []
        for i in range(len(weeks)):
            total = sum(imp.get((i, s), 0) for s in shown)
            shares.append(imp.get((i, src), 0) / total if total else 0)
        path = _band(
            [(xc(i), 8 + MIX_H * (1 - below[i] - shares[i])) for i in range(len(weeks))],
            [(xc(i), 8 + MIX_H * (1 - below[i])) for i in range(len(weeks))],
        )
        share = sum(imp.get((i, src), 0) for i in recent)
        stacks.append(
            {
                "src": src,
                "cls": SOURCE_CLASS[src],
                "path": path,
                "share": 100 * share / recent_all if recent_all else 0,
            }
        )
        below = [below[i] + shares[i] for i in range(len(weeks))]
    # right: install rate per source, weekly, with intervals
    step = HEADLINE_STEP
    rates = {}
    for src in shown:
        for i in range(len(weeks)):
            n = imp.get((i, src), 0)
            if n:
                rates[(src, i)] = Rate(dl.get((i, src), 0), n, step)
    top = _axis_top(max([min(r.interval[1], 1.0) for r in rates.values()] or [0.01]))

    def y(v):
        return round(8 + MIX_H * (1 - min(v, top) / top), 1)

    lines = []
    for src in shown:
        pts = [(i, rates[(src, i)]) for i in range(len(weeks)) if (src, i) in rates]
        window = [r for _, r in pts[-4:]]
        k, n = sum(r.k for r in window), sum(r.n for r in window)
        latest = Rate(k, n, step) if n else None
        lines.append(
            {
                "src": src,
                "cls": SOURCE_CLASS[src],
                "points": " ".join(f"{xc(i)},{y(r.raw)}" for i, r in pts if not r.low_volume),
                "thin": [{"x": xc(i), "y": y(r.raw)} for i, r in pts if r.low_volume],
                "latest": latest.fmt(latest.raw) if latest else "\u2013",
                "latest_thin": latest.low_volume if latest else True,
            }
        )
    grid = [{"y": y(v), "label": Rate(0, 1, step).fmt(v)} for v in (0, top / 2, top)]
    months = month_ticks([ws for ws, _ in weeks], xc, min_gap=38)
    return {
        "stacks": stacks,
        "lines": lines,
        "grid": grid,
        "months": months,
        "w": MIX_W,
        "h": MIX_H + 26,
        "folded": sorted(folded),
    }
