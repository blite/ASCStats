"""UTC days from the analytics reports: proceeds as App Store Connect's Trends shows them,
and each date rebuilt from the newest copy of each report that holds it."""

import gzip
from datetime import date, timedelta

import pytest

from ascstats.stats.models import App, FunnelDay, PurchaseDay, UsageDay, UtcDay
from ascstats.stats.parsing import (
    ANALYTICS_REPORTS,
    DOWNLOADS_REPORT,
    PURCHASES_REPORT,
    SESSIONS_REPORT,
)
from ascstats.stats.pipeline import NO_DATA, Pipeline, analytics_prefix, snapshot_prefix
from ascstats.stats.storage import raw_store

APP = 1000000001
NOV2, NOV3, NOV4 = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)

PURCHASES = [
    "Date", "App Name", "App Apple Identifier", "Purchase Type", "Content Name",
    "Content Apple Identifier", "Payment Method", "Device", "Platform Version", "Source Type",
    "Page Type", "App Download Date", "Pre-Order", "Territory", "Purchases", "Proceeds in USD",
    "Sales in USD", "Paying Users",
]  # fmt: skip
DOWNLOADS = [
    "Date", "App Name", "App Apple Identifier", "Download Type", "App Version", "Device",
    "Platform Version", "Source Type", "Source Info", "Campaign", "Page Type", "Page Title",
    "Pre-Order", "Territory", "Counts",
]  # fmt: skip
SESSIONS = [
    "Date", "App Name", "App Apple Identifier", "App Version", "Device", "Platform Version",
    "Source Type", "Source Info", "Campaign", "Page Type", "Page Title", "App Download Date",
    "Territory", "Sessions", "Total Session Duration", "Unique Devices",
]  # fmt: skip


def tsv(header, rows) -> bytes:
    lines = ["\t".join(header)] + ["\t".join(map(str, r)) for r in rows]
    return gzip.compress(("\n".join(lines) + "\n").encode())


def buy(day, content, territory, n, proceeds, sales, kind="In-app purchase"):
    return [day, "Spider", APP, kind, content, 1, "Paid", "iPhone", "iOS 26", "App Store search",
            "Product page", "", "No", territory, n, proceeds, sales, max(n, 0)]  # fmt: skip


def dl(day, kind, n, device="iPhone"):
    return [day, "Spider", APP, kind, "1.0", device, "iOS 26", "App Store search", "", "",
            "Product page", "", "No", "US", n]  # fmt: skip


def session(day, n):
    return [day, "Spider", APP, "1.0", "iPhone", "iOS 26", "App Store search", "", "", "", "",
            "", "US", n, n * 60, n]  # fmt: skip


def land(processing: date, report: str, header, rows, prefix=analytics_prefix):
    raw_store().put(prefix(processing, APP, report) + "segment-000.tsv.gz", tsv(header, rows))


@pytest.fixture
def spider(db, settings):
    settings.ASC_SOURCE = "asc"  # the load and quality steps only read the raw store
    return App.objects.create(apple_id=APP, name="Spider", sku="SPIDER")


def load(day):
    store = raw_store()
    for name in ANALYTICS_REPORTS:  # what the download step lands for a report Apple skipped
        if not store.list(analytics_prefix(day, APP, name)):
            store.put(analytics_prefix(day, APP, name) + NO_DATA, b"")
    pipe = Pipeline(source="asc", client_factory=lambda: None)
    for step in ("load", "quality"):
        getattr(pipe, step)("analytics", day)


def test_proceeds_match_app_store_connect_to_the_cent(spider):
    # Nov 3 as Apple's Trends showed it: five purchases, $14.68 proceeds, $20.94 sales,
    # Canada converted to USD by Apple
    land(NOV4, PURCHASES_REPORT, PURCHASES, [
        buy("2026-11-03", "Unlimited Play", "US", 1, "3.50", "4.99"),
        buy("2026-11-03", "Unlimited Play", "CA", 1, "3.48", "4.98"),
        buy("2026-11-03", "Hearts No Ads", "US", 2, "5.60", "7.98"),
        buy("2026-11-03", "Spades VIP", "US", 1, "2.10", "2.99"),
        buy("2026-11-03", "Spades VIP", "US", -1, "-2.10", "-2.99"),  # a refund
        buy("2026-11-03", "Spades VIP", "US", 1, "2.10", "2.99"),
    ])  # fmt: skip
    load(NOV4)
    (day,) = UtcDay.objects.filter(date=NOV3)
    assert (day.iap_units, day.proceeds_cents, day.sales_cents) == (5, 1468, 2094)
    canada = PurchaseDay.objects.get(territory="CA")
    assert (canada.content, canada.purchases, canada.proceeds_cents) == ("Unlimited Play", 1, 348)
    spades = PurchaseDay.objects.get(content="Spades VIP")
    assert (spades.purchases, spades.proceeds_cents) == (1, 210)  # the refund nets out

    from ascstats.stats.charts import Period
    from ascstats.stats.dashboard import tiles

    proceeds = tiles(NOV3, Period(NOV3, 1))[2]
    assert (proceeds.label, round(proceeds.value, 2)) == ("Proceeds", 14.68)


