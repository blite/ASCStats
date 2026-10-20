"""Every table the pipeline touches is defined here and created by Django migrations."""

from django.db import models
from django.db.models import Q

# Product Type Identifiers. Updates carry the new version in the Version column (useful for
# dating releases); neither updates nor redownloads count as downloads (Apple's "units").
UPDATE_PRODUCT_TYPES = ["7", "7F", "7T", "F7"]
REDOWNLOAD_PRODUCT_TYPES = ["3", "3F", "3T", "F3"]
# Mac and iOS share one app record but ship their own versions. The Device column says which
# platform a row belongs to ("Desktop" is a Mac); product types do not: real reports carry
# F7 updates and F3 redownloads for iPhone and iPad too.
IOS, MAC_OS = "IOS", "MAC_OS"  # App Store Connect's platform names
MAC_DEVICES = {"Desktop", "Mac"}  # Sales says Desktop; analytics reports may say Mac
MAC_ROWS = Q(device__in=MAC_DEVICES)


class Report(models.TextChoices):
    SALES = "sales", "Sales & Trends (daily summary)"
    ANALYTICS = "analytics", "Analytics Reports (daily)"


class AppQuerySet(models.QuerySet):
    def active(self):
        """The apps every stat covers."""
        return self.filter(is_active=True)


class App(models.Model):
    apple_id = models.BigIntegerField(unique=True, help_text="App Apple Identifier")
    name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100, blank=True)
    bundle_id = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True, help_text="Include in daily imports")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.apple_id})"


class SalesRow(models.Model):
    """One line of the Sales & Trends SALES/SUMMARY/DAILY report, attributed to an app."""

    app = models.ForeignKey(
        App, on_delete=models.CASCADE, related_name="sales_rows", db_index=False
    )
    report_date = models.DateField(db_index=True)
    sku = models.CharField(max_length=100)
    title = models.CharField(max_length=255)
    version = models.CharField(max_length=50, blank=True)
    product_type = models.CharField(max_length=10, help_text="Product Type Identifier, e.g. 1F")
    units = models.IntegerField(help_text="Negative for refunds")
    developer_proceeds = models.DecimalField(max_digits=14, decimal_places=4)
    currency_of_proceeds = models.CharField(max_length=3)
    customer_price = models.DecimalField(max_digits=14, decimal_places=4)
    customer_currency = models.CharField(max_length=3)
    country_code = models.CharField(max_length=2)
    device = models.CharField(max_length=50, blank=True)
    parent_identifier = models.CharField(max_length=100, blank=True)

    class Meta:
        indexes = [models.Index(fields=["app", "report_date"])]

    def __str__(self) -> str:
        return f"{self.report_date} {self.sku} {self.country_code} x{self.units}"


# Sales rows that count as downloads: the app itself, not its in-app purchases, updates or
# redownloads.
DOWNLOAD_ROWS = Q(parent_identifier="") & ~Q(
    product_type__in=UPDATE_PRODUCT_TYPES + REDOWNLOAD_PRODUCT_TYPES
)
UPDATE_ROWS = Q(parent_identifier="", product_type__in=UPDATE_PRODUCT_TYPES)
IAP_ROWS = ~Q(parent_identifier="")
# Stats only ever cover active apps. Sales files are vendor-wide, so inactive apps' rows are
# still loaded (raw fidelity, quality checks) but never counted.
ACTIVE_ROWS = Q(app__is_active=True)


def platform_rows(platform: str) -> Q:
    return MAC_ROWS if platform == MAC_OS else ~MAC_ROWS


def platform_of(device: str) -> str:
    return MAC_OS if device in MAC_DEVICES else IOS


def sales_kind(product_type: str, parent_identifier: str) -> str:
    """What a sales line counts as: the Python twin of DOWNLOAD_ROWS/UPDATE_ROWS/IAP_ROWS."""
    if parent_identifier:
        return "iap_units"
    if product_type in UPDATE_PRODUCT_TYPES:
        return "updates"
    if product_type in REDOWNLOAD_PRODUCT_TYPES:
        return "redownloads"
    return "downloads"


