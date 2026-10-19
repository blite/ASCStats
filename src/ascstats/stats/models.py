"""Every table the pipeline touches is defined here and created by Django migrations."""

from django.db import models


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
