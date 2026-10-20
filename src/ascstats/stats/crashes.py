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

from .models import IOS, App, UsageDay

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


def version_crashes(app: App) -> dict[str, VersionCrashes]:
    """iOS crashes, affected devices and sessions per version string."""
    rows = (
        UsageDay.objects.filter(app=app, platform=IOS)
        .exclude(version="")
        .values("version")
        .annotate(c=Sum("crashes"), d=Sum("crash_devices"), s=Sum("sessions"))
    )
    return {r["version"]: VersionCrashes(r["version"], r["c"], r["d"], r["s"]) for r in rows}