class SalesDay(models.Model):
    """Daily rollup of the sales report per app and platform, emitted at load time, so stats
    never re-add SalesRow lines. SalesRow keeps the detail (product, country, currency)."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, db_index=False)
    date = models.DateField(db_index=True)
    platform = models.CharField(max_length=10)
    downloads = models.IntegerField(default=0, help_text="net of refunds")
    redownloads = models.IntegerField(default=0)
    updates = models.IntegerField(default=0)
    iap_units = models.IntegerField(default=0, help_text="net of refunds")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app", "date", "platform"], name="uniq_sales_day")
        ]

    def __str__(self) -> str:
        return f"{self.app_id} {self.date} {self.platform}"


class FunnelDay(models.Model):
    """App Store discovery and download totals per app, day and source, reduced at load time
    from the Discovery and Engagement and App Downloads reports. Raw report rows stay in the
    raw store; a new question means a new emitter and a replay, not a wider table."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, db_index=False)
    date = models.DateField()
    source_type = models.CharField(max_length=60, blank=True)
    impressions = models.BigIntegerField(default=0)
    unique_impressions = models.BigIntegerField(default=0)
    page_views = models.BigIntegerField(default=0)
    unique_page_views = models.BigIntegerField(default=0)
    first_time_downloads = models.BigIntegerField(default=0)
    redownloads = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app", "date", "source_type"], name="uniq_funnel_day")
        ]

    def __str__(self) -> str:
        return f"{self.app_id} {self.date} {self.source_type}"


class UsageDay(models.Model):
    """Sessions and crashes per app, day, platform and app version, reduced at load time from
    the App Sessions and App Crashes reports (users who share analytics only)."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, db_index=False)
    date = models.DateField()
    platform = models.CharField(max_length=10)
    version = models.CharField(max_length=50, blank=True)
    sessions = models.BigIntegerField(default=0)
    session_seconds = models.BigIntegerField(default=0)
    session_devices = models.BigIntegerField(default=0, help_text="summed per segment")
    crashes = models.BigIntegerField(default=0)
    crash_devices = models.BigIntegerField(default=0, help_text="summed per segment")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["app", "date", "platform", "version"], name="uniq_usage_day"
            )
        ]

    def __str__(self) -> str:
        return f"{self.app_id} {self.date} {self.platform} {self.version}"


class ConversionMetric(models.Model):
    """Derived daily funnel per app: impressions -> product-page views -> first-time downloads
    (App Store Analytics) -> in-app purchases (Sales).

    Rates are not stored: they are ratios of sums, so they must be computed over a window
    (sum numerators and denominators, then divide), never averaged from daily ratios. Many
    downloads come straight from search results without a page view, so the headline rate is
    downloads / unique impressions; downloads / page views can exceed 100%.
    """

    app = models.ForeignKey(
        App, on_delete=models.CASCADE, related_name="conversion_metrics", db_index=False
    )
    date = models.DateField()
    impressions = models.BigIntegerField(default=0, help_text="total, repeats included")
    unique_impressions = models.BigIntegerField(default=0, help_text="unique devices")
    page_views = models.BigIntegerField(default=0, help_text="total, repeats included")
    unique_page_views = models.BigIntegerField(default=0, help_text="unique devices")
    first_time_downloads = models.BigIntegerField(default=0, help_text="App Store Analytics")
    units = models.BigIntegerField(default=0, help_text="downloads in the sales report")
    iap_units = models.BigIntegerField(default=0, help_text="in-app purchases, net of refunds")
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["app", "date"], name="uniq_conversion_day")]
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"{self.app_id} {self.date}"

    @property
    def install_rate(self) -> float | None:
        """First-time downloads / unique impressions: the funnel's headline rate."""
        if not self.unique_impressions:
            return None
        return self.first_time_downloads / self.unique_impressions

    @property
    def tap_through_rate(self) -> float | None:
        if not self.unique_impressions:
            return None
        return self.unique_page_views / self.unique_impressions

    @property
    def page_view_conversion(self) -> float | None:
        """Downloads / unique page views. Can exceed 1: search downloads skip the page."""
        if not self.unique_page_views:
            return None
        return self.first_time_downloads / self.unique_page_views


