from django import template

from ascstats.stats.revenue import money as _money

register = template.Library()


@register.filter
def money(value):
    return "\u2013" if value is None else _money(float(value))


@register.filter
def signed_pct(value):
    """+12% / -8%, or an en dash for None."""
    return "\u2013" if value is None else f"{value:+.0f}%"
