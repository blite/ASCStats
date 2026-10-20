from django import template

from ascstats.stats.revenue import money as _money

register = template.Library()


@register.filter
def money(value):
    return "\u2013" if value is None else _money(float(value))


@register.filter
def signed_pct(value):
    """+12% / -8%, or an en dash for None."""
    if value is None:
        return "\u2013"
    return "0%" if round(value) == 0 else f"{value:+.0f}%"
