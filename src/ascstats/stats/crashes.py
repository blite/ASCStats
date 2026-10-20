"""Crash rate per app version, from Apple's App Crashes and App Sessions Standard reports.

Both reports carry an App Version column, so no date windows are needed: crashes and sessions
are summed per version directly, and the rate is crashes per 1,000 sessions (Apple's own
crash-rate definition). Both only include users who share analytics with developers, and
Apple omits days with fewer than five such users, so small apps have gaps; the rate is only
shown once a version has enough sessions to mean something.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Sum
from django.db.models.fields.json import KeyTextTransform

from .models import AnalyticsRow, App
from .parsing import CRASHES_REPORT, SESSIONS_REPORT

MAC_DEVICES = {"Desktop", "Mac"}
MIN_SESSIONS = 200  # below this a crash rate is mostly noise


@dataclass
class VersionCrashes:
    version: str
    crashes: int
    unique_devices: int
    sessions: int

    @property
    def per_1k_sessions(self) -> float | None:
        if self.sessions < MIN_SESSIONS:
            return None
        return round(1000 * self.crashes / self.sessions, 2)


def _by_version(app: App, report: str) -> dict[str, tuple[int, int]]:
    rows = (
        AnalyticsRow.objects.filter(app=app, report_name=report)
        .exclude(device__in=MAC_DEVICES)
        .annotate(version=KeyTextTransform("App Version", "dimensions"))
        .values("version")
        .annotate(n=Sum("counts"), devices=Sum("unique_counts"))
    )
    return {r["version"]: (r["n"] or 0, r["devices"] or 0) for r in rows if r["version"]}


def version_crashes(app: App) -> dict[str, VersionCrashes]:
    """iOS crashes, affected devices and sessions per version string."""
    crashes, sessions = _by_version(app, CRASHES_REPORT), _by_version(app, SESSIONS_REPORT)
    out = {}
    for v in crashes.keys() | sessions.keys():
        n, devices = crashes.get(v, (0, 0))
        out[v] = VersionCrashes(v, n, devices, sessions.get(v, (0, 0))[0])
    return out
