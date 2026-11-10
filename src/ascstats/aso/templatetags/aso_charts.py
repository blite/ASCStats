"""Tags for the Top charts page (``aso/charts.html``)."""

from django import template
from django.urls import reverse

from ascstats.stats.territories import name

register = template.Library()


@register.simple_tag
def chart_url(storefront, genre, day=None):
    """A category's chart page: its newest fetch (/charts/us/7012/) or one day's
    (/charts/us/7012/2026-11-09/)."""
    if day:
        return reverse("charts_day", args=[storefront, genre, day])
    return reverse("charts", args=[storefront, genre])


@register.filter
def territory_name(alpha2: str) -> str:
    """us -> United States."""
    return name(alpha2)
