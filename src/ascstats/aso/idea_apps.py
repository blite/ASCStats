"""Ideas: apps not built yet, kept to research their keywords and competitors before you
commit to them. An idea is an ``App`` with ``is_idea`` set and a negative ID, so its Keywords and
Competitors tabs are the same pages a real app has; the stats, imports and rating snapshots
never see it (``AppQuerySet.active``). Its keywords are searched once, when added, and its
competitors ride the daily snapshot like any other.

Once the app is real, ``make_real`` carries the research over: into the app App Store Connect
already imported, or onto the idea itself, which takes the app's Apple ID.
"""

from __future__ import annotations

import httpx
from django.db import transaction

from ascstats.stats.models import App, next_idea_id
from ascstats.stats.ratings import store_listing

from .competitors import CompetitorError, lookup_listing, parse
from .models import Competitor


class IdeaError(Exception):
    """Something a person starting or converting an idea can fix; the message says what."""


def create(name: str) -> App:
    """A new idea called ``name``."""
    name = " ".join(name.split())
    if not name:
        raise IdeaError("Give the idea a name.")
    return App.objects.create(apple_id=next_idea_id(), name=name, is_idea=True)


@transaction.atomic
def make_real(idea: App, text: str, transport: httpx.BaseTransport | None = None) -> App:
    """Turn ``idea`` into the app ``text`` names (an App Store link or an Apple ID), keeping
    every keyword and competitor it follows; returns the real app. An app the site already
    has takes them over and the idea goes; otherwise the idea becomes that app, and the next
    import fills in its sales. Becoming the app, it takes the app's name and listing from the
    store in the link's storefront, so the idea's working title doesn't stay; if the store
    doesn't list it there yet, the idea's name stays (rename it in the admin)."""
    try:
        apple_id, storefront = parse(text)
    except CompetitorError as error:
        raise IdeaError(str(error)) from None
    real = App.objects.filter(apple_id=apple_id).first()  # never an idea: parse() is positive
    # an app can't follow itself: drop it if the research followed it as a competitor
    itself = Competitor.objects.filter(apple_id=apple_id).first()
    if itself is not None:
        itself.tracked_for.remove(idea)
    if real is None:
        idea.apple_id, idea.is_idea = apple_id, False
        try:
            listing = lookup_listing(apple_id, storefront, transport)
        except CompetitorError:  # not listed there yet: nothing to copy
            listing = {}
        idea.name = listing.get("trackName") or idea.name
        idea.save(update_fields=["apple_id", "is_idea", "name"])
        store_listing(idea, listing)
        return idea
    real.keywords.add(*idea.keywords.all())
    real.competitors.add(*idea.competitors.all())
    idea.delete()
    return real
