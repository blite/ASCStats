"""Every table the pipeline touches is defined here and created by Django migrations."""

from django.db import models
from django.db.models import Q

# Product Type Identifiers for app updates. They carry the new version in the Version column
# (useful for dating releases) but are not downloads.
UPDATE_PRODUCT_TYPES = ["7", "7F", "7T", "F7"]


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


# Sales rows that count as downloads: the app itself, not its in-app purchases or updates.
DOWNLOAD_ROWS = Q(parent_identifier="") & ~Q(product_type__in=UPDATE_PRODUCT_TYPES)
UPDATE_ROWS = Q(parent_identifier="", product_type__in=UPDATE_PRODUCT_TYPES)


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
    """A shipped version, dated by the first day it shows up in the sales report (downloads
    and updates carry the version the customer received). App Store Connect's own version
    list adds state and creation time when the pipeline runs against the real API."""

    app = models.ForeignKey(App, on_delete=models.CASCADE, related_name="versions")
    version = models.CharField(max_length=50)
    first_seen = models.DateField(null=True, blank=True, help_text="First day in sales data")
    platform = models.CharField(max_length=20, blank=True)
    asc_state = models.CharField(max_length=40, blank=True)
    asc_created = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["app", "version"], name="uniq_version")]
        ordering = ["app", "first_seen"]

    def __str__(self) -> str:
        return f"{self.app_id} {self.version}"


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