def test_a_later_report_reaching_back_keeps_earlier_downloads(spider):
    # The bug this replaces: Nov 4's Sessions report reaches back to Nov 2, and loading it
    # used to delete Nov 2's downloads (which only Nov 3's downloads report holds)
    land(NOV3, DOWNLOADS_REPORT, DOWNLOADS, [
        dl("2026-11-02", "First-time download", 50),
        dl("2026-11-02", "Redownload", 4),
        dl("2026-11-02", "Restore", 1),
        dl("2026-11-02", "Auto-update", 30),
        dl("2026-11-02", "Manual update", 2, "Desktop"),
    ])  # fmt: skip
    land(NOV3, SESSIONS_REPORT, SESSIONS, [session("2026-11-02", 10)])
    land(NOV4, DOWNLOADS_REPORT, DOWNLOADS, [dl("2026-11-03", "First-time download", 60)])
    land(NOV4, SESSIONS_REPORT, SESSIONS, [session("2026-11-02", 11), session("2026-11-03", 9)])
    load(NOV3)
    load(NOV4)
    nov2 = {d.platform: d for d in UtcDay.objects.filter(date=NOV2)}
    assert (nov2["IOS"].downloads, nov2["IOS"].redownloads, nov2["IOS"].updates) == (50, 5, 30)
    assert nov2["MAC_OS"].updates == 2
    assert FunnelDay.objects.get(date=NOV2).first_time_downloads == 50
    # Nov 2's sessions come from the newest copy (Nov 4's), never the two added together
    assert UsageDay.objects.get(date=NOV2).sessions == 11

    # loading an older day again afterwards changes nothing: the newest copy still wins
    load(NOV3)
    assert UsageDay.objects.get(date=NOV2).sessions == 11
    assert UtcDay.objects.get(date=NOV3, platform="IOS").downloads == 60


def test_rebuild_reads_every_copy_and_snapshots(spider):
    land(
        NOV3,
        PURCHASES_REPORT,
        PURCHASES,
        [buy("2026-11-02", "Spades VIP", "US", 1, "2.10", "2.99")],
    )
    # a snapshot holds history the daily reports no longer do; for a date both hold, the
    # daily copy of a newer day wins
    old = date(2025, 3, 1)
    land(NOV4, PURCHASES_REPORT, PURCHASES, [
        buy(old.isoformat(), "Spades VIP", "US", 3, "6.30", "8.97"),
        buy("2026-11-02", "Spades VIP", "US", 9, "99.00", "99.00"),
    ], prefix=snapshot_prefix)  # fmt: skip
    land(NOV4 + timedelta(days=1), PURCHASES_REPORT, PURCHASES, [
        buy("2026-11-02", "Spades VIP", "US", 1, "2.10", "2.99"),
    ])  # fmt: skip
    pipe = Pipeline(source="asc", client_factory=lambda: None)
    assert pipe.rebuild(spider) == 4  # a UtcDay and a PurchaseDay for each of two days
    assert UtcDay.objects.get(date=old).proceeds_cents == 630
    assert UtcDay.objects.get(date=NOV2).proceeds_cents == 210


def test_the_newest_day_waits_for_its_downloads(spider):
    from ascstats.stats.dashboard import last_day

    land(NOV4, DOWNLOADS_REPORT, DOWNLOADS, [dl("2026-11-03", "First-time download", 60)])
    land(NOV4, PURCHASES_REPORT, PURCHASES, [
        buy("2026-11-03", "Spades VIP", "US", 1, "2.10", "2.99"),
        buy("2026-11-04", "Spades VIP", "US", 1, "2.10", "2.99"),  # no downloads for it yet
    ])  # fmt: skip
    load(NOV4)
    assert last_day() == NOV3
