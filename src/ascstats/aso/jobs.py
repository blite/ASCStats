"""Keyword jobs the web app starts (``stats/jobs.py`` has the scheduled ones)."""

from django.tasks import task

from . import keywords
from .models import Keyword


@task(queue_name="store", priority=50)  # ahead of any other store job waiting
def search_keyword(keyword_id: int) -> int:
    """Search one just-added keyword now, so its rank shows without waiting for the nightly
    search, which then skips it: a keyword is searched at most once a day."""
    keyword = Keyword.objects.filter(pk=keyword_id).first()
    return keywords.snapshot(only=[keyword]) if keyword else 0


@task(queue_name="store", priority=40)  # after a just-added keyword's search
def scan_competitor(scan_id: int) -> str:
    """Search the terms a competitor seems to aim at (``rival_keywords.run``): about three
    minutes at the store's pace."""
    from . import rival_keywords
    from .models import KeywordScan

    scan = KeywordScan.objects.filter(pk=scan_id).select_related("competitor").first()
    if scan is None or scan.status != KeywordScan.Status.QUEUED:
        return "skipped"
    return rival_keywords.run(scan).status


@task(queue_name="store", priority=45)  # after a just-added keyword's search
def read_competitor(competitor_id: int) -> str:
    """Read a just-followed competitor's whole listing and its subtitle now, rather than
    waiting for the daily run (``competitors.read_new``)."""
    from . import competitors
    from .models import Competitor

    competitor = Competitor.objects.filter(pk=competitor_id).first()
    return competitors.read_new(competitor) if competitor else "gone"
