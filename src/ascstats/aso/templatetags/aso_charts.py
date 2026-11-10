"""Tags for the Top charts page (``aso/charts.html``)."""

from django import template
from django.urls import reverse

from ascstats.stats.territories import name

register = template.Library()


@register.simple_tag
def chart_url(storefront, genre, day=None, days=1):
    """A category's chart page: its newest capture (/charts/us/7012/), a day of it
    (/charts/us/7012/2026-11-09/) and either compared with 7 or 30 days back (…/7d/)."""
    if day:
        if days == 1:
            return reverse("charts_day", args=[storefront, genre, day])
        return reverse("charts_day_window", args=[storefront, genre, day, days])
    if days == 1:
        return reverse("charts", args=[storefront, genre])
    return reverse("charts_window", args=[storefront, genre, days])


@register.filter
def territory_name(alpha2: str) -> str:
    """us -> United States."""
    return name(alpha2)


@register.filter
def get(mapping, key):
    """A dict's value for ``key``, or None."""
    return mapping.get(key) if mapping else None
