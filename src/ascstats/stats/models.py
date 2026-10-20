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
MAC_DEVICE = "Desktop"
MAC_ROWS = Q(device=MAC_DEVICE)


class Report(models.TextChoices):
    SALES = "sales", "Sales & Trends (daily summary)"
    ANALYTICS = "analytics", "Analytics Reports (daily)"


class App(models.Model):
    apple_id = models.BigIntegerField(unique=True, help_text="App Apple Identifier")
    name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100, blank=True)
    bundle_id = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True, help_text="Include in daily imports")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.apple_id})"


class SalesRow(models.Model):
    """One line of the Sales & Trends SALES/SUMMARY/DAILY report, attributed to an app."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="sales_rows")
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


def platform_rows(platform: str) -> Q:
    return MAC_ROWS if platform == MAC_OS else ~MAC_ROWS


class AnalyticsRow(models.Model):
    """One line of an Analytics Reports segment.

    Analytics reports have different columns per report; the ones every dashboard needs are
    real columns and the rest stay queryable in ``dimensions``.
    """

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="analytics_rows")
    report_date = models.DateField(db_index=True)
    report_name = models.CharField(max_length=120)
    event = models.CharField(max_length=60, help_text="Event or Download Type")
    source_type = models.CharField(max_length=60, blank=True)
    device = models.CharField(max_length=60, blank=True)
    territory = models.CharField(max_length=2, blank=True)
    counts = models.BigIntegerField()
    unique_counts = models.BigIntegerField(null=True, blank=True)
    dimensions = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=["app", "report_name", "report_date"])]

    def __str__(self) -> str:
        return f"{self.report_date} {self.report_name} {self.event} {self.counts}"


class ConversionMetric(models.Model):
    """Derived daily funnel: product-page views from Analytics against units from Sales."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="conversion_metrics")
    date = models.DateField()
    impressions = models.BigIntegerField(default=0)
    page_views = models.BigIntegerField(default=0)
    first_time_downloads = models.BigIntegerField(default=0)
    units = models.BigIntegerField(default=0)
    conversion_rate = models.FloatField(
        null=True, help_text="first-time downloads / page views; null without page views"
    )
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["app", "date"], name="uniq_conversion_day")]
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"{self.app_id} {self.date}"


class AppVersion(models.Model):
    """A shipped version of one platform, dated by the first day it shows up in the sales
    report (downloads and updates carry the version the customer received). App Store
    Connect's own version list adds state and creation time when running on real data."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="versions")
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

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="rating_snapshots")
    date = models.DateField()
    storefront = models.CharField(max_length=2, help_text="lowercase ISO country code")
    count = models.IntegerField()
    star_sum = models.IntegerField(help_text="count x average, rounded: total stars given")
    version = models.CharField(max_length=50, blank=True, help_text="version on sale that day")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app", "date", "storefront"], name="uniq_rating_day")
        ]
        indexes = [models.Index(fields=["app", "date"])]

    def __str__(self) -> str:
        return f"{self.app_id} {self.date} {self.storefront} {self.count}"

    @property
    def average(self) -> float | None:
        return self.star_sum / self.count if self.count else None


class CustomerReview(models.Model):
    """A written review, as App Store Connect files it: under the version it was written for."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="reviews")
    asc_id = models.CharField(max_length=64, unique=True)
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

    def __str__(self) -> str:
        return f"{self.app_id} {self.rating}* {self.created:%Y-%m-%d}"


class ReviewSummary(models.Model):
    """Apple's AI-generated summary of an app's reviews in one territory (API 4.0+)."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="review_summaries")
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


class FxRate(models.Model):
    """Daily reference rate (European Central Bank, via Frankfurter): USD per one unit of
    ``currency``. Only used to put proceeds from many storefronts on one scale; Apple's own
    payout FX differs a little, so converted revenue is an estimate."""

    date = models.DateField()
    currency = models.CharField(max_length=3)
    usd_per_unit = models.DecimalField(max_digits=20, decimal_places=10)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["date", "currency"], name="uniq_fx")]
        indexes = [models.Index(fields=["currency", "date"])]

    def __str__(self) -> str:
        return f"{self.date} {self.currency} {self.usd_per_unit}"


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
