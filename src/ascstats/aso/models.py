"""Other apps on the App Store, next to yours: competitors you follow, and where search terms
rank you and them. All of it comes from the public store (``ascstats.store``), keyless."""

from django.db import models

from ascstats.stats.models import App


class Competitor(models.Model):
    """Someone else's app, followed next to one or more of yours. All of it comes from the
    public store listing (iTunes Lookup), refreshed by every rating snapshot."""

    apple_id = models.BigIntegerField(unique=True, help_text="App Apple Identifier")
    name = models.CharField(max_length=255)
    seller = models.CharField(max_length=255, blank=True)
    icon_url = models.URLField(max_length=500, blank=True)
    primary_genre = models.CharField(max_length=100, blank=True)
    released_on = models.DateField(null=True, blank=True, help_text="First App Store release")
    price = models.CharField(max_length=30, blank=True, help_text="As the US store shows it")
    version = models.CharField(max_length=50, blank=True, help_text="Version on sale")
    version_released_on = models.DateField(null=True, blank=True)
    # The listing's text, kept to notice changes (``ListingChange``)
    subtitle = models.CharField(max_length=255, blank=True, help_text="From the store page")
    description = models.TextField(blank=True)
    screenshots = models.JSONField(default=list, blank=True, help_text="iPhone screenshot URLs")
    languages = models.JSONField(default=list, blank=True, help_text="ISO codes, sorted")
    listing_storefront = models.CharField(
        max_length=2, blank=True, help_text="the storefront the kept listing is from"
    )
    listing_seen_on = models.DateField(
        null=True, blank=True, help_text="last full Lookup listing; changes count from the first"
    )
    subtitle_read_on = models.DateField(null=True, blank=True)
    subtitle_version = models.CharField(
        max_length=50, blank=True, help_text="the version on sale when the subtitle was read"
    )
    stores_read_on = models.DateField(
        null=True, blank=True, help_text="last read of its other storefronts' ratings"
    )
    tracked_for = models.ManyToManyField(App, related_name="competitors", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.apple_id})"

    @property
    def store_url(self) -> str:
        """Its App Store page, in the storefront its kept listing is from."""
        return f"https://apps.apple.com/{self.listing_storefront or 'us'}/app/id{self.apple_id}"


class CompetitorSnapshot(models.Model):
    """A competitor's public rating totals in one storefront on one day: the same snapshot as
    ``RatingSnapshot``, taken in the same Lookup call as your own apps."""

    competitor = models.ForeignKey(
        Competitor, on_delete=models.CASCADE, related_name="snapshots", db_index=False
    )
    date = models.DateField()
    storefront = models.CharField(max_length=2, help_text="lowercase ISO country code")
    count = models.IntegerField()
    star_sum = models.IntegerField(help_text="count x average, rounded: total stars given")
    version = models.CharField(max_length=50, blank=True, help_text="version on sale that day")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["competitor", "date", "storefront"], name="uniq_competitor_rating_day"
            )
        ]

    def __str__(self) -> str:
        return f"{self.competitor_id} {self.date} {self.storefront} {self.count}"


class CompetitorRelease(models.Model):
    """A competitor version, as the listing showed it: Apple's release date and notes. The
    listing only carries the current version, so history starts when tracking does."""

    competitor = models.ForeignKey(
        Competitor, on_delete=models.CASCADE, related_name="releases", db_index=False
    )
    version = models.CharField(max_length=50)
    released_on = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    first_seen = models.DateField(help_text="First snapshot that showed it")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["competitor", "version"], name="uniq_competitor_release"
            )
        ]
        ordering = ["competitor", "released_on"]

    def __str__(self) -> str:
        return f"{self.competitor_id} {self.version}"


class ListingChange(models.Model):
    """One field of a competitor's listing that changed between two daily snapshots: its
    name, subtitle, description, screenshots, price, category, seller or languages. The
    first snapshot is the baseline, so a competitor's changes start the day after."""

    class Field(models.TextChoices):
        NAME = "name", "Name"
        SUBTITLE = "subtitle", "Subtitle"
        DESCRIPTION = "description", "Description"
        SCREENSHOTS = "screenshots", "Screenshots"
        PRICE = "price", "Price"
        GENRE = "primary_genre", "Category"
        SELLER = "seller", "Seller"
        LANGUAGES = "languages", "Languages"

    competitor = models.ForeignKey(
        Competitor, on_delete=models.CASCADE, related_name="changes", db_index=False
    )
    date = models.DateField()
    field = models.CharField(max_length=20, choices=Field.choices)
    before = models.JSONField(help_text="text, or a list for screenshots and languages")
    after = models.JSONField()
    storefront = models.CharField(max_length=2)
    version = models.CharField(max_length=50, blank=True, help_text="version on sale after")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["competitor", "date", "field"], name="uniq_listing_change"
            )
        ]
        ordering = ["-date", "competitor", "field"]

    def __str__(self) -> str:
        return f"{self.competitor_id} {self.date} {self.field}"


