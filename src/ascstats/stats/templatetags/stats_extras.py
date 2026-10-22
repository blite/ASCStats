from django import template
from django.urls import reverse

from ascstats.stats.charts import DEFAULT_PERIOD
from ascstats.stats.revenue import money as _money

register = template.Library()

PERIOD_PAGES = {"overview": "dashboard", "revenue": "revenue"}


@register.simple_tag
def period_url(page, days):
    """A page for a period: /7d/, /revenue/1d/. The default period is the page's own
    URL, so each view has one address."""
    name = PERIOD_PAGES[page]
    if not days or days == DEFAULT_PERIOD:
        return reverse(name)
    return reverse(f"{name}_period", args=[days])


@register.simple_tag
def url_pattern(name, *fields):
    """A URL with {field} placeholders for a form to fill in from its fields, reversed like
    any other URL (so it carries the script prefix): url_pattern 'chart' 'app' 'days' gives
    /chart/{app}/{days}/."""
    sentinels = [str(10**9 + i) for i in range(len(fields))]
    url = reverse(name, args=sentinels)
    for sentinel, field in zip(sentinels, fields, strict=True):
        url = url.replace(sentinel, "{" + field + "}")
    return url


@register.filter
def money(value):
    return "\u2013" if value is None else _money(float(value))


@register.filter
def signed_pct(value):
    """+12% / -8%, or an en dash for None."""
    if value is None:
        return "\u2013"
    return "0%" if round(value) == 0 else f"{value:+.0f}%"
