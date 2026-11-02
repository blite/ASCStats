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
