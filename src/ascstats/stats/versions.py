"""App versions and what each release did to the numbers.

A version is dated by the first day it appears in the sales report: new downloads and updates
carry the version the customer received, so this works from data the pipeline already has.
Impact compares the days a version was live with the days just before it, two ways:

- naive: mean per day after vs before, with a Welch t-score;
- vs trend (downloads): a log-linear trend with day-of-week effects is fitted to the
  ``BASELINE_DAYS`` before the release and projected over the days the version was live. The
  change is actual against that projection, so an app that was growing anyway does not
  credit its releases with the growth. The t-score uses the fit's residual noise plus the
  uncertainty of the projection itself. The first ``LAUNCH_DAYS`` of a platform's sales are
  never used as a baseline.

Neither is an experiment: a feature, a promotion or a previous release's bump inside the
baseline still lands in the numbers. Anything with |t| under ~2 is indistinguishable from
day-to-day noise.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
from django.db import transaction
from django.db.models import Max, Min, Q, Sum

from ascstats.asc.client import ASCClient, ReportNotReady

from .models import (
    DOWNLOAD_ROWS,
    IAP_ROWS,
    IOS,
    MAC_OS,
    UPDATE_ROWS,
    App,
    AppVersion,
    ConversionMetric,
    SalesRow,
    platform_rows,
)

log = logging.getLogger(__name__)

SIGNIFICANT_T = 2.0
BASELINE_DAYS = 28  # history the trend is fitted on
MIN_BASELINE_DAYS = 14  # two of each weekday; with less, no trend-adjusted estimate
LAUNCH_DAYS = 21  # a launch ramp is not a trend: projecting it forward invents huge drops


def build_versions(app: App) -> int:
    """Recompute AppVersion.first_seen per platform from the loaded sales rows. Idempotent:
    dates are cleared first, so a version no longer found in sales loses its date."""
    written = 0
    with transaction.atomic():
        AppVersion.objects.filter(app=app).update(first_seen=None)
        for platform in (IOS, MAC_OS):
            first_seen = (
                SalesRow.objects.filter(
                    (DOWNLOAD_ROWS | UPDATE_ROWS) & platform_rows(platform), app=app
                )
                .exclude(version="")
                .values("version")
                .annotate(first=Min("report_date"))
            )
            for row in first_seen:
                AppVersion.objects.update_or_create(
                    app=app,
                    platform=platform,
                    version=row["version"],
                    defaults={"first_seen": row["first"]},
                )
                written += 1
    return written


def sync_asc_versions(client: ASCClient, app: App) -> int:
    """Add App Store Connect's state and creation time (read-only GET). Apps that still show
    up in sales but are no longer visible to the API (removed or transferred) 404: their
    versions stay dated from sales alone."""
    try:
        resources = client.app_store_versions(app.apple_id)
    except ReportNotReady:
        log.warning("app %s is not visible to this API key; versions from sales only", app)
        return 0
    written = 0
    for resource in resources:
        attrs = resource["attributes"]
        created = attrs.get("createdDate")
        AppVersion.objects.update_or_create(
            app=app,
            platform=attrs.get("platform") or IOS,
            version=attrs["versionString"],
            defaults={
                "asc_state": attrs.get("appVersionState") or attrs.get("appStoreState") or "",
                "asc_created": datetime.fromisoformat(created) if created else None,
            },
        )
        written += 1
    return written


@dataclass
class Delta:
    metric: str
    before: float | None
    after: float | None
    change_pct: float | None
    t_score: float | None = None

    @property
    def significant(self) -> bool:
        return self.t_score is not None and abs(self.t_score) >= SIGNIFICANT_T


@dataclass
class VersionImpact:
    platform: str
    version: str
    released_on: date
    before_days: int
    after_days: int
    updates: int  # update units while the version was live in the window
    deltas: list[Delta] = field(default_factory=list)


def _welch_t(before: list[float], after: list[float]) -> float | None:
    if len(before) < 2 or len(after) < 2:
        return None
    se = math.sqrt(
        statistics.variance(before) / len(before) + statistics.variance(after) / len(after)
    )
    if se == 0:
        return None
    return (statistics.fmean(after) - statistics.fmean(before)) / se


def _pct(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before == 0:
        return None
    return round(100 * (after - before) / before, 1)


def _daily(app: App, q: Q, start: date, end: date) -> dict[date, int]:
    return dict(
        SalesRow.objects.filter(q, app=app, report_date__range=(start, end))
        .values_list("report_date")
        .annotate(Sum("units"))
    )


def _series(values: dict[date, int], start: date, end: date) -> list[float]:
    days = (end - start).days + 1
    return [float(values.get(start + timedelta(days=i), 0)) for i in range(max(days, 0))]


def _mean_delta(metric: str, before: list[float], after: list[float]) -> Delta:
    b = statistics.fmean(before) if before else None
    a = statistics.fmean(after) if after else None
    return Delta(
        metric,
        None if b is None else round(b, 2),
        None if a is None else round(a, 2),
        _pct(b, a),
        None if (t := _welch_t(before, after)) is None else round(t, 2),
    )


def _design(days: list[date], origin: date) -> np.ndarray:
    """Intercept, days since ``origin`` and Monday..Saturday dummies (Sunday is the base)."""
    x = np.zeros((len(days), 8))
    for i, day in enumerate(days):
        x[i, 0] = 1.0
        x[i, 1] = (day - origin).days
        if day.weekday() < 6:
            x[i, 2 + day.weekday()] = 1.0
    return x


def _trend_delta(
    metric: str, values: dict[date, int], baseline: tuple[date, date], live: tuple[date, date]
) -> Delta:
    """Actual per-day mean while live against the baseline trend projected over those days."""
    b_days = [baseline[0] + timedelta(days=i) for i in range((baseline[1] - baseline[0]).days + 1)]
    a_days = [live[0] + timedelta(days=i) for i in range((live[1] - live[0]).days + 1)]
    if len(b_days) < MIN_BASELINE_DAYS or not a_days:
        return Delta(metric, None, None, None)
    xb, xa = _design(b_days, live[0]), _design(a_days, live[0])
    yb = np.log1p([values.get(d, 0) for d in b_days])
    actual = np.array([values.get(d, 0) for d in a_days], dtype=float)
    beta, *_ = np.linalg.lstsq(xb, yb, rcond=None)
    predicted = xa @ beta
    expected = float(np.expm1(predicted).clip(min=0).mean())
    observed = float(actual.mean())
    residuals = yb - xb @ beta
    s2 = float(residuals @ residuals) / (len(b_days) - xb.shape[1])
    xbar = xa.mean(axis=0)
    se = math.sqrt(s2 / len(a_days) + s2 * float(xbar @ np.linalg.pinv(xb.T @ xb) @ xbar))
    t = float((np.log1p(actual) - predicted).mean()) / se if se > 0 else None
    return Delta(
        metric,
        round(expected, 2),
        round(observed, 2),
        _pct(expected, observed),
        None if t is None else round(t, 2),
    )


def _conversion(app: App, start: date, end: date) -> float | None:
    agg = ConversionMetric.objects.filter(app=app, date__range=(start, end)).aggregate(
        pv=Sum("page_views"), ft=Sum("first_time_downloads")
    )
    return agg["ft"] / agg["pv"] if agg["pv"] else None


def version_impact(app: App, window: int = 14, platform: str | None = None) -> list[VersionImpact]:
    """For every dated version: up to ``window`` days live vs up to ``window`` days before,
    counting only that platform's rows. Windows stop at the neighbouring releases of the same
    platform and at the edges of the loaded data, so each side only ever contains one version.
    Conversion comes from App Store analytics, which is iOS-only here."""
    span = SalesRow.objects.filter(app=app).aggregate(
        first=Min("report_date"), last=Max("report_date")
    )
    if span["first"] is None:
        return []
    data_start, data_end = span["first"], span["last"]
    # An app whose first sale is the first day loaded for any app may be older than the data.
    dataset_start = SalesRow.objects.aggregate(first=Min("report_date"))["first"]
    out = []
    for plat in [platform] if platform else [IOS, MAC_OS]:
        versions = list(
            AppVersion.objects.filter(app=app, platform=plat, first_seen__isnull=False).order_by(
                "first_seen"
            )
        )
        if not versions:
            continue
        rows = platform_rows(plat)
        downloads = _daily(app, DOWNLOAD_ROWS & rows, data_start, data_end)
        launched = min(downloads, default=None)
        iap = _daily(app, IAP_ROWS & rows, data_start, data_end)
        updates = _daily(app, UPDATE_ROWS & rows, data_start, data_end)
        for i, v in enumerate(versions):
            released = v.first_seen
            prev_release = versions[i - 1].first_seen if i else data_start
            next_release = versions[i + 1].first_seen if i + 1 < len(versions) else None
            b_start = max(released - timedelta(days=window), prev_release, data_start)
            b_end = released - timedelta(days=1)
            a_end = min(released + timedelta(days=window - 1), data_end)
            if next_release:
                a_end = min(a_end, next_release - timedelta(days=1))
            if b_end < b_start:  # nothing loaded before this version: no baseline
                b_end = b_start - timedelta(days=1)
            impact = VersionImpact(
                platform=plat,
                version=v.version,
                released_on=released,
                before_days=(b_end - b_start).days + 1,
                after_days=(a_end - released).days + 1,
                updates=int(sum(_series(updates, released, a_end))),
            )
            before = _series(downloads, b_start, b_end)
            after = _series(downloads, released, a_end)
            impact.deltas.append(_mean_delta("downloads/day", before, after))
            trend_start = max(released - timedelta(days=BASELINE_DAYS), data_start)
            if launched and launched > dataset_start:  # launched inside the loaded history
                trend_start = max(trend_start, launched + timedelta(days=LAUNCH_DAYS))
            impact.deltas.append(
                _trend_delta(
                    "downloads/day vs trend",
                    downloads,
                    (trend_start, released - timedelta(days=1)),
                    (released, a_end),
                )
            )
            impact.deltas.append(
                _mean_delta(
                    "in-app purchases/day",
                    _series(iap, b_start, b_end),
                    _series(iap, released, a_end),
                )
            )
            conv_b = conv_a = None
            if plat == IOS:
                conv_b = _conversion(app, b_start, b_end) if impact.before_days else None
                conv_a = _conversion(app, released, a_end)
            impact.deltas.append(
                Delta(
                    "conversion rate",
                    None if conv_b is None else round(conv_b, 4),
                    None if conv_a is None else round(conv_a, 4),
                    _pct(conv_b, conv_a),
                )
            )
            out.append(impact)
    return out