class Keyword(models.Model):
    """A search term in one storefront, tracked for one or more of your apps. One search a
    day ranks every app at once, so a term two apps track is searched once. A term no app
    tracks was searched by a competitor's keyword scan (``KeywordScan``) and is not searched
    again until someone tracks it."""

    term = models.CharField(max_length=100, help_text="lowercase, single-spaced")
    storefront = models.CharField(max_length=2, default="us", help_text="lowercase ISO code")
    tracked_for = models.ManyToManyField(App, related_name="keywords", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["term", "storefront"], name="uniq_keyword")]
        ordering = ["term", "storefront"]

    def __str__(self) -> str:
        return f"{self.term} ({self.storefront})"


class KeywordSnapshot(models.Model):
    """One day's search results for a keyword: every app's position, and the first few
    results as the store showed them."""

    keyword = models.ForeignKey(
        Keyword, on_delete=models.CASCADE, related_name="snapshots", db_index=False
    )
    date = models.DateField()
    ids = models.JSONField(default=list, help_text="Apple IDs in result order, up to 200")
    top = models.JSONField(
        default=list,
        help_text="the first 25 results: {id, name, seller, icon, ratings, average}",
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["keyword", "date"], name="uniq_keyword_day")]

    def __str__(self) -> str:
        return f"{self.keyword_id} {self.date} ({len(self.ids)} results)"

    def rank(self, apple_id: int) -> int | None:
        """1-based position of ``apple_id``, or None when it is not among the results."""
        try:
            return self.ids.index(apple_id) + 1
        except ValueError:
            return None


class KeywordSuggestion(models.Model):
    """A weekly look at how the store's search suggestions offer a keyword: the evidence
    behind its popularity estimate (``aso.scoring``)."""

    class Reach(models.TextChoices):
        HIT = "hit", "suggested from its first letters"
        LISTED = "listed", "suggested only once typed in full"
        ABSENT = "absent", "not suggested"

    keyword = models.ForeignKey(
        Keyword, on_delete=models.CASCADE, related_name="suggestions", db_index=False
    )
    date = models.DateField()
    reach = models.CharField(max_length=10, choices=Reach.choices)
    prefix = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="characters typed when it was first suggested"
    )
    position = models.PositiveSmallIntegerField(null=True, blank=True)
    continuations = models.PositiveSmallIntegerField(
        help_text="suggestions that are the term or extend it by whole words"
    )
    requests = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["keyword", "date"], name="uniq_keyword_suggestion")
        ]
        ordering = ["keyword", "date"]

    def __str__(self) -> str:
        return f"{self.keyword_id} {self.date} {self.reach}"


class KeywordScan(models.Model):
    """A look at which searches a competitor comes up for (``aso.rival_keywords``): terms
    taken from its listing and the store's suggestions, each searched once. The searches are
    saved as ordinary ``KeywordSnapshot`` rows of untracked keywords, so tracking one later
    keeps its first day."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Searching"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    competitor = models.ForeignKey(
        Competitor, on_delete=models.CASCADE, related_name="scans", db_index=False
    )
    storefront = models.CharField(max_length=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    terms = models.JSONField(default=list, help_text="the terms looked at, searched or reused")
    searched = models.PositiveSmallIntegerField(default=0, help_text="new searches it asked")
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["competitor", "storefront", "-created_at"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.competitor_id} {self.storefront} {self.status}"


class ChartCapture(models.Model):
    """A top free, paid or grossing chart of one category in one storefront as fetched on one
    day (a refetch that day replaces it): the apps' Apple IDs in chart order, up to 100
    (``store.CHART_DEPTH``). Each app's listing is a ``ChartApp``, so the same app in many
    charts is stored once."""

    class Kind(models.TextChoices):
        FREE = "free", "Free"
        PAID = "paid", "Paid"
        GROSSING = "grossing", "Grossing"

    date = models.DateField()
    storefront = models.CharField(max_length=2)
    genre = models.PositiveIntegerField(help_text="Apple genre ID; 0 for the overall chart")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    ids = models.JSONField(default=list, help_text="Apple IDs in chart order")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["storefront", "genre", "kind", "date"], name="uniq_chart_capture"
            )
        ]
        ordering = ["-date", "storefront", "genre", "kind"]

    def __str__(self) -> str:
        return f"{self.date} {self.storefront} {self.genre} {self.kind} ({len(self.ids)})"


class ChartApp(models.Model):
    """An app as the chart feed last showed it: enough to list it in a chart."""

    apple_id = models.BigIntegerField(unique=True)
    name = models.CharField(max_length=255)
    seller = models.CharField(max_length=255, blank=True)
    icon_url = models.URLField(max_length=500, blank=True)
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, blank=True)
    released_on = models.DateField(null=True, blank=True)
    summary = models.TextField(blank=True, help_text="the start of its description")
    seen_on = models.DateField(help_text="the last capture that listed it")

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.apple_id})"