class AppVersion(models.Model):
    """A shipped version of one platform, dated by the first day it shows up in the sales
    report (downloads and updates carry the version the customer received). App Store
    Connect's own version list adds state and creation time when running on real data."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="versions", db_index=False)
    version = models.CharField(max_length=50)
    first_seen = models.DateField(null=True, blank=True, help_text="First day in sales data")
    platform = models.CharField(max_length=20, default=IOS, help_text="IOS or MAC_OS")
    asc_state = models.CharField(max_length=40, blank=True)
    asc_created = models.DateTimeField(null=True, blank=True)
    asc_id = models.CharField(max_length=64, blank=True, help_text="appStoreVersions resource id")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app", "platform", "version"], name="uniq_version")
        ]
        ordering = ["app", "platform", "first_seen"]

    def __str__(self) -> str:
        return f"{self.app_id} {self.platform} {self.version}"


class RatingSnapshot(models.Model):
    """The App Store's public rating totals for one app in one storefront on one day.

    Apple only publishes the current totals (iTunes Lookup), not history, so history is built
    by snapshotting daily. The average comes unrounded, so ``star_sum`` is exact and two
    consecutive snapshots give the exact count and average of the ratings in between.
    """

    app = models.ForeignKey(
        App, on_delete=models.CASCADE, related_name="rating_snapshots", db_index=False
    )
    date = models.DateField()
    storefront = models.CharField(max_length=2, help_text="lowercase ISO country code")
    count = models.IntegerField()
    star_sum = models.IntegerField(help_text="count x average, rounded: total stars given")
    version = models.CharField(max_length=50, blank=True, help_text="version on sale that day")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app", "date", "storefront"], name="uniq_rating_day")
        ]

    def __str__(self) -> str:
        return f"{self.app_id} {self.date} {self.storefront} {self.count}"

    @property
    def average(self) -> float | None:
        return self.star_sum / self.count if self.count else None


class CustomerReview(models.Model):
    """A written review, as App Store Connect files it: under the version it was written for."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="reviews", db_index=False)
    asc_id = models.CharField(max_length=64)
    rating = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=500, blank=True)
    body = models.TextField(blank=True)
    territory = models.CharField(max_length=3, help_text="ISO alpha-3, as Apple sends it")
    created = models.DateTimeField()
    version = models.CharField(max_length=50, blank=True)
    platform = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["-created"]
        indexes = [models.Index(fields=["app", "created"])]
        # a constraint, not unique=True: that would add a LIKE-pattern index nothing uses
        constraints = [models.UniqueConstraint(fields=["asc_id"], name="uniq_review_asc_id")]

    def __str__(self) -> str:
        return f"{self.app_id} {self.rating}* {self.created:%Y-%m-%d}"


class ReviewSummary(models.Model):
    """Apple's AI-generated summary of an app's reviews in one territory (API 4.0+)."""

    app = models.ForeignKey(
        App, on_delete=models.CASCADE, related_name="review_summaries", db_index=False
    )
    platform = models.CharField(max_length=20)
    territory = models.CharField(max_length=3, blank=True)
    locale = models.CharField(max_length=20, blank=True)
    created = models.DateTimeField(null=True, blank=True)
    text = models.TextField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["app", "platform", "territory"], name="uniq_review_summary"
            )
        ]

    def __str__(self) -> str:
        return f"{self.app_id} {self.platform} {self.territory}"


class FxDay(models.Model):
    """One day's reference rates (European Central Bank, via Frankfurter): ``rates`` maps a
    currency code to USD per one unit of it, exactly as fetched, plus USD itself. Only used to
    put proceeds from many storefronts on one scale; Apple's payouts use its own rates, so
    converted revenue is an estimate and float precision is plenty."""

    date = models.DateField(unique=True)
    rates = models.JSONField(default=dict, help_text="{currency: USD per unit}")

    class Meta:
        ordering = ["date"]

    def __str__(self) -> str:
        return f"{self.date} ({len(self.rates)} currencies)"


class IngestionRun(models.Model):
    """Run log: one row per (report, date[, app], step) attempt."""

    class Status(models.TextChoices):
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        NOT_READY = "not_ready"
        FAILED = "failed"

    report = models.CharField(max_length=20, choices=Report.choices)
    report_date = models.DateField()
    app = models.ForeignKey(App, null=True, blank=True, on_delete=models.SET_NULL)
    step = models.CharField(max_length=20)
    source = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    raw_key = models.CharField(max_length=500, blank=True)
    rows_parsed = models.IntegerField(null=True, blank=True)
    rows_deleted = models.IntegerField(null=True, blank=True)
    rows_loaded = models.IntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["report", "report_date"])]

    def __str__(self) -> str:
        return f"{self.report} {self.report_date} {self.step} {self.status}"
