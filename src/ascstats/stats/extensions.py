"""Hooks other apps fill in from their ``AppConfig.ready()``, so ``stats`` never imports them.

``aso`` (competitors, keywords) uses them all: its competitors ride in the ratings snapshot's
Lookup requests, its keyword searches follow it in the daily store job, its tables refresh
cached pages, the synthetic seed creates its demo data, and the static export renders its
pages.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Protocol


class SnapshotRider(Protocol):
    """Other apps asked in the same Lookup requests as yours (``ratings.snapshot``)."""

    def ids(self) -> list[int]: ...

    def save(self, day: date, storefront: str, answers: dict[int, dict]) -> int:
        """Store one storefront's answers (apple_id -> Lookup result); rows written."""

    def finish(self, day: date) -> None:
        """After every storefront: e.g. copy the US listing onto each app."""

    def synthetic(self, day: date) -> int:
        """Write ``day``'s synthetic snapshot instead of asking Apple; rows written."""


snapshot_riders: list[Callable[[], SnapshotRider]] = []
data_models: list = []  # models whose rows count toward the cached pages' data version
synthetic_setup: list[Callable[[], None]] = []  # seed_synthetic, once before the days
synthetic_day: list[Callable[[date], None]] = []  # seed_synthetic, for every day seeded
export_paths: list[Callable[[object], list[str]]] = []  # export_site, per app: more pages
daily_store_steps: list[Callable[[], None]] = []  # jobs.store_daily, after the ratings
